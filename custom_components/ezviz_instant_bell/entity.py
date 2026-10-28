"""Shared base for the entities of one doorbell."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .hub import InstantBellHub


class InstantBellEntity(Entity):
    """Entity that belongs to one doorbell."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: InstantBellHub, serial: str, key: str) -> None:
        self._hub = hub
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=hub.names.get(serial, serial),
            manufacturer="EZVIZ",
            model="Video intercom",
        )
