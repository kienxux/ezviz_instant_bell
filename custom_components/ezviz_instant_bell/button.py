"""Buttons that open the door and the gate. Pressing is always a deliberate act."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InstantBellConfigEntry
from .const import CONF_CREATE_BUTTONS, DOMAIN
from .entity import ActionEntity, InstantBellEntity
from .hub import InstantBellHub


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    registry = er.async_get(hass)
    entities: list[ButtonEntity] = []
    for serial in hub.serials:
        entities.append(UnlockButton(hub, serial, "unlock_gate", hub.gate_lock_no))
        entities.append(UnlockButton(hub, serial, "unlock_door", hub.door_lock_no))
        # Version 0.1.0 had optional buttons "Open door" / "Open gate". Phone
        # widgets and dashboards may use them, so they stay (same entity ID)
        # for everyone who has them or had switched them on.
        for key, lock_no in (("open_door", hub.door_lock_no), ("open_gate", hub.gate_lock_no)):
            unique_id = f"{serial}_{key}"
            known = registry.async_get_entity_id("button", DOMAIN, unique_id)
            if not known and not entry.options.get(CONF_CREATE_BUTTONS):
                continue
            if not known:
                # Registered again under the ID 0.1.0 gave it: device name + button name.
                registry.async_get_or_create(
                    "button",
                    DOMAIN,
                    unique_id,
                    config_entry=entry,
                    suggested_object_id=f"{slugify(hub.names.get(serial, serial))}_{key}",
                )
            entities.append(OpenButton(hub, serial, key, lock_no))
    async_add_entities(entities)


class UnlockButton(ActionEntity, ButtonEntity):
    """Opens exactly one lock; never falls back to a different one."""

    def __init__(self, hub: InstantBellHub, serial: str, key: str, lock_no: int) -> None:
        super().__init__(hub, serial, key)
        self._lock_no = lock_no

    async def async_press(self) -> None:
        await self._hub.async_unlock(self._serial, self._lock_no)


class OpenButton(InstantBellEntity, ButtonEntity):
    """The button of version 0.1.0 (kept so that its entity ID goes on working)."""

    def __init__(self, hub: InstantBellHub, serial: str, key: str, lock_no: int) -> None:
        super().__init__(hub, serial, key)
        self._lock_no = lock_no

    async def async_press(self) -> None:
        await self._hub.async_unlock(self._serial, self._lock_no)
