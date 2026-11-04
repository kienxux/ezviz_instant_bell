"""Where the camera keys (video decryption) are kept: one per door station.

Pure Python (no Home Assistant imports). The option ``encryption_key`` of the
early versions held one key for everything; it stays as the fallback, and is
still written while the entry has a single door station.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONF_KEY = "encryption_key"
CONF_KEYS = "encryption_keys"


def _bare(serial: str) -> str:
    """``MONITOR-CAMERA`` -> ``MONITOR``: the part a key belongs to."""
    return serial.split("-", 1)[0].upper()


def key_for(options: Mapping[str, Any], serial: str) -> str:
    """The stored key of this door station; the single old key when there is none of its own."""
    keys = options.get(CONF_KEYS)
    if isinstance(keys, Mapping):
        own = str(keys.get(_bare(serial)) or "").strip()
        if own:
            return own
    return str(options.get(CONF_KEY) or "").strip()


def with_keys(options: Mapping[str, Any], found: Mapping[str, str], door_count: int) -> dict[str, Any]:
    """Options with the keys found added (``{serial: key}``). Other options are left alone."""
    merged = dict(options)
    keys = dict(merged.get(CONF_KEYS) or {})
    for serial, key in found.items():
        keys[_bare(serial)] = key
    merged[CONF_KEYS] = keys
    if door_count == 1 and found:
        merged[CONF_KEY] = next(iter(found.values()))
    return merged
