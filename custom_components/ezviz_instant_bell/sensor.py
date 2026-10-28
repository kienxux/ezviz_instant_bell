"""Diagnostic sensor: how many of the 10 EZVIZ terminal slots are used."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed

from . import InstantBellConfigEntry
from .const import DOMAIN
from .hub import InstantBellHub

_LOGGER = logging.getLogger(__name__)

TERMINAL_LIMIT = 10
WARN_AT = 8
ISSUE_ID = "terminals_almost_full"


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    coordinator = TerminalCoordinator(hass, entry, hub)
    await coordinator.async_config_entry_first_refresh()
    async_add_entities([TerminalCountSensor(coordinator, entry)])


class TerminalCoordinator(DataUpdateCoordinator[list[str]]):
    """Reads the terminal list every half hour and raises a repair notice when nearly full."""

    def __init__(self, hass: HomeAssistant, entry: InstantBellConfigEntry, hub: InstantBellHub) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} terminals", update_interval=timedelta(minutes=30)
        )
        self._hub = hub

    async def _async_update_data(self) -> list[str]:
        try:
            names = await self._hub.async_terminal_names()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(str(err)) from err
        if len(names) >= WARN_AT:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_ID,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_ID,
                translation_placeholders={"count": str(len(names)), "limit": str(TERMINAL_LIMIT)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_ID)
        return names


class TerminalCountSensor(CoordinatorEntity[TerminalCoordinator], SensorEntity):
    """Number of terminals registered on the EZVIZ account."""

    _attr_has_entity_name = True
    _attr_translation_key = "terminals"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: TerminalCoordinator, entry: InstantBellConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_terminals"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"EZVIZ account {entry.data['username']}",
            manufacturer="EZVIZ",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "limit": TERMINAL_LIMIT,
            "by_name": dict(Counter(self.coordinator.data)),
        }
