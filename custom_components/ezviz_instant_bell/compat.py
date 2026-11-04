"""Make ``pyezvizapi`` cope with values it does not know.

The library turns some numbers of the device record into enum members, for
example ``SoundMode(alarmSoundMode)``. A device that reports a number the
library has never seen (a CP5 sends 5) makes ``EzvizCamera.status()`` raise
``ValueError`` and the whole status read fails. Every enum of the library that
has an ``UNKNOWN`` member now maps such numbers to it.
"""

from __future__ import annotations

import logging
from enum import Enum

_LOGGER = logging.getLogger(__name__)

_MARK = "_ezviz_instant_bell_tolerant"


def _unknown(cls: type[Enum], value: object) -> Enum:
    _LOGGER.debug("%s has no member for %r, using UNKNOWN", cls.__name__, value)
    return cls.UNKNOWN  # type: ignore[attr-defined]


def tolerate_unknown_enum_values() -> list[str]:
    """Patch the library's enums once. Return the names changed. Call after importing ``pyezvizapi``."""
    import pyezvizapi.constants as constants

    changed: list[str] = []
    for name, cls in vars(constants).items():
        if not (isinstance(cls, type) and issubclass(cls, Enum)) or cls is Enum:
            continue
        if "UNKNOWN" not in cls.__members__ or getattr(cls, _MARK, False):
            continue
        cls._missing_ = classmethod(_unknown)  # type: ignore[assignment]
        type.__setattr__(cls, _MARK, True)
        changed.append(name)
    return changed
