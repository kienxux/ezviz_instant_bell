"""Buttons that open the door and the gate. Pressing is always a deliberate act."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InstantBellConfigEntry
from .const import (
    CONF_CREATE_BUTTONS,
    CONF_DOOR_LOCK_NO,
    CONF_GATE_LOCK_NO,
    DEFAULT_DOOR_LOCK_NO,
    DEFAULT_GATE_LOCK_NO,
)
from .entity import InstantBellEntity
from .hub import InstantBellHub


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    if not entry.options.get(CONF_CREATE_BUTTONS, False):
        # Off by default so existing buttons (and phone widgets that use them)
        # stay untouched. Remove any that were created earlier.
        registry = er.async_get(hass)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id):
            if item.domain == "button":
                registry.async_remove(item.entity_id)
        return
    door = entry.options.get(CONF_DOOR_LOCK_NO, DEFAULT_DOOR_LOCK_NO)
    gate = entry.options.get(CONF_GATE_LOCK_NO, DEFAULT_GATE_LOCK_NO)
    entities: list[UnlockButton] = []
    for serial in hub.serials:
        entities.append(UnlockButton(hub, serial, "open_door", door))
        entities.append(UnlockButton(hub, serial, "open_gate", gate))
    async_add_entities(entities)


class UnlockButton(InstantBellEntity, ButtonEntity):
    """Opens exactly one lock; never falls back to a different one."""

    def __init__(self, hub: InstantBellHub, serial: str, key: str, lock_no: int) -> None:
        super().__init__(hub, serial, key)
        self._lock_no = lock_no

    async def async_press(self) -> None:
        await self._hub.async_unlock(self._serial, self._lock_no)
