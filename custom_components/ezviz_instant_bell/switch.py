"""Switches: chime, do not disturb, privacy, armed, name-tag light, doorbell indicator, keyboard backlight, motion sound alert."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InstantBellConfigEntry
from .const import DOMAIN, LEGACY_DOMAIN
from .coordinator import StatusCoordinator
from .device_api import DeviceApi
from .entity import StatusEntity, remove_entities
from .hub import InstantBellHub

# The cloud needs a few seconds before its status shows a change. Until then
# the switch shows the value that was sent, so it does not jump back and forth.
OPTIMISTIC_SECONDS = 20.0


@dataclass(frozen=True)
class SwitchDef:
    """How one switch reads and writes its value."""

    key: str
    read: Callable[[dict[str, Any], str], bool | None]
    write: Callable[[DeviceApi, str, bool], None]


def _flat(name: str) -> Callable[[dict[str, Any], str], bool | None]:
    return lambda status, _owner: status.get(name)


def _by_monitor(name: str) -> Callable[[dict[str, Any], str], bool | None]:
    def read(status: dict[str, Any], owner: str) -> bool | None:
        values = status.get(name)
        return values.get(owner) if isinstance(values, dict) else None

    return read


CHIME = SwitchDef("chime_sound", _flat("chime_is_on"), lambda a, s, v: a.set_chime_fields(s, doorbell_enable=int(v)))
CHIME_PIR = SwitchDef("chime_pir", _flat("chime_pir_is_on"), lambda a, s, v: a.set_chime_fields(s, pir_enable=int(v)))
MONITOR_CHIME = SwitchDef(
    "chime_sound_monitor",
    _by_monitor("chime_is_on_monitors"),
    lambda a, s, v: a.set_chime_fields(s, doorbell_enable=int(v)),
)
MONITOR_CHIME_PIR = SwitchDef(
    "chime_pir_monitor",
    _by_monitor("chime_pir_is_on_monitors"),
    lambda a, s, v: a.set_chime_fields(s, pir_enable=int(v)),
)
DND = SwitchDef("dnd", _flat("dnd_on"), lambda a, s, v: a.set_dnd(s, v))
PRIVACY = SwitchDef("privacy", _flat("privacy_on"), lambda a, s, v: a.set_privacy(s, v))
DEFENCE = SwitchDef("defence", _flat("defence_on"), lambda a, s, v: a.set_defence(s, v))
LABEL_LIGHT = SwitchDef("label_light", _flat("label_light_on"), lambda a, s, v: a.set_label_light(s, v))
INDICATOR_LIGHT = SwitchDef(
    "indicator_light", _flat("indicator_light_on"), lambda a, s, v: a.set_indicator_light(s, v)
)
KEYPAD_BACKLIGHT = SwitchDef(
    "keypad_backlight", _flat("keypad_backlight_on"), lambda a, s, v: a.set_keypad_backlight(s, v)
)
# Made only when the door station reports the setting (not every model has it).
OPTIONAL_SWITCHES = (INDICATOR_LIGHT, KEYPAD_BACKLIGHT)

DOOR_SWITCHES = (CHIME, DND, PRIVACY, DEFENCE, LABEL_LIGHT, CHIME_PIR)
# Only an HP7 / CP7 style door station has a chime and a name-tag light.
INTERCOM_SWITCHES = (CHIME, LABEL_LIGHT, CHIME_PIR, INDICATOR_LIGHT)
MONITOR_SWITCHES = (MONITOR_CHIME, MONITOR_CHIME_PIR)


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    entities: list[ChimeSwitch] = []
    for serial in hub.serials:
        intercom = hub.is_intercom(serial)
        if not intercom:
            remove_entities(hass, "switch", [f"{LEGACY_DOMAIN}_{serial}_{d.key}" for d in INTERCOM_SWITCHES])
        entities.extend(
            ChimeSwitch(hub, hub.coordinator, serial, d)
            for d in DOOR_SWITCHES
            if intercom or d not in INTERCOM_SWITCHES
        )
        reported = (hub.coordinator.data or {}).get(serial, {})
        if intercom:
            entities.extend(
                ChimeSwitch(hub, hub.coordinator, serial, d)
                for d in OPTIONAL_SWITCHES
                if f"{d.key}_on" in reported
            )
        for monitor in hub.monitors.get(serial, []):
            entities.extend(ChimeSwitch(hub, hub.coordinator, serial, d, owner=monitor) for d in MONITOR_SWITCHES)
    async_add_entities(entities)


class ChimeSwitch(StatusEntity, SwitchEntity):
    """A switch with the optimistic grace period."""

    def __init__(
        self,
        hub: InstantBellHub,
        coordinator: StatusCoordinator,
        serial: str,
        definition: SwitchDef,
        *,
        owner: str | None = None,
    ) -> None:
        super().__init__(hub, coordinator, serial, definition.key, owner=owner)
        self._definition = definition
        self._sent: bool | None = None
        self._sent_until = 0.0

    @property
    def is_on(self) -> bool | None:
        if time.monotonic() < self._sent_until:
            return self._sent
        return self._definition.read(self.status, self._owner)

    async def _async_set(self, value: bool) -> None:
        try:
            await self._hub.async_call(self._definition.write, self._hub.api, self._owner, value)
        except Exception as err:  # noqa: BLE001 - the library raises several types
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="setting_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._sent = value
        self._sent_until = time.monotonic() + OPTIMISTIC_SECONDS
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
