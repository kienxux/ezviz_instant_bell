"""Camera that shows the picture of the latest alarm."""

from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InstantBellConfigEntry
from .coordinator import StatusCoordinator
from .entity import StatusEntity
from .hub import InstantBellHub


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(LastSnapshotCamera(hub, hub.coordinator, serial) for serial in hub.serials)


class LastSnapshotCamera(StatusEntity, Camera):
    """Still picture of the latest alarm (there is no live video in this camera)."""

    def __init__(self, hub: InstantBellHub, coordinator: StatusCoordinator, serial: str) -> None:
        StatusEntity.__init__(self, hub, coordinator, serial, "last_snapshot")
        Camera.__init__(self)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        url = self.status.get("last_alarm_pic")
        if not url:
            return None
        return await self._hub.async_download_picture(url, self._serial)
