"""Where the live video comes from: the local network (CPD7 protocol) or an already started source."""
# Adapted for EZVIZ HP7 / CP7 Video Intercom from the ezviz_hp7 integration by Bobsilvio
# (MIT, https://github.com/Bobsilvio/ezviz_hp7); see THIRD_PARTY_LICENSES.md.
# The local-network video protocol and the motion-JPEG view are by albrzmr (MIT).
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .stream_support import StreamSupport

_LOGGER = logging.getLogger(__name__)


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

    def start(self) -> "Cpd7LanSource":
        """Open the LAN session (blocking — run in an executor)."""
        from .cpd7 import Cpd7LanClient, StreamDecoder

        key = self._api.fetch_lan_aes_key(self._serial)
        local_ip = self._api.get_local_ip(self._serial)
        if not local_ip:
            raise RuntimeError(
                f"could not resolve LAN IP for {self._serial} "
                "(device not on this network?)"
            )
        related = self._api.get_related_device(self._serial)
        client = Cpd7LanClient(
            local_ip, related, key, channel=self._channel,
            stream_quality=self._stream_quality,
        )
        try:
            client.start()
        except Exception:
            # Same level as the "prewarm failed" warning below so this shows
            # up without needing debug logging enabled — ip/related let us
            # tell "stale/wrong LAN target" apart from "device genuinely not
            # answering there".
            _LOGGER.warning(
                "StreamRelay: LAN InviteStream rejected (serial=%s ip=%s "
                "related=%s) — see the error below for the device's raw "
                "response",
                self._serial, local_ip, related,
            )
            # fetch_lan_aes_key() caches the control key for
            # LAN_AES_KEY_TTL (1h) and is never invalidated on
            # failure — every retry in that window keeps re-encrypting
            # INIT/INVITE with the same key. If the doorbell rotates its
            # local control key on a reboot, the device can't decrypt our
            # request and falls back to exactly this generic plaintext
            # rejection (Result 3, no <Session>) — indistinguishable on the
            # wire from any other InviteStream refusal. Drop the cached key
            # so the next attempt asks CAS for a fresh one instead of
            # silently reusing a stale one for up to the full TTL.
            self._api.invalidate_lan_aes_key(self._serial)
            raise
        self._client = client
        self._decoder = StreamDecoder(client.ecdh_priv)
        _LOGGER.info(
            "StreamRelay: LAN source up (serial=%s ip=%s stream=%s)",
            self._serial, local_ip, self._stream_quality,
        )
        return self

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
