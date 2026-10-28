"""Image entity: the picture that came with the latest event."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import InstantBellConfigEntry
from .entity import InstantBellEntity
from .hub import InstantBellHub
from .parsing import BellEvent, same_device


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(LastPictureEntity(hass, hub, serial) for serial in hub.serials)


class LastPictureEntity(InstantBellEntity, ImageEntity):
    """Latest picture from a ring, unlock or gate event."""

    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, hub: InstantBellHub, serial: str) -> None:
        InstantBellEntity.__init__(self, hub, serial, "last_picture")
        ImageEntity.__init__(self, hass)
        self._data: bytes | None = None
        self._kind: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._hub.signal_picture, self._handle_picture)
        )

    @callback
    def _handle_picture(self, event: BellEvent, data: bytes) -> None:
        if not same_device(event.serial, self._serial):
            return
        self._data = data
        self._kind = event.kind
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._data

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"event_kind": self._kind}
