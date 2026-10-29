"""Regular reading of the doorbell's status (sensors, switches, chime ...)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, STATUS_GRACE_CYCLES, UPDATE_INTERVAL

if TYPE_CHECKING:
    from .hub import InstantBellHub

_LOGGER = logging.getLogger(__name__)


class StatusCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Status of every chosen door station: ``{serial: {...}}``.

    The EZVIZ cloud now and then answers slowly or with an error and recovers
    on the next try, so a short run of failures keeps the last values instead
    of turning every entity to "unavailable". After a longer run one new login
    is tried, because an expired session would otherwise stay broken.
    """

    def __init__(self, hass: HomeAssistant, entry: Any, hub: InstantBellHub) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} status",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self._hub = hub
        self._fail_streak = 0
        self._last_alarm_time: dict[str, str | None] = {}
        self._last_alarm_detail: dict[str, dict[str, Any]] = {}
        self._slowed_for_stream = False

    def _apply_stream_aware_interval(self) -> None:
        """Read the status a quarter as often while someone watches the local stream.

        Several of the requests are passed on to the doorbell, and answering
        them competes with its streaming on its small processor. Back to the
        normal rate as soon as the last viewer leaves.
        """
        watching = any(relay.has_lan_viewers for relay in self._hub.live_relays.values())
        if watching == self._slowed_for_stream:
            return
        self._slowed_for_stream = watching
        self.update_interval = timedelta(seconds=UPDATE_INTERVAL * (4 if watching else 1))
        _LOGGER.debug("Status interval now %ss (local stream viewers: %s)", self.update_interval.total_seconds(), watching)

    def _read(self) -> dict[str, dict[str, Any]]:
        """Blocking: read all door stations with one pass over the cloud."""
        api = self._hub.api
        infos = api.fetch_infos()
        if not infos:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for serial in self._hub.serials:
            status = api.build_status(serial, self._hub.monitors.get(serial, []), infos)
            if status:
                result[serial] = status
        return result

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        self._apply_stream_aware_interval()
        failure: str | None = None
        data: dict[str, dict[str, Any]] = {}
        try:
            data = await self._hub.async_call(self._read)
            if not data:
                failure = "empty answer from EZVIZ"
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:  # noqa: BLE001 - the library raises several types
            failure = str(err) or type(err).__name__

        if failure is not None:
            self._fail_streak += 1
            if self._fail_streak <= STATUS_GRACE_CYCLES and self.data:
                _LOGGER.debug("EZVIZ status failed (%s in a row), keeping last values: %s", self._fail_streak, failure)
                return self.data
            if self._fail_streak == STATUS_GRACE_CYCLES + 1:
                _LOGGER.warning("EZVIZ status failing for %s cycles, logging in again", self._fail_streak)
                try:
                    await self._hub.async_relogin()
                except ConfigEntryAuthFailed:
                    raise
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("New login failed: %s", err)
            raise UpdateFailed(f"EZVIZ status failed: {failure}")

        self._fail_streak = 0
        for serial, status in data.items():
            await self._add_alarm_detail(serial, status)
        return data

    async def _add_alarm_detail(self, serial: str, status: dict[str, Any]) -> None:
        """Fetch the detailed record only when a new alarm has appeared."""
        alarm_time = status.get("last_alarm_time")
        if alarm_time and alarm_time != self._last_alarm_time.get(serial):
            detail = await self._hub.async_call(self._hub.api.latest_alarm, serial)
            if detail:
                self._last_alarm_detail[serial] = detail
            self._last_alarm_time[serial] = alarm_time
        if serial in self._last_alarm_detail:
            status["latest_alarm_detail"] = self._last_alarm_detail[serial]
