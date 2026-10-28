"""Diagnostic sensor: how many of the 10 EZVIZ terminal slots are used."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import InstantBellConfigEntry
from .const import DOMAIN
from .coordinator import StatusCoordinator
from .entity import StatusEntity
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
    entities: list[SensorEntity] = [TerminalCountSensor(coordinator, entry)]

    status = hub.coordinator
    for serial in hub.serials:
        for description in SENSORS:
            entities.append(StatusSensor(hub, status, serial, description))
        # Only when the device reports a microphone volume.
        if (status.data or {}).get(serial, {}).get("mic_volume") is not None:
            entities.append(StatusSensor(hub, status, serial, MIC_VOLUME))
    async_add_entities(entities)


def _online(value: Any) -> str:
    return "online" if value in (1, "1", True, "online") else "offline"


def _motion(value: Any) -> str:
    return "detected" if value in (1, "1", True, "true") else "none"


def _yes_no(value: Any) -> str:
    return "yes" if value in (1, "1", True, "true") else "no"


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _timestamp(value: Any) -> datetime | None:
    """EZVIZ writes the time as local wall-clock text."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    except ValueError:
        return None


@dataclass(frozen=True)
class SensorDef:
    """One sensor that shows a value of the status."""

    key: str
    icon: str
    transform: Callable[[Any], Any] | None = None
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    diagnostic: bool = False
    enabled_default: bool = True


SENSORS: tuple[SensorDef, ...] = (
    SensorDef("name", "mdi:label"),
    SensorDef("version", "mdi:update"),
    SensorDef("status", "mdi:power", _online),
    SensorDef("signal", "mdi:wifi", _number, unit=PERCENTAGE, diagnostic=True, enabled_default=False),
    SensorDef("ssid", "mdi:wifi", diagnostic=True, enabled_default=False),
    SensorDef("local_ip", "mdi:ip", diagnostic=True, enabled_default=False),
    SensorDef("wan_ip", "mdi:wan", diagnostic=True, enabled_default=False),
    SensorDef("motion", "mdi:run", _motion),
    SensorDef("last_alarm_time", "mdi:clock-alert", _timestamp, SensorDeviceClass.TIMESTAMP),
    SensorDef("alarm_name", "mdi:alert"),
    SensorDef(
        "seconds_last_trigger",
        "mdi:timer",
        None,
        SensorDeviceClass.DURATION,
        UnitOfTime.SECONDS,
        diagnostic=True,
        enabled_default=False,
    ),
    SensorDef("upgrade_available", "mdi:update", _yes_no, diagnostic=True, enabled_default=False),
)
MIC_VOLUME = SensorDef("mic_volume", "mdi:microphone", None, unit=PERCENTAGE, diagnostic=True)


class StatusSensor(StatusEntity, SensorEntity):
    """A value from the doorbell's status."""

    def __init__(
        self, hub: InstantBellHub, coordinator: StatusCoordinator, serial: str, description: SensorDef
    ) -> None:
        super().__init__(hub, coordinator, serial, description.key, unique_suffix=f"sensor_{description.key}")
        self._description = description
        self._attr_icon = description.icon
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.unit
        if description.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_entity_registry_enabled_default = description.enabled_default

    @property
    def native_value(self) -> Any:
        value = self.status.get(self._description.key)
        transform = self._description.transform
        if transform is None:
            return value
        try:
            return transform(value)
        except (ValueError, TypeError):
            return None


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
