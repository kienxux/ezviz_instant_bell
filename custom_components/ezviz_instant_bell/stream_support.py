"""What the live video needs from the EZVIZ account.

Everything here is blocking (run it in the executor). It uses the one session
of the hub; calls on that session go through the hub's lock so that they never
overlap with the status reads.

Credit: the p2p-register requirement and the LAN control key (CAS
get-encryption on the bare serial) were found by albrzmr
(https://github.com/albrzmr/ezviz_hp7, MIT); the camera-key and
video-encryption handling follows the ``ezviz_hp7`` integration by Bobsilvio
(MIT).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

import requests
from requests import RequestException

_LOGGER = logging.getLogger(__name__)

LAN_AES_KEY_TTL = 3600.0


def bare_serial(serial: str) -> str:
    """``MAIN-CAM`` -> ``MAIN`` (the part the verification code belongs to)."""
    return serial.split("-", 1)[0]


class StreamSupport:
    """Keys, LAN address and encryption settings for the live video."""

    def __init__(
        self,
        get_client: Callable[[], Any],
        region_host: str,
        call_lock: threading.Lock | None = None,
    ) -> None:
        self._get_client = get_client
        self._host = region_host
        self._lock = call_lock
        self._lan_aes_cache: dict[str, tuple[bytes, float]] = {}
        self._lan_ip_cache: dict[str, str] = {}
        self._cam_key_cache: dict[str, str] = {}
        # Serials for which the cloud asked for a one-time code (20002).
        self.key_needs_otp: set[str] = set()

    # -- small helpers ----------------------------------------------------

    @property
    def client(self) -> Any:
        return self._get_client()

    @staticmethod
    def _bare_serial(serial: str) -> str:
        return bare_serial(serial)

    def _locked(self):
        return self._lock if self._lock is not None else nullcontext()

    @staticmethod
    def get_related_device(serial: str) -> str:
        """The camera-module sub-serial of a ``MAIN-CAM`` serial (else the serial)."""
        return serial.split("-", 1)[1] if "-" in serial else serial

    # -- LAN stream -------------------------------------------------------

    def _p2p_register(self) -> None:
        """Authorise this client to open the doorbell's LAN streaming ports (best effort)."""
        client = self.client
        if client is None:
            return
        try:
            with self._locked():
                session_id = client.export_token().get("session_id")
        except Exception:  # noqa: BLE001
            session_id = None
        if not session_id:
            return
        url = f"https://{self._host}/v3/p2pbusiness/configurations/p2p"
        headers = {
            "appId": "ys7",
            "clientType": "1",
            "netType": "WIFI",
            "User-Agent": "EZVIZ/CloudClient",
        }
        try:
            resp = requests.post(url, headers=headers, data={"sessionId": session_id}, timeout=8.0)
            _LOGGER.debug("EZVIZ p2p register -> %d", resp.status_code)
        except RequestException as exc:
            _LOGGER.debug("EZVIZ p2p register failed (best effort): %s", exc)

    def fetch_lan_aes_key(self, serial: str, force: bool = False) -> bytes:
        """The 16-byte AES-128 LAN control key of the device (cached for an hour)."""
        from pyezvizapi.cas import EzvizCAS

        bare = bare_serial(serial)
        if not force:
            cached = self._lan_aes_cache.get(bare)
            if cached and (time.monotonic() - cached[1]) < LAN_AES_KEY_TTL:
                return cached[0]
        client = self.client
        if client is None:
            raise RuntimeError("EZVIZ client not initialised for LAN key fetch")

        self._p2p_register()
        try:
            with self._locked():
                token = client.export_token()
            info = EzvizCAS(token).cas_get_encryption(bare)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"CAS get-encryption failed: {exc}") from exc

        session = (info or {}).get("Response", {}).get("Session", {})
        key_str = str(session.get("@Key") or "")
        if len(key_str) != 16:
            result = str((info or {}).get("Response", {}).get("Result"))
            hint = ""
            if result in ("1052175", "1052170"):
                # CAS accepts the request but will not give a LAN key; on CP5/CP7
                # this is Image/Video Encryption being on (credit: albrzmr).
                hint = (
                    " - if this is a CP5/CP7, turn OFF Image/Video Encryption in the "
                    "EZVIZ app (device settings); the LAN stream needs it disabled"
                )
            raise RuntimeError(f"invalid LAN AES key from CAS (Result={result}, key={key_str!r}){hint}")
        key_bytes = key_str.encode("ascii")
        self._lan_aes_cache[bare] = (key_bytes, time.monotonic())
        return key_bytes

    def invalidate_lan_aes_key(self, serial: str | None = None) -> None:
        """Forget the cached LAN key(s); called when the device refuses the handshake."""
        if serial is None:
            self._lan_aes_cache.clear()
        else:
            self._lan_aes_cache.pop(bare_serial(serial), None)

    def get_local_ip(self, serial: str) -> str | None:
        """The doorbell's LAN address from the account's CONNECTION data.

        Remembered, so that the LAN stream keeps working while the cloud is slow.
        """
        bare = bare_serial(serial)
        try:
            from pyezvizapi.local_stream import _local_sdk_endpoint_from_client

            client = self.client
            if client is None:
                raise RuntimeError("client unavailable")
            with self._locked():
                endpoint = _local_sdk_endpoint_from_client(client, serial)
            host = getattr(endpoint, "host", None)
            if host:
                self._lan_ip_cache[bare] = str(host)
                return str(host)
            raise RuntimeError("no host in CONNECTION data")
        except Exception as exc:  # noqa: BLE001
            cached = self._lan_ip_cache.get(bare)
            if cached:
                _LOGGER.debug("Local IP of %s not resolved (%s), using remembered %s", serial, exc, cached)
                return cached
            _LOGGER.debug("Local IP of %s not resolved: %s", serial, exc)
            return None

    # -- encryption -------------------------------------------------------

    def fetch_camera_key_with_code(self, serial: str, code: str) -> str | None:
        """Camera encryption key, with the one-time code EZVIZ e-mailed.

        Without a code the cloud mails one; the second call must carry it.
        """
        client = self.client
        if client is None:
            return None
        bare = bare_serial(serial)
        last: Exception | None = None
        for candidate in (bare, serial):
            try:
                with self._locked():
                    key = client.get_cam_key(candidate, smscode=code or None)
            except Exception as exc:  # noqa: BLE001
                last = exc
                _LOGGER.debug("Keyed camera key fetch failed for %s: %s", candidate, exc)
                continue
            if key:
                self.key_needs_otp.discard(bare)
                self._cam_key_cache[bare] = str(key)
                _LOGGER.info("Camera key obtained for %s", candidate)
                return str(key)
        if last is not None:
            raise last
        return None

    def get_camera_encryption_key(self, serial: str) -> str | None:
        """Camera encryption key, remembered for the session.

        Needed to decrypt the video when Image/Video Encryption is on (firmware
        from V5.3.6 on ships with it enabled and no switch in the app).
        """
        from pyezvizapi.exceptions import EzvizAuthVerificationCode

        bare = bare_serial(serial)
        cached = self._cam_key_cache.get(bare)
        if cached is not None:
            return cached
        client = self.client
        if client is None:
            return None
        needs_otp = False
        for candidate in (bare, serial):
            try:
                with self._locked():
                    key = client.get_cam_key(candidate)
            except EzvizAuthVerificationCode:
                # 20002: the cloud will not hand the key to an un-elevated
                # session, and asking is what makes EZVIZ mail the code.
                needs_otp = True
                continue
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Camera key fetch failed for %s: %s", candidate, exc)
                continue
            if key:
                self.key_needs_otp.discard(bare)
                self._cam_key_cache[bare] = str(key)
                _LOGGER.info("Got camera encryption key for %s", candidate)
                return str(key)
        if needs_otp:
            self.key_needs_otp.add(bare)
            _LOGGER.warning(
                "EZVIZ has just e-mailed a 4-digit security code for %s (subject '[Device Encryption] "
                "Security Code'). It unlocks this device's encrypted video and expires in about 30 "
                "minutes. Enter it in Settings > Repairs, or call the action "
                "ezviz_instant_bell.fetch_encryption_key with code: \"<the code>\". Do not use the "
                "6-character code from the device label here.",
                bare,
            )
        return None

    def set_video_encryption(self, serial: str, enable: bool, verification_code: str) -> bool:
        """Turn the device's Image/Video Encryption on or off.

        Encryption blocks the LAN stream. Needs the verification code (normally
        the 6 characters on the label; on firmware that hides the switch, the
        short code EZVIZ e-mails). This changes a security setting, so it is
        never done automatically.
        """
        client = self.client
        if client is None:
            return False
        code = (verification_code or "").strip().upper()
        # The code belongs to the physical doorbell: the bare serial matches it.
        candidates = [bare_serial(serial)]
        if serial not in candidates:
            candidates.append(serial)
        # Some firmware wants the current encryption password too; it defaults
        # to the verification code, so try both ways.
        payloads: list[dict[str, Any]] = [{}]
        if not enable:
            payloads.append({"old_password": code})
        last_exc: Exception | None = None
        for candidate in candidates:
            for extra in payloads:
                try:
                    with self._locked():
                        client.set_video_enc(
                            candidate,
                            enable=1 if enable else 0,
                            camera_verification_code=code,
                            **extra,
                        )
                    _LOGGER.info("Video encryption set to %s (serial=%s)", "ON" if enable else "OFF", candidate)
                    return True
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    _LOGGER.warning(
                        "Video encryption change rejected (serial=%s variant=%s): %s",
                        candidate,
                        "oldPassword" if extra else "validateCode",
                        exc,
                    )
        _LOGGER.error(
            "Could not change video encryption for %s. If this is '1011 / verification code "
            "incorrect', the label code is not what EZVIZ wants: on firmware that hides the "
            "encryption switch it accepts only the short numeric code it e-mails to the account.",
            serial,
        )
        if last_exc is not None:
            raise last_exc
        return False
