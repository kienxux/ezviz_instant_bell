"""EZVIZ video intercoms (HP7 / CP7 family) with instant ring picture and one-tap door."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .api import async_remove_card, async_setup_card
from .const import DOMAIN
from .keys import with_keys
from .coordinator import StatusCoordinator
from .entity import link_monitors
from .hub import InstantBellHub
from .parsing import same_device
from .stream_share import register_share_views

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
                if not hub.is_intercom(serial):
                    continue  # a camera or doorbell without locks
                if not wanted or serial == wanted or same_device(serial, wanted):
                    matches.append((hub, serial))
        if not matches:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_such_doorbell")
        if len(matches) > 1 and not wanted:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="serial_needed")
        hub, serial = matches[0]
        await hub.async_unlock(serial, hub.door_lock_no if door else hub.gate_lock_no, call.context)

    async def unlock_door(call: ServiceCall) -> None:
        await _unlock(call, True)

    async def unlock_gate(call: ServiceCall) -> None:
        await _unlock(call, False)

    def _hubs() -> list[tuple[ConfigEntry, InstantBellHub]]:
        found = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            hub = getattr(entry, "runtime_data", None)
            if isinstance(hub, InstantBellHub):
                found.append((entry, hub))
        return found

    async def set_video_encryption(call: ServiceCall) -> None:
        wanted = str(call.data.get("serial") or "").strip()
        for _entry, hub in _hubs():
            for serial in hub.serials:
                if not wanted or serial == wanted or same_device(serial, wanted):
                    try:
                        await hass.async_add_executor_job(
                            hub.stream.set_video_encryption,
                            serial,
                            bool(call.data.get("enable", False)),
                            str(call.data.get("verification_code", "")),
                        )
                    except Exception as err:  # noqa: BLE001
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="setting_failed",
                            translation_placeholders={"error": str(err)},
                        ) from err
                    return
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_such_doorbell")

    async def fetch_encryption_key(call: ServiceCall) -> None:
        """Get the camera key of a door station and keep it in the options.

        Call once without a code (EZVIZ then e-mails a one-time code), then
        again with the code. Name the door station with ``serial`` when the
        account has more than one: every code belongs to one device.
        """
        code = str(call.data.get("code", "")).strip()
        wanted = str(call.data.get("serial") or "").strip()
        for entry, hub in _hubs():
            if not wanted and len(hub.serials) > 1:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="serial_needed")
            found: dict[str, str] = {}
            for serial in hub.serials:
                if wanted and not (serial == wanted or same_device(serial, wanted)):
                    continue
                try:
                    key = await hass.async_add_executor_job(hub.stream.fetch_camera_key_with_code, serial, code)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Key fetch failed for %s: %s. If EZVIZ just sent a one-time code, call this "
                        "action again with code: \"<that code>\".",
                        serial,
                        err,
                    )
                    continue
                if not key:
                    _LOGGER.warning(
                        "No key returned for %s. Check your mail for a one-time code and call this "
                        "action again with it in the 'code' field.",
                        serial,
                    )
                    continue
                found[serial] = key
            if found:  # one update, so that the entry reloads once
                hass.config_entries.async_update_entry(entry, options=with_keys(entry.options, found, len(hub.serials)))
                hass.config_entries.async_schedule_reload(entry.entry_id)
                _LOGGER.warning("Stored the camera key for %s; the entry reloads and decrypts the stream.", ", ".join(found))

    hass.services.async_register(DOMAIN, "unlock_door", unlock_door, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, "unlock_gate", unlock_gate, SERVICE_SCHEMA)
    async_register_admin_service(
        hass,
        DOMAIN,
        "set_video_encryption",
        set_video_encryption,
        vol.Schema(
            {
                vol.Optional("serial"): cv.string,
                vol.Required("enable"): cv.boolean,
                vol.Required("verification_code"): cv.string,
            }
        ),
    )
    async_register_admin_service(
        hass, DOMAIN, "fetch_encryption_key", fetch_encryption_key, vol.Schema({vol.Optional("code"): cv.string, vol.Optional("serial"): cv.string})
    )
    register_share_views(hass)
    await async_setup_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: InstantBellConfigEntry) -> bool:
    """Set up one EZVIZ account."""
    hub = InstantBellHub(hass, entry)
    await hub.async_start()
    entry.async_on_unload(hub.async_stop)

    coordinator = StatusCoordinator(hass, entry, hub)
    hub.coordinator = coordinator
    if hub.cloud_online:
        await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = hub

    @callback
    def _refresh_soon(_event: object) -> None:
        # A ring or unlock changes the "latest alarm"; do not wait for the next read.
        entry.async_create_background_task(hass, coordinator.async_request_refresh(), f"{DOMAIN}_refresh")

    entry.async_on_unload(async_dispatcher_connect(hass, hub.signal_event, _refresh_soon))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    link_monitors(hass, hub)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: InstantBellConfigEntry) -> bool:
    """Unload the account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: InstantBellConfigEntry) -> None:
    """Remove the card from the dashboard resources with the last account."""
    if not [other for other in hass.config_entries.async_entries(DOMAIN) if other.entry_id != entry.entry_id]:
        await async_remove_card(hass)
