"""Shared bases for the entities of one doorbell."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LEGACY_DOMAIN
from .coordinator import StatusCoordinator
from .hub import InstantBellHub
from .state import DEFAULT_MODEL


def model_of(hub: InstantBellHub, serial: str) -> str:
    """Short model label of a door station (``HP7``, ``CPD5`` ...)."""
    coordinator = getattr(hub, "coordinator", None)
    data = (coordinator.data or {}) if coordinator is not None else {}
    known = (data.get(serial) or {}).get("model") or hub.remembered_model(serial)
    return str(known or DEFAULT_MODEL)


def door_name(hub: InstantBellHub, serial: str) -> str:
    """Name of a door station: the one from the EZVIZ app, else model and serial."""
    return hub.names.get(serial) or f"EZVIZ {model_of(hub, serial)} ({serial})"


def monitor_name(hub: InstantBellHub, door_serial: str, monitor_serial: str) -> str:
    """Name of an indoor monitor: the one from the EZVIZ app, else model and serial.

    On some models the app gives the monitor the name of its door station; it
    then gets "Monitor" added, so the two devices can be told apart.
    """
    name = hub.monitor_names.get(monitor_serial)
    if not name:
        return f"EZVIZ {model_of(hub, door_serial)} Monitor ({monitor_serial})"
    if name == hub.names.get(door_serial):
        return f"{name} Monitor"
    return name


def door_device_info(hub: InstantBellHub, serial: str) -> DeviceInfo:
    """The device of a door station."""
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=door_name(hub, serial),
        manufacturer="EZVIZ",
        model=model_of(hub, serial),
    )


def monitor_device_info(hub: InstantBellHub, door_serial: str, monitor_serial: str) -> DeviceInfo:
    """The device of an indoor monitor (linked to its door station by ``link_monitors``)."""
    return DeviceInfo(
        identifiers={(DOMAIN, monitor_serial)},
        name=monitor_name(hub, door_serial, monitor_serial),
        manufacturer="EZVIZ",
        model=f"{model_of(hub, door_serial)} Monitor",
    )



@callback
def link_monitors(hass: HomeAssistant, hub: InstantBellHub) -> None:
    """Show each indoor monitor as connected to its door station.

    Done in the device registry, not with ``DeviceInfo``: its ``via_device``
    is deprecated in newer Home Assistant and ``via_device_id`` is unknown to
    older versions.
    """
    registry = dr.async_get(hass)
    by_serial = {
        serial: device
        for device in dr.async_entries_for_config_entry(registry, hub.entry.entry_id)
        for domain, serial in device.identifiers
        if domain == DOMAIN
    }
    for door, monitors in hub.monitors.items():
        parent = by_serial.get(door)
        for monitor in monitors:
            child = by_serial.get(monitor)
            # A monitor given with the serial of the door station itself is the same device (#27).
            if parent is None or child is None or child.id == parent.id:
                continue
            if child.via_device_id != parent.id:
                registry.async_update_device(child.id, via_device_id=parent.id)


@callback
def remove_entities(hass: HomeAssistant, platform: str, unique_ids: list[str]) -> None:
    """Remove entities that earlier versions made for a device that cannot use them.

    Earlier versions gave every chosen device the unlock buttons and the chime
    controls; on a camera they would stay as "no longer provided".
    """
    registry = er.async_get(hass)
    for unique_id in unique_ids:
        if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
            registry.async_remove(entity_id)


class InstantBellEntity(Entity):
    """Entity of one doorbell that is fed by events, not by polling."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: InstantBellHub, serial: str, key: str) -> None:
        self._hub = hub
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = door_device_info(hub, serial)


class StatusEntity(CoordinatorEntity[StatusCoordinator]):
    """Entity that shows a value of the regularly read status.

    The unique ID is built the way the ``ezviz_hp7`` integration built it
    (``ezviz_hp7_<serial>_<suffix>``), so entities moved over from it keep
    their entity ID.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        hub: InstantBellHub,
        coordinator: StatusCoordinator,
        serial: str,
        key: str,
        *,
        unique_suffix: str | None = None,
        owner: str | None = None,
    ) -> None:
        """``serial`` is the door station whose status is shown.

        ``owner`` is the serial the entity belongs to when that is an indoor
        monitor; it carries the entity's device and unique ID.
        """
        super().__init__(coordinator)
        self._hub = hub
        self._serial = serial
        self._owner = owner or serial
        self._attr_translation_key = key
        self._attr_unique_id = f"{LEGACY_DOMAIN}_{self._owner}_{unique_suffix or key}"
        if owner and owner != serial:
            self._attr_device_info = monitor_device_info(hub, serial, owner)
        else:
            self._attr_device_info = door_device_info(hub, serial)

    @property
    def status(self) -> dict[str, Any]:
        """Latest status values of this entity's door station."""
        return (self.coordinator.data or {}).get(self._serial) or {}

    @property
    def available(self) -> bool:
        return super().available and self._serial in (self.coordinator.data or {})


class ActionEntity(Entity):
    """Entity that does something (a button) and does not depend on the status."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: InstantBellHub, serial: str, key: str) -> None:
        self._hub = hub
        self._serial = serial
        self._attr_translation_key = key
        self._attr_unique_id = f"{LEGACY_DOMAIN}_{serial}_{key}"
        self._attr_device_info = door_device_info(hub, serial)
