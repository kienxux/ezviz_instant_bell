"""Repair flow for the encryption code of the live video.

On firmware that keeps Image/Video Encryption on with no switch in the app, the
cloud refuses to hand the decryption key to a session that has not been
elevated, and answers 20002. That refusal makes EZVIZ e-mail a 4-digit code
(subject "[Device Encryption] Security Code") which is good for about 30
minutes, so the person needs a form in front of them, not a log line.

Adapted from the ``ezviz_hp7`` integration by Bobsilvio (MIT).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import CONF_ENCRYPTION_KEY, DOMAIN
from .hub import InstantBellHub

_LOGGER = logging.getLogger(__name__)


class EncryptionOtpRepairFlow(RepairsFlow):
    """Ask for the code EZVIZ mailed, then keep the key it unlocks."""

    def __init__(self, serial: str) -> None:
        self._serial = serial

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Exchange the code for the key and store it in the options.

        An empty code means the last one expired: ask the cloud again (it mails
        a fresh one) and show the form again.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            code = str(user_input.get("code", "")).strip()
            entry, hub = _resolve(self.hass, self._serial)
            if hub is None or entry is None:
                return self.async_abort(reason="no_device")

            if not code:
                await self.hass.async_add_executor_job(hub.stream.get_camera_encryption_key, self._serial)
                errors["code"] = "resent"
            else:
                try:
                    key = await self.hass.async_add_executor_job(
                        hub.stream.fetch_camera_key_with_code, self._serial, code
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Key exchange failed for %s: %s", self._serial, exc)
                    key = None
                if key:
                    self.hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_ENCRYPTION_KEY: key})
                    _LOGGER.warning("Stored the camera key for %s; the entry reloads and decrypts the stream.", self._serial)
                    return self.async_create_entry(title="", data={})
                errors["code"] = "invalid_code"

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Optional("code", default=""): str}),
            errors=errors,
            description_placeholders={"serial": self._serial},
        )


def _resolve(hass: HomeAssistant, serial: str) -> tuple[Any, InstantBellHub | None]:
    """The config entry and hub that own this serial."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        hub = getattr(entry, "runtime_data", None)
        if isinstance(hub, InstantBellHub) and serial in hub.serials:
            return entry, hub
    return None, None


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    serial = str((data or {}).get("serial") or "")
    if not serial and issue_id.startswith("encryption_otp_"):
        serial = issue_id[len("encryption_otp_"):]
    return EncryptionOtpRepairFlow(serial)
