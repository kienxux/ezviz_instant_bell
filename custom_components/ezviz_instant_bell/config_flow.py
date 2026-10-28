"""Config flow: log in, answer a verification code if asked, pick doorbells."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import ezviz
from .const import (
    CONF_CREATE_BUTTONS,
    CONF_DEVICES,
    CONF_DOOR_LOCK_NO,
    CONF_GATE_LOCK_NO,
    CONF_NAMES,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    DEFAULT_DOOR_LOCK_NO,
    DEFAULT_GATE_LOCK_NO,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    REGIONS,
)
from .feature_code import resolve_feature_code

_LOGGER = logging.getLogger(__name__)

# Device categories that are the door station of an HP7 / CP7 style intercom.
DOORBELL_CATEGORIES = {"BDoorBell"}


class InstantBellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one EZVIZ account."""

    VERSION = 1

    def __init__(self) -> None:
        self._username = ""
        self._password = ""
        self._region = DEFAULT_REGION
        self._client: Any = None
        self._devices: dict[str, dict[str, str]] = {}

    async def _async_login(self, sms_code: int | None = None) -> None:
        """Log in; raises the ezviz exceptions."""
        code = await self.hass.async_add_executor_job(
            resolve_feature_code, Path(self.hass.config.path(".storage"))
        )
        self._client = await self.hass.async_add_executor_job(
            lambda: ezviz.login(
                code,
                self._username,
                self._password,
                self._region,
                client=self._client,
                sms_code=sms_code,
            )
        )

    # -- user ---------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input["username"].strip()
            self._password = user_input["password"]
            self._region = user_input[CONF_REGION]
            self._client = None
            await self.async_set_unique_id(self._username.lower())
            self._abort_if_unique_id_configured()
            try:
                await self._async_login()
            except ezviz.NeedsCode as err:
                self._client = err.client
                return await self.async_step_code()
            except ezviz.InvalidAuth:
                errors["base"] = "invalid_auth"
            except ezviz.CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_after_login()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("username", default=self._username): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
                    ),
                    vol.Required("password"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                    ),
                    vol.Required(CONF_REGION, default=self._region): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=k, label=v) for k, v in REGIONS.items()],
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # -- verification code --------------------------------------------

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._async_login(sms_code=int(user_input["code"]))
            except (ValueError, ezviz.InvalidAuth):
                errors["base"] = "invalid_code"
            except ezviz.NeedsCode:
                errors["base"] = "invalid_code"
            except ezviz.CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_after_login()
        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required("code"): TextSelector()}),
            errors=errors,
        )

    async def _async_after_login(self) -> ConfigFlowResult:
        if self.source == "reauth":
            return self._finish_reauth()
        try:
            self._devices = await self.hass.async_add_executor_job(ezviz.list_devices, self._client)
        except ezviz.CannotConnect:
            return self.async_abort(reason="cannot_connect")
        if not self._devices:
            return self.async_abort(reason="no_devices")
        return await self.async_step_devices()

    # -- devices ------------------------------------------------------

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            chosen = user_input[CONF_DEVICES]
            return self.async_create_entry(
                title=self._username,
                data={
                    "username": self._username,
                    "password": self._password,
                    CONF_REGION: self._region,
                    CONF_DEVICES: chosen,
                    CONF_NAMES: {s: self._devices[s]["name"] for s in chosen},
                },
            )
        options = [
            SelectOptionDict(
                value=serial,
                label=f"{info['name']} ({info['category'] or 'device'}, {serial})",
            )
            for serial, info in self._devices.items()
        ]
        default = [s for s, i in self._devices.items() if i["category"] in DOORBELL_CATEGORIES]
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICES, default=default): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    # -- reauth -------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._username = entry_data["username"]
        self._region = entry_data.get(CONF_REGION, DEFAULT_REGION)
        self._client = None
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input["password"]
            self._client = None
            try:
                await self._async_login()
            except ezviz.NeedsCode as err:
                self._client = err.client
                return await self.async_step_code()
            except ezviz.InvalidAuth:
                errors["base"] = "invalid_auth"
            except ezviz.CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self._finish_reauth()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("password"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                    )
                }
            ),
            description_placeholders={"username": self._username},
            errors=errors,
        )

    def _finish_reauth(self) -> ConfigFlowResult:
        return self.async_update_reload_and_abort(
            self._get_reauth_entry(), data_updates={"password": self._password}
        )

    # -- options ------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        return InstantBellOptionsFlow()


class InstantBellOptionsFlow(OptionsFlow):
    """Polling interval and lock numbers."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                    CONF_CREATE_BUTTONS: bool(user_input[CONF_CREATE_BUTTONS]),
                    CONF_DOOR_LOCK_NO: int(user_input[CONF_DOOR_LOCK_NO]),
                    CONF_GATE_LOCK_NO: int(user_input[CONF_GATE_LOCK_NO]),
                }
            )
        opts = self.config_entry.options
        number = lambda low, high: NumberSelector(  # noqa: E731
            NumberSelectorConfig(min=low, max=high, step=1, mode=NumberSelectorMode.BOX)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL, default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
                    ): number(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL),
                    vol.Required(
                        CONF_CREATE_BUTTONS, default=opts.get(CONF_CREATE_BUTTONS, False)
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_DOOR_LOCK_NO, default=opts.get(CONF_DOOR_LOCK_NO, DEFAULT_DOOR_LOCK_NO)
                    ): number(1, 8),
                    vol.Required(
                        CONF_GATE_LOCK_NO, default=opts.get(CONF_GATE_LOCK_NO, DEFAULT_GATE_LOCK_NO)
                    ): number(1, 8),
                }
            ),
        )
