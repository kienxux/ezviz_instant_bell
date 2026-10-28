"""Stable EZVIZ terminal ID ("featureCode").

EZVIZ registers every distinct featureCode as a separate terminal of the
account and allows only 10. ``pyezvizapi`` derives it from the MAC address of
the machine, which changes in Docker bridge networks, so every restart can
register a new terminal. This module creates one random code, stores it on
disk and applies it to ``pyezvizapi`` before the first login.

Nothing here imports Home Assistant, so it can be tested on its own.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from collections.abc import Mapping, MutableMapping
from hashlib import md5
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

FEATURE_CODE_FILENAME = "ezviz_instant_bell_feature_code"
ENV_VAR = "EZVIZ_INSTANT_BELL_FEATURE_CODE"

_VALID = re.compile(r"^[0-9a-f]{32}$")

# Modules of pyezvizapi that import FEATURE_CODE by name at import time.
_LIBRARY_MODULES = (
    "pyezvizapi.constants",
    "pyezvizapi.client",
    "pyezvizapi.cas",
    "pyezvizapi.mqtt",
)


def valid_code(value: object) -> str | None:
    """Return the code in normalised form if it is 32 hex characters, else None."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _VALID.match(candidate) else None


def generate_code() -> str:
    """Create a new random 32-character hex code."""
    return md5(uuid.uuid4().bytes).hexdigest()  # noqa: S324 - not used for security


def _read(path: Path) -> str | None:
    try:
        return valid_code(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _write(path: Path, code: str) -> bool:
    """Write the code atomically. Return True on success."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(code, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as err:
        _LOGGER.warning("Could not store the EZVIZ terminal ID in %s: %s", path, err)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def resolve_feature_code(
    storage_dir: Path | str, environ: Mapping[str, str] | None = None
) -> str:
    """Return the terminal ID to use.

    Order: environment variable, stored file, newly generated (and stored).
    If nothing can be written the new code lives only until restart, and a
    warning says so.
    """
    env = os.environ if environ is None else environ
    forced = valid_code(env.get(ENV_VAR))
    if forced:
        return forced

    path = Path(storage_dir) / FEATURE_CODE_FILENAME
    stored = _read(path)
    if stored:
        return stored

    code = generate_code()
    if not _write(path, code):
        _LOGGER.warning(
            "The EZVIZ terminal ID is not stored and will change on restart; "
            "check that %s is writable",
            path.parent,
        )
    return code


def apply_to_library(
    code: str, modules: MutableMapping[str, object] | None = None
) -> list[str]:
    """Make ``pyezvizapi`` use ``code``. Return the names of the modules changed.

    Call this after importing ``pyezvizapi`` and before creating a client.
    """
    loaded = sys.modules if modules is None else modules
    changed: list[str] = []
    for name in _LIBRARY_MODULES:
        module = loaded.get(name)
        if module is not None and hasattr(module, "FEATURE_CODE"):
            module.FEATURE_CODE = code
            changed.append(name)
    constants = loaded.get("pyezvizapi.constants")
    header = getattr(constants, "REQUEST_HEADER", None)
    if isinstance(header, dict):
        header["featureCode"] = code
    return changed
