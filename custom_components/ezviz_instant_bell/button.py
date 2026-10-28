"""Buttons that open the door and the gate. Pressing is always a deliberate act."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InstantBellConfigEntry
from .entity import ActionEntity
from .hub import InstantBellHub


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    # Version 0.1.0 had optional buttons under other IDs; they are replaced.
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.domain == "button" and item.unique_id.endswith(("_open_door", "_open_gate")):
            registry.async_remove(item.entity_id)
    entities: list[UnlockButton] = []
    for serial in hub.serials:
        entities.append(UnlockButton(hub, serial, "unlock_gate", hub.gate_lock_no))
        entities.append(UnlockButton(hub, serial, "unlock_door", hub.door_lock_no))
    async_add_entities(entities)


class UnlockButton(ActionEntity, ButtonEntity):
    """Opens exactly one lock; never falls back to a different one."""

    def __init__(self, hub: InstantBellHub, serial: str, key: str, lock_no: int) -> None:
        super().__init__(hub, serial, key)
        self._lock_no = lock_no

    async def async_press(self) -> None:
        await self._hub.async_unlock(self._serial, self._lock_no)
