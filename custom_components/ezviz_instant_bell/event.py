"""Event entity: fires when the bell rings or a door or gate is opened."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InstantBellConfigEntry
from .entity import InstantBellEntity
from .hub import InstantBellHub
from .parsing import EVENT_KINDS, BellEvent, same_device


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(BellEventEntity(hub, serial) for serial in hub.serials)


class BellEventEntity(InstantBellEntity, EventEntity):
    """One entity per doorbell with the event types ring, door_unlock, gate_open, other."""

    _attr_event_types = EVENT_KINDS

    def __init__(self, hub: InstantBellHub, serial: str) -> None:
        super().__init__(hub, serial, "bell")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._hub.signal_event, self._handle_event)
        )

    @callback
    def _handle_event(self, event: BellEvent) -> None:
        if not same_device(event.serial, self._serial):
            return
        self._trigger_event(
            event.kind,
            {
                "source": event.source,
                "message": event.title,
                "occurred": event.occurred.isoformat() if event.occurred else None,
                "code": event.code,
            },
        )
        self.async_write_ha_state()
