"""The video-server side of recordings: stream-info request, media channels, end of data."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from pyezvizapi import stream as st

from custom_components.ezviz_instant_bell import clip_stream
from custom_components.ezviz_instant_bell.clip_stream import ClipUnavailable, iter_media, open_playback

LIVE = "ysproto://203.0.113.5:8554/live?biz=1&ssn=TOKEN&cln=3&timestamp=1"
BEGIN, END = datetime(2026, 10, 28, 15, 42, 3), datetime(2026, 10, 28, 15, 42, 52)


class FakeSocket:
    """Answers the stream-info request with ``replies`` and records what was sent."""

    def __init__(self, replies: bytes) -> None:
        self.sent: list[bytes] = []
        self._data = replies
        self.timeouts: list[float] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        chunk, self._data = self._data[:n], self._data[n:]
        if not chunk:
            raise TimeoutError
        return chunk

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        pass


def packet(channel: int, body: bytes, code: int = 0) -> bytes:
    return st.encode_vtm_packet(body, channel=channel, message_code=code)


def info_reply(result: int = 0) -> bytes:
    varint = bytearray()
    value = result
    while True:
        byte = value & 0x7F
        value >>= 7
        varint.append(byte | (0x80 if value else 0))
        if not value:
            break
    body = bytes((0x08, *varint)) + b"\x22\x03SSN"  # result, and the stream session (field 4)
    return packet(st.VtmChannel.MESSAGE, body, int(st.VtmMessageCode.STREAMINFO_RSP))


def client_with(replies: bytes):
    sock = FakeSocket(replies)
    client = clip_stream._playback_client_class()(
        "ysproto://203.0.113.5:8554/playback?x=1",
        timeout=5,
        socket_factory=lambda _addr, _timeout: sock,
    )
    return client, sock


def test_stream_info_request_asks_for_a_recording_stream():
    client, sock = client_with(info_reply(0))
    response = client.start()
    assert response.result == 0
    sent = sock.sent[0]
    assert sent[8:].endswith(bytes((8 << 3, clip_stream.PLAYBACK_STREAM_TYPE)))  # field 8 = 10 after the body
    assert b"/playback?" in sent


def test_media_comes_from_both_channels_and_keepalives_are_answered():
    replies = (
        info_reply(0)
        + packet(0x01, b"AAAA")
        + packet(0x02, b"BBBB")
        + packet(0x00, b"", int(st.VtmMessageCode.KEEPALIVE_REQ))
        + packet(0x00, b"control")  # not video
        + packet(0x01, b"CC")
    )
    client, sock = client_with(replies)
    client.start()
    data = list(iter_media(client, expected_seconds=10))  # the end: the fake runs dry (a timeout)
    assert b"".join(data) == b"AAAABBBBCC"
    assert len(sock.sent) == 2  # stream-info request and one keepalive answer
    assert sock.timeouts and sock.timeouts[-1] == clip_stream.READ_TIMEOUT  # short silence limit once video flows


def test_no_video_at_all_is_an_error():
    client, _ = client_with(info_reply(0))
    client.start()
    with pytest.raises(ClipUnavailable):
        list(iter_media(client, expected_seconds=10))


def test_an_encrypted_recording_is_reported():
    client, _ = client_with(info_reply(0) + packet(0x0B, b"secret"))
    client.start()
    with pytest.raises(ClipUnavailable, match="encrypted"):
        list(iter_media(client, expected_seconds=10))


def test_a_refused_recording_closes_the_connection():
    client, sock = client_with(info_reply(6103))
    closed = []
    client.close = lambda: closed.append(True)  # type: ignore[method-assign]
    with patch.object(clip_stream, "_playback_client_class", return_value=lambda *a, **k: client), patch(
        "pyezvizapi.cloud_stream.get_cloud_stream_info", return_value={"stream_url": LIVE}
    ):
        with pytest.raises(Exception):
            open_playback(object(), "MAIN-CAM", 1, BEGIN, END)
    assert closed
