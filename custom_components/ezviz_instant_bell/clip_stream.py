"""Fetch a recording of the SD card through the EZVIZ video servers (blocking).

It speaks the same protocol as the live view (``pyezvizapi.stream``), with the
three differences that pyEzvizApi pull request #266 found for recordings:

* the request asks for ``/playback`` with a begin and end time (see ``recordings``),
* the stream-info request carries an extra field (the stream type, 10),
* the picture arrives on channel 2 as well as channel 1.

Credit: pyEzvizApi pull request #266 (Apache-2.0); confirmed on a real door station.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import datetime
from typing import Any

from .recordings import build_playback_url

_LOGGER = logging.getLogger(__name__)

PLAYBACK_CLIENT_TYPE = 3
PLAYBACK_STREAM_TYPE = 10
CLIENT_VERSION = "v3.9.0.20251120"
MEDIA_CHANNELS = (0x01, 0x02)
KEEPALIVE_SECONDS = 5.0
READ_TIMEOUT = 8.0  # silence this long after video has arrived means the recording is over
FIRST_DATA_TIMEOUT = 25.0  # the servers can take a while to find the recording


class ClipUnavailable(Exception):
    """The servers would not play this recording."""


def open_playback(
    client: Any,
    serial: str,
    channel: int,
    begin: datetime,
    end: datetime,
    *,
    call_lock: Any = None,
) -> Any:
    """Return a started playback connection (call ``close`` when done). Blocking.

    ``call_lock`` guards the account session while the addresses are asked for;
    the video itself is read without it.
    """
    from pyezvizapi.cloud_stream import get_cloud_stream_info

    with call_lock or nullcontext():
        info = get_cloud_stream_info(
            client, serial, channel=channel, client_type=PLAYBACK_CLIENT_TYPE, refresh_vtm=True
        )
    url = build_playback_url(
        info["stream_url"], serial, channel, begin, end, client_type=PLAYBACK_CLIENT_TYPE
    )
    playback = _playback_client_class()(url, timeout=FIRST_DATA_TIMEOUT, client_version=CLIENT_VERSION)
    try:
        response = playback.start()
        if response.result:
            raise ClipUnavailable(f"The EZVIZ servers refused the recording (code {response.result})")
    except Exception:
        playback.close()
        raise
    return playback


def iter_media(playback: Any, *, expected_seconds: float) -> Iterator[bytes]:
    """Yield the video bytes (MPEG program stream) until the recording ends.

    The end is when the connection closes, goes quiet for ``READ_TIMEOUT``
    seconds, or when well more than the recording length has passed.
    """
    from pyezvizapi.stream import VtmMessageCode

    started = time.monotonic()
    last_keepalive = started
    got = 0
    while time.monotonic() - started < expected_seconds * 3 + 60:
        if time.monotonic() - last_keepalive >= KEEPALIVE_SECONDS:
            playback.send_keepalive()
            last_keepalive = time.monotonic()
        try:
            packet = playback.read_packet()
        except Exception as err:  # noqa: BLE001 - timeout, closed socket: the end of the data
            if not got:
                raise ClipUnavailable(f"No video arrived: {err}") from err
            _LOGGER.debug("Recording stream ended after %s bytes: %s", got, err)
            return
        if packet.message_code == VtmMessageCode.KEEPALIVE_REQ:
            playback.send_keepalive(message_code=VtmMessageCode.KEEPALIVE_RSP)
            last_keepalive = time.monotonic()
            continue
        if packet.channel in MEDIA_CHANNELS and packet.body:
            if not got:  # from now on silence means the end, not a slow start
                sock = getattr(playback, "_socket", None)
                if sock is not None:
                    sock.settimeout(READ_TIMEOUT)
            got += len(packet.body)
            yield packet.body
        elif packet.channel == 0x0B:
            raise ClipUnavailable("The recording is encrypted (video encryption is on)")


def _playback_client_class() -> Any:
    """The library's stream client with the recording tweaks (built late: needs pyezvizapi)."""
    from pyezvizapi import stream as st

    class PlaybackClient(st.VtmStreamClient):
        """``VtmStreamClient`` that asks for a recording and reads every channel."""

        def start(self, *, vtm_stream_key: str | None = None, max_control_packets: int = 20, max_redirects: int = 3):  # type: ignore[override]
            redirects = 0
            while True:
                self.connect()
                request = st.build_stream_info_request(
                    self.stream_url, vtm_stream_key=vtm_stream_key, client_version=self.client_version
                )
                # field 8 (varint): the stream type of a recording
                self.send_packet(request + bytes((8 << 3, PLAYBACK_STREAM_TYPE)))
                for _ in range(max_control_packets):
                    packet = self.read_packet()
                    if packet.message_code != st.VtmMessageCode.STREAMINFO_RSP:
                        continue
                    self.stream_info = st.parse_stream_info_response(packet.body)
                    redirect, key = self.stream_info.streamurl, self.stream_info.vtmstreamkey
                    if self.stream_info.result and redirect and key and redirects < max_redirects:
                        self.close()
                        self.stream_url, vtm_stream_key = redirect, key
                        redirects += 1
                        break
                    return self.stream_info
                else:
                    raise st.PyEzvizError("Timed out waiting for the stream info of the recording")

        def read_packet(self):  # type: ignore[override]
            header = self._recv_exact(st.VTM_HEADER_SIZE)
            if header[0] != st.VTM_MAGIC:
                raise st.PyEzvizError("VTM magic byte not found")
            length = int.from_bytes(header[2:4], "big")
            body = self._recv_exact(length)
            return st.VtmPacket(
                channel=header[1],
                length=length,
                sequence=int.from_bytes(header[4:6], "big"),
                message_code=int.from_bytes(header[6:8], "big"),
                body=body,
            )

    return PlaybackClient
