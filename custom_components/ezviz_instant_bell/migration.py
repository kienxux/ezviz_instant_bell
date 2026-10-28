"""Take over the entities of the earlier ``ezviz_hp7`` integration.

Entities keep their entity ID when their registry entry is moved to this
integration, so dashboards, automations and phone widgets go on working. This
only works while the old integration is not running: a running one owns the
entities' states.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from .const import DOMAIN, LEGACY_DOMAIN

_LOGGER = logging.getLogger(__name__)

# Nothing is left behind: the live video entity moves over as well.
NOT_MOVED_SUFFIXES: tuple[str, ...] = ()

_RUNNING = {ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.SETUP_RETRY}


def _belongs_to(unique_id: str, serials: set[str]) -> bool:
    return any(unique_id.startswith(f"{LEGACY_DOMAIN}_{serial}_") for serial in serials)


def async_migrate_legacy(hass: HomeAssistant, entry: ConfigEntry, serials: set[str]) -> tuple[int, bool]:
    """Move the old entities of ``serials`` to ``entry``.

    Returns ``(moved, blocked)``. ``blocked`` is True when old entities exist but
    the old integration is still running, so nothing could be moved yet.
    """
    legacy_entries = hass.config_entries.async_entries(LEGACY_DOMAIN)
    if not legacy_entries or not serials:
        return 0, False

    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    legacy_ids = {e.entry_id for e in legacy_entries}

    candidates = [
        item
        for item in registry.entities.values()
        if item.platform == LEGACY_DOMAIN
        and item.config_entry_id in legacy_ids
        and _belongs_to(item.unique_id, serials)
        and not (NOT_MOVED_SUFFIXES and item.unique_id.endswith(NOT_MOVED_SUFFIXES))
    ]
    if not candidates:
        return 0, False

    if any(e.state in _RUNNING for e in legacy_entries):
        return 0, True

    # Devices first, so that area and renames survive. A device keeps its ID
    # when it can be re-labelled; if this integration already made one for the
    # same serial, the entities go to that one.
    target_device: dict[str, str] = {}
    for item in candidates:
        legacy_device_id = item.device_id
        if not legacy_device_id or legacy_device_id in target_device:
            continue
        device = devices.async_get(legacy_device_id)
        if device is None:
            continue
        new_ids = {(DOMAIN, ident) for domain, ident in device.identifiers if domain == LEGACY_DOMAIN}
        if not new_ids:
            continue
        existing = devices.async_get_device(identifiers=new_ids)
        if existing is not None and existing.id != device.id:
            devices.async_update_device(existing.id, add_config_entry_id=entry.entry_id)
            target_device[legacy_device_id] = existing.id
            continue
        devices.async_update_device(
            device.id,
            add_config_entry_id=entry.entry_id,
            new_identifiers={i for i in device.identifiers if i[0] != LEGACY_DOMAIN} | new_ids,
        )
        target_device[legacy_device_id] = device.id

    moved = 0
    for item in candidates:
        # If this integration already made an entity for the same thing (it ran
        # next to the old one), drop that one; the old one has the entity ID
        # the dashboards use.
        duplicate = registry.async_get_entity_id(item.domain, DOMAIN, item.unique_id)
        if duplicate:
            if hass.states.get(duplicate) is not None:
                hass.states.async_remove(duplicate)
            registry.async_remove(duplicate)
        if hass.states.get(item.entity_id) is not None:
            # The old integration left a placeholder state; the registry only
            # moves entities that are not loaded.
            hass.states.async_remove(item.entity_id)
        try:
            registry.async_update_entity_platform(
                item.entity_id,
                DOMAIN,
                new_config_entry_id=entry.entry_id,
                new_device_id=target_device.get(item.device_id or "", item.device_id),
            )
        except ValueError as err:
            _LOGGER.warning("Could not move %s: %s", item.entity_id, err)
            continue
        moved += 1
        if item.disabled_by == RegistryEntryDisabler.CONFIG_ENTRY:
            # The old setup was switched off; that must not switch these off.
            registry.async_update_entity(item.entity_id, disabled_by=None)

    # Let go of the old setup on the devices only now: removing it earlier makes
    # the registry drop the entities that still belong to it. Devices that still
    # carry an entity of the old setup (the live video) keep it.
    for legacy_device_id in set(target_device):
        device = devices.async_get(legacy_device_id)
        if device is None:
            continue
        still_used = {
            e.config_entry_id
            for e in er.async_entries_for_device(registry, device.id, include_disabled_entities=True)
        }
        for legacy_id in (legacy_ids & set(device.config_entries)) - still_used:
            devices.async_update_device(device.id, remove_config_entry_id=legacy_id)

    if moved:
        _LOGGER.info("Took over %s entities from the earlier %s integration", moved, LEGACY_DOMAIN)
    return moved, False
