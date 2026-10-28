"""Thin wrapper around ``pyezvizapi`` used by the config flow and the hub.

All functions here block and must run in the executor.
"""

from __future__ import annotations

from typing import Any

from .feature_code import apply_to_library


class CannotConnect(Exception):
    """EZVIZ could not be reached."""


class InvalidAuth(Exception):
    """Wrong account, password or verification code."""


class NeedsCode(Exception):
    """EZVIZ sent a verification code that must be entered."""

    def __init__(self, client: Any) -> None:
        super().__init__("verification code required")
        self.client = client


def _classify(err: Exception) -> Exception:
    text = str(err).lower()
    if any(w in text for w in ("incorrect", "locked", "invalid", "password")):
        return InvalidAuth(str(err))
    return CannotConnect(str(err))


def login(
    feature_code: str,
    username: str,
    password: str,
    region: str,
    *,
    client: Any = None,
    sms_code: int | None = None,
) -> Any:
    """Log in and return the client. Raises InvalidAuth, NeedsCode or CannotConnect.

    Pass the ``client`` carried by NeedsCode together with ``sms_code`` to finish
    a login that asked for a verification code.
    """
    import pyezvizapi  # noqa: F401  (loads the submodules before patching)
    from pyezvizapi.client import EzvizClient
    from pyezvizapi.exceptions import (
        EzvizAuthVerificationCode,
        HTTPError,
        InvalidURL,
        PyEzvizError,
    )

    apply_to_library(feature_code)
    if client is None:
        client = EzvizClient(username, password, region)
    try:
        client.login(sms_code=sms_code)
    except EzvizAuthVerificationCode as err:
        raise NeedsCode(client) from err
    except (HTTPError, InvalidURL) as err:
        raise CannotConnect(str(err) or type(err).__name__) from err
    except PyEzvizError as err:
        raise _classify(err) from err
    return client


def list_devices(client: Any) -> dict[str, dict[str, str]]:
    """Return ``{serial: {"name": ..., "category": ...}}`` for the account."""
    try:
        infos = client.get_device_infos()
    except Exception as err:  # noqa: BLE001
        raise CannotConnect(str(err)) from err
    devices: dict[str, dict[str, str]] = {}
    for serial, info in (infos or {}).items():
        details = info.get("deviceInfos", {}) if isinstance(info, dict) else {}
        devices[str(serial)] = {
            "name": str(details.get("name") or serial),
            "category": str(details.get("deviceCategory") or ""),
        }
    return devices


def terminal_names(client: Any) -> list[str]:
    """Names of the terminals registered on the account."""
    answer = client.get_terminals(limit=50)
    items = answer.get("terminals", []) if isinstance(answer, dict) else []
    return [str(t.get("name") or "?") for t in items if isinstance(t, dict)]
