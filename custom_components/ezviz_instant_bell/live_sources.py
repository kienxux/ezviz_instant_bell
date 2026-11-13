"""Where the live video comes from: the local network (CPD7 protocol) or an already started source."""
# Adapted for EZVIZ HP7 / CP7 Video Intercom from the ezviz_hp7 integration by Bobsilvio
# (MIT, https://github.com/Bobsilvio/ezviz_hp7); see THIRD_PARTY_LICENSES.md.
# The local-network video protocol and the motion-JPEG view are by albrzmr (MIT).
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .stream_support import StreamSupport

_LOGGER = logging.getLogger(__name__)

# The door station sends about 15 frames a second, so a read that waits this
# long already means trouble; four in a row (about 8 s) end the session.
LAN_READ_TIMEOUT = 2.0


class Cpd7LanSource:
    """VTM-shaped wrapper around the CPD7 LAN pipeline.

    Exposes ``start()`` / ``iter_payloads()`` / ``close()`` so it is a
    drop-in replacement for the cloud VTM session inside the relay's
    broadcast reader. ``iter_payloads()`` yields MPEG-PS bytes, exactly
    like the cloud path, so the rest of the pipeline (PesParser -> ffmpeg)
    is unchanged. LAN protocol credit: albrzmr (see cpd7/__init__.py).
    """

    def __init__(
        self, api: "StreamSupport", serial: str, channel: int = 1,
        stream_quality: str = "main",
    ) -> None:
        self._api = api
        self._serial = serial
        self._channel = channel
        self._stream_quality = stream_quality
        self._client: Any = None
        self._decoder: Any = None
        self._closed = False
        self._started_at = 0.0

    def start(self) -> "Cpd7LanSource":
        """Open the LAN session (blocking — run in an executor).

        Key and address are the stored ones, so the start needs no cloud.
        Only when the device refuses them or is not reached there are both
        asked from the cloud again, and the start is tried once more if
        either has changed (new address from the router, new key).
        """
        from .cpd7 import StreamDecoder

        key = self._api.fetch_lan_aes_key(self._serial)
        local_ip = self._api.get_local_ip(self._serial)
        if not local_ip:
            raise RuntimeError(
                f"could not resolve LAN IP for {self._serial} "
                "(device not on this network?)"
            )
        try:
            client = self._connect(key, local_ip)
        except Exception:
            try:
                fresh = (
                    self._api.fetch_lan_aes_key(self._serial, force=True),
                    self._api.get_local_ip(self._serial, force=True),
                )
            except Exception as exc:  # noqa: BLE001 - the first error is the one to report
                _LOGGER.debug("StreamRelay: LAN key/address not refreshed: %s", exc)
                fresh = (key, local_ip)
            if not fresh[1] or fresh == (key, local_ip):
                raise
            _LOGGER.info(
                "StreamRelay: LAN %s of %s changed in the cloud, trying again",
                "address" if fresh[0] == key else "key", self._serial,
            )
            key, local_ip = fresh
            client = self._connect(key, local_ip)
        self._client = client
        self._decoder = StreamDecoder(client.ecdh_priv)
        self._started_at = time.monotonic()
        _LOGGER.info(
            "StreamRelay: LAN source up (serial=%s ip=%s stream=%s)",
            self._serial, local_ip, self._stream_quality,
        )
        return self

    def _connect(self, key: bytes, local_ip: str) -> Any:
        from .cpd7 import Cpd7LanClient

        related = self._api.get_related_device(self._serial)
        client = Cpd7LanClient(
            local_ip, related, key, channel=self._channel,
            stream_quality=self._stream_quality, recv_timeout=LAN_READ_TIMEOUT,
        )
        try:
            client.start()
        except Exception:
            # Same level as the "prewarm failed" warning so this shows up
            # without debug logging — ip/related tell "stale/wrong LAN
            # target" apart from "device genuinely not answering there".
            _LOGGER.warning(
                "StreamRelay: LAN InviteStream rejected (serial=%s ip=%s "
                "related=%s) — see the error below for the device's raw "
                "response",
                self._serial, local_ip, related,
            )
            raise
        return client

    @property
    def streamssn(self) -> str:
        return f"lan:{self._serial}"

    def iter_payloads(self):
        """Yield MPEG-PS payloads decoded from the LAN play socket."""
        empty_strikes = 0
        while not self._closed:
            buf = self._client.read_chunk()
            if not buf:
                empty_strikes += 1
                # Tolerate brief gaps; give up after sustained silence.
                if empty_strikes > 3:
                    if not self._closed:
                        _LOGGER.info(
                            "StreamRelay: local stream of %s ended after %.0f s (%s)",
                            self._serial, time.monotonic() - self._started_at,
                            "closed by the door station"
                            if getattr(self._client, "peer_closed", False)
                            else f"no data for {4 * LAN_READ_TIMEOUT:.0f} s",
                        )
                    break
                continue
            empty_strikes = 0
            self._decoder.feed(buf)
            out = self._decoder.take()
            if out:
                yield out

    def close(self) -> None:
        self._closed = True
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


class _PreStartedSource:
    """Wrap an already-started source so a second ``start()`` is a no-op.

    Used by the "auto" path, which starts the LAN source eagerly to probe
    whether it works before committing the broadcast reader to it.
    """

    def __init__(self, src: Any) -> None:
        self._src = src

    def start(self) -> Any:
        return self._src

    def iter_payloads(self):
        return self._src.iter_payloads()

    def close(self) -> None:
        self._src.close()

    @property
    def streamssn(self) -> str:
        return getattr(self._src, "streamssn", "lan")
