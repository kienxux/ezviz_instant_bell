"""Reading and changing the settings of the doorbell and its indoor monitor.

Every method blocks: call them in the executor, and hold the hub's call lock
(the EZVIZ session is shared and not safe for parallel requests).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from . import state
from .alarms import alarm_from_answer

_LOGGER = logging.getLogger(__name__)

CHIME_KEY = "ChimeMusic"
CHIME_CACHE_SECONDS = 45.0
CHIME_FAILS_BEFORE_SKIP = 3


class DeviceApi:
    """Settings calls for the devices of one EZVIZ account."""

    def __init__(self, get_client: Callable[[], Any]) -> None:
        self._get_client = get_client
        self._chime_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._chime_fails: dict[str, int] = {}
        self._chime_skip: set[str] = set()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def fetch_infos(self) -> dict[str, Any]:
        """Device records of the whole account, by serial (one cloud request)."""
        infos = self._get_client().get_device_infos()
        return infos if isinstance(infos, dict) else {}

    def build_status(
        self, serial: str, monitors: list[str], infos: dict[str, Any]
    ) -> dict[str, Any]:
        """All values of one door station (and its monitors) for the entities."""
        from pyezvizapi.camera import EzvizCamera

        client = self._get_client()
        info = infos.get(serial)
        if not isinstance(info, dict):
            return {}
        camera = EzvizCamera(client, serial, device_obj=info)
        data = state.base_status(camera.status(refresh=True) or {})
        data.update(state.read_extra_states(info))

        chimes: dict[str, dict[str, Any]] = {}
        for target in [serial, *monitors]:
            config = self.get_chime_config(target)
            if config is not None:
                chimes[target] = state.chime_values(config)
        data.update(state.chime_status(chimes, serial, monitors))

        keys = state.read_key_list(info)
        if keys is not None:
            data["keys"] = keys
        data["model"] = state.detect_model(info)
        return data

    def latest_alarm(self, serial: str) -> dict[str, Any] | None:
        """Detailed record of the newest alarm (card, face, picture id ...)."""
        try:
            return alarm_from_answer(self._get_client().get_alarminfo(serial, limit=1))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("get_alarminfo failed for %s: %s", serial, err)
            return None

    # ------------------------------------------------------------------
    # Chime
    # ------------------------------------------------------------------

    def get_chime_config(self, serial: str) -> dict[str, Any] | None:
        if serial in self._chime_skip:
            return None
        cached = self._chime_cache.get(serial)
        if cached and time.monotonic() - cached[1] < CHIME_CACHE_SECONDS:
            return cached[0]
        try:
            answer = self._get_client().get_dev_config(serial, 1, CHIME_KEY)
        except Exception as err:  # noqa: BLE001 - the library raises several types
            self._chime_fails[serial] = self._chime_fails.get(serial, 0) + 1
            text = (str(err) or repr(err)).lower()
            if (
                "2000" in text
                or "not exist" in text
                or "403" in text
                or "forbidden" in text
                or self._chime_fails[serial] >= CHIME_FAILS_BEFORE_SKIP
            ):
                # This serial has no chime (for example the door station
                # itself): stop asking until the next restart.
                self._chime_skip.add(serial)
                _LOGGER.debug("No chime settings for %s, not asking again: %s", serial, err)
            return None
        config = state.parse_chime_answer(answer)
        if config is not None:
            self._chime_fails.pop(serial, None)
            self._chime_cache[serial] = (config, time.monotonic())
        return config

    def set_chime_fields(self, serial: str, **fields: int) -> None:
        """Change some chime settings and keep the others as they are."""
        merged = state.merge_chime(self.get_chime_config(serial), **fields)
        client = self._get_client()
        import json

        from pyezvizapi.api_endpoints import API_ENDPOINT_DEVCONFIG_BY_KEY

        params = {"key": CHIME_KEY, "value": json.dumps(merged, separators=(",", ":"))}
        try:
            answer = client._request_json(  # noqa: SLF001 - no public call sends it as query
                "PUT", f"{API_ENDPOINT_DEVCONFIG_BY_KEY}{serial}/1/op", params=params
            )
            client._ensure_ok(answer, "Could not set the chime")  # noqa: SLF001
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Chime write as query failed (%s), trying the public call", err)
            client.set_dev_config_kv(serial, 1, CHIME_KEY, merged)
        self._chime_cache.pop(serial, None)

    # ------------------------------------------------------------------
    # Switches
    # ------------------------------------------------------------------

    def set_dnd(self, serial: str, enable: bool) -> None:
        self._get_client().do_not_disturb(serial, enable=int(bool(enable)))

    def set_privacy(self, serial: str, enable: bool) -> None:
        self._get_client().switch_status(serial, state.PRIVACY_SWITCH_TYPE, bool(enable))

    def set_defence(self, serial: str, enable: bool) -> None:
        self._get_client().set_camera_defence(serial, int(bool(enable)))

    def set_label_light(self, serial: str, enable: bool) -> None:
        """The name-plate light: IoT feature first (newer models), else switch 611."""
        client = self._get_client()
        try:
            client.set_iot_feature(
                serial,
                resource_identifier="Video",
                local_index="1",
                domain_id="LightCtrl",
                action_id="NightLightEnable",
                value={"value": bool(enable)},
            )
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Label light by IoT feature not accepted (%s), using switch 611", err)
        client.switch_status(serial, state.LABEL_LIGHT_SWITCH_TYPE, bool(enable))
