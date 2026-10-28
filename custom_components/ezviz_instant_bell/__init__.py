"""Instant Bell for EZVIZ: ring, picture and door buttons for EZVIZ HP7 / CP7."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .hub import InstantBellHub

PLATFORMS = [Platform.BUTTON, Platform.EVENT, Platform.IMAGE, Platform.SENSOR]

type InstantBellConfigEntry = ConfigEntry[InstantBellHub]


async def async_setup_entry(hass: HomeAssistant, entry: InstantBellConfigEntry) -> bool:
    """Set up one EZVIZ account."""
    hub = InstantBellHub(hass, entry)
    await hub.async_start()
    entry.runtime_data = hub
    entry.async_on_unload(hub.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: InstantBellConfigEntry) -> bool:
    """Unload the account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: InstantBellConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
