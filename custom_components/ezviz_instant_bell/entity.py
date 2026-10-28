"""Shared bases for the entities of one doorbell."""

from __future__ import annotations

from typing import Any

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
    return str((data.get(serial) or {}).get("model") or DEFAULT_MODEL)


def door_device_info(hub: InstantBellHub, serial: str) -> DeviceInfo:
    """The device of a door station. Named like the earlier integration named it."""
    model = model_of(hub, serial)
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"EZVIZ {model} ({serial})",
        manufacturer="EZVIZ",
        model=model,
    )


def monitor_device_info(hub: InstantBellHub, door_serial: str, monitor_serial: str) -> DeviceInfo:
    """The device of an indoor monitor."""
    model = f"{model_of(hub, door_serial)} Monitor"
    return DeviceInfo(
        identifiers={(DOMAIN, monitor_serial)},
        name=f"EZVIZ {model} ({monitor_serial})",
        manufacturer="EZVIZ",
        model=model,
    )


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
