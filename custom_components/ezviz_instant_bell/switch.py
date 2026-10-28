"""Switches: chime, do not disturb, privacy, armed, name-tag light, motion sound alert."""

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
from .const import DOMAIN
from .coordinator import StatusCoordinator
from .device_api import DeviceApi
from .entity import StatusEntity
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

DOOR_SWITCHES = (CHIME, DND, PRIVACY, DEFENCE, LABEL_LIGHT, CHIME_PIR)
MONITOR_SWITCHES = (MONITOR_CHIME, MONITOR_CHIME_PIR)


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    entities: list[ChimeSwitch] = []
    for serial in hub.serials:
        entities.extend(ChimeSwitch(hub, hub.coordinator, serial, d) for d in DOOR_SWITCHES)
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
