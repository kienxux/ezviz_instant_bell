"""Diagnostics download: what a bug report needs, without secrets.

Account data, serial numbers, addresses, keys and card lists are never
included. Door stations are numbered; of the status only plain values that
carry no personal data are shown, everything else only as its type.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from . import InstantBellConfigEntry
from .const import CONF_MONITOR_SERIALS
from .keys import CONF_KEY, CONF_KEYS

TO_REDACT = {"username", "password", CONF_KEY, CONF_KEYS, CONF_MONITOR_SERIALS, "names", "devices"}

# Status values that say nothing about the people or the network.
SAFE_STATUS = {
    "model", "version", "upgrade_available", "status", "pir_status", "motion",
    "seconds_last_trigger", "last_alarm_time",
}  # fmt: skip


def _status(values: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if k in SAFE_STATUS else f"<{type(v).__name__}>") for k, v in sorted(values.items())}


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: InstantBellConfigEntry) -> dict[str, Any]:
    hub = entry.runtime_data
    coordinator = hub.coordinator
    data = (coordinator.data if coordinator is not None else None) or {}
    doors = {}
    for number, serial in enumerate(hub.serials, start=1):
        relay = hub.live_relays.get(serial)
        doors[f"door_{number}"] = {
            "monitors": len(hub.monitors.get(serial, [])),
            "stream_mode": hub.stream_modes.get(serial),
            "stream_source": getattr(relay, "active_source", None),
            "video_codec": getattr(relay, "detected_codec", None),
            "status": _status(data.get(serial, {})),
        }
    return {
        "versions": {"pyezvizapi": _version("pyezvizapi"), "home_assistant": HA_VERSION},
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "push_connected": hub.push_connected,
        "cloud_online": hub.cloud_online,
        "stored_for_local_use": {f"door_{n}": sorted(hub.stream.remembered.get(s.split("-")[0].upper(), {})) for n, s in enumerate(hub.serials, start=1)},
        "last_update_ok": coordinator.last_update_success if coordinator is not None else None,
        "doors": doors,
    }
