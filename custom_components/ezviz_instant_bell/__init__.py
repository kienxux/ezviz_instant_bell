"""EZVIZ video intercoms (HP7 / CP7 family) with instant ring picture and one-tap door."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import StatusCoordinator
from .hub import InstantBellHub
from .migration import async_migrate_legacy
from .parsing import same_device

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

LEGACY_ISSUE = "legacy_running"

type InstantBellConfigEntry = ConfigEntry[InstantBellHub]

SERVICE_SCHEMA = vol.Schema({vol.Optional("serial"): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (they work for every set-up account)."""

    async def _unlock(call: ServiceCall, door: bool) -> None:
        wanted = str(call.data.get("serial") or "").strip()
        matches: list[tuple[InstantBellHub, str]] = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            hub = getattr(entry, "runtime_data", None)
            if not isinstance(hub, InstantBellHub):
                continue
            for serial in hub.serials:
                if not wanted or serial == wanted or same_device(serial, wanted):
                    matches.append((hub, serial))
        if not matches:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_such_doorbell")
        if len(matches) > 1 and not wanted:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="serial_needed")
        hub, serial = matches[0]
        await hub.async_unlock(serial, hub.door_lock_no if door else hub.gate_lock_no)

    async def unlock_door(call: ServiceCall) -> None:
        await _unlock(call, True)

    async def unlock_gate(call: ServiceCall) -> None:
        await _unlock(call, False)

    hass.services.async_register(DOMAIN, "unlock_door", unlock_door, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, "unlock_gate", unlock_gate, SERVICE_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: InstantBellConfigEntry) -> bool:
    """Set up one EZVIZ account."""
    hub = InstantBellHub(hass, entry)
    await hub.async_start()
    entry.async_on_unload(hub.async_stop)

    coordinator = StatusCoordinator(hass, entry, hub)
    hub.coordinator = coordinator
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = hub

    # Entities of the earlier ezviz_hp7 integration move over before our own
    # entities are created, so that they keep their entity IDs.
    serials = set(hub.serials)
    for monitors in hub.monitors.values():
        serials.update(monitors)
    _, blocked = async_migrate_legacy(hass, entry, serials)
    if blocked:
        ir.async_create_issue(
            hass,
            DOMAIN,
            LEGACY_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=LEGACY_ISSUE,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, LEGACY_ISSUE)

    @callback
    def _refresh_soon(_event: object) -> None:
        # A ring or unlock changes the "latest alarm"; do not wait for the next read.
        entry.async_create_background_task(hass, coordinator.async_request_refresh(), f"{DOMAIN}_refresh")

    entry.async_on_unload(async_dispatcher_connect(hass, hub.signal_event, _refresh_soon))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: InstantBellConfigEntry) -> bool:
    """Unload the account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: InstantBellConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
