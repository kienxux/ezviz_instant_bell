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
    CONF_AGGRESSIVE_MPEGTS,
    CONF_CREATE_BUTTONS,
    CONF_DEVICES,
    CONF_DIAGNOSTIC_DUMPS,
    CONF_ENCRYPTION_KEY,
    CONF_DOOR_LOCK_NO,
    CONF_GATE_LOCK_NO,
    CONF_MONITOR_SERIALS,
    CONF_NAMES,
    CONF_POLL_INTERVAL,
    CONF_RELAY_BIND,
    CONF_RELAY_PORT,
    CONF_REGION,
    CONF_STREAM_MODE,
    CONF_STREAM_QUALITY,
    CONF_STREAM_SOURCE,
    CONF_VIDEO_CODEC,
    DEFAULT_DOOR_LOCK_NO,
    DEFAULT_GATE_LOCK_NO,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DEFAULT_RELAY_BIND,
    DOMAIN,
    LEGACY_DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    REGIONS,
    STREAM_MODES,
    STREAM_QUALITIES,
    STREAM_SOURCES,
    VIDEO_CODECS,
)
from .feature_code import resolve_feature_code

_LOGGER = logging.getLogger(__name__)

# Device categories that are the door station of an HP7 / CP7 style intercom.
DOORBELL_CATEGORIES = {"BDoorBell"}

# Live video options with the same names in the earlier ezviz_hp7 integration.
LIVE_OPTION_KEYS = (
    CONF_RELAY_PORT,
    CONF_RELAY_BIND,
    CONF_AGGRESSIVE_MPEGTS,
    CONF_VIDEO_CODEC,
    CONF_STREAM_SOURCE,
    CONF_STREAM_MODE,
    CONF_STREAM_QUALITY,
    CONF_ENCRYPTION_KEY,
    CONF_DIAGNOSTIC_DUMPS,
)

# Region names of the earlier ezviz_hp7 integration and the hosts they meant.
LEGACY_REGIONS = {
    "eu": "apiieu.ezvizlife.com",
    "us": "apiisa.ezvizlife.com",
    "cn": "apiicn.ezvizlife.com",
    "as": "apiias.ezvizlife.com",
    "sa": "apiisa.ezvizlife.com",
    "ru": "apirus.ezvizru.com",
}


class InstantBellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one EZVIZ account."""

    VERSION = 1

    def __init__(self) -> None:
        self._username = ""
        self._password = ""
        self._region = DEFAULT_REGION
        self._client: Any = None
        self._devices: dict[str, dict[str, str]] = {}
        self._asked = False  # the "start from the old setup?" menu was shown
        self._preselect: list[str] = []
        self._monitors = ""
        self._live_options: dict[str, Any] = {}

    def _legacy_entries(self) -> list[Any]:
        return self.hass.config_entries.async_entries(LEGACY_DOMAIN)

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
        if user_input is None and not self._asked and self._legacy_entries():
            self._asked = True
            return self.async_show_menu(step_id="user", menu_options=["legacy", "manual"])
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

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Enter the login by hand even though an old setup exists."""
        self._asked = True
        return await self.async_step_user()

    async def async_step_legacy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take the login, doorbell and monitor from the earlier ezviz_hp7 setup."""
        entries = self._legacy_entries()
        if not entries:
            return await self.async_step_manual()
        legacy = entries[0]
        data = legacy.data
        self._username = str(data.get("username") or "").strip()
        self._password = str(data.get("password") or "")
        region = str(data.get(CONF_REGION) or "eu")
        self._region = LEGACY_REGIONS.get(region, region)
        self._preselect = [str(e.data["serial"]) for e in entries if e.data.get("serial")]
        monitors = legacy.options.get("monitor_serial") or ""
        if isinstance(monitors, (list, tuple)):
            monitors = ",".join(str(m) for m in monitors)
        self._monitors = str(monitors).strip()
        self._live_options = {k: legacy.options[k] for k in LIVE_OPTION_KEYS if k in legacy.options}
        if not self._username or not self._password:
            return await self.async_step_manual()
        await self.async_set_unique_id(self._username.lower())
        self._abort_if_unique_id_configured()
        self._client = None
        try:
            await self._async_login()
        except ezviz.NeedsCode as err:
            self._client = err.client
            return await self.async_step_code()
        except ezviz.InvalidAuth:
            self._asked = True
            return await self.async_step_user()
        except ezviz.CannotConnect:
            return self.async_abort(reason="cannot_connect")
        return await self._async_after_login()

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
                options={
                    **self._live_options,
                    **({CONF_MONITOR_SERIALS: self._monitors} if self._monitors else {}),
                },
            )
        options = [
            SelectOptionDict(
                value=serial,
                label=f"{info['name']} ({info['category'] or 'device'}, {serial})",
            )
            for serial, info in self._devices.items()
        ]
        default = [s for s in self._preselect if s in self._devices] or [
            s for s, i in self._devices.items() if i["category"] in DOORBELL_CATEGORIES
        ]
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
    """Ring check interval, indoor monitors, lock numbers and live video."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data: dict[str, Any] = {
                CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                CONF_MONITOR_SERIALS: str(user_input.get(CONF_MONITOR_SERIALS) or "").strip(),
                CONF_DOOR_LOCK_NO: int(user_input[CONF_DOOR_LOCK_NO]),
                CONF_GATE_LOCK_NO: int(user_input[CONF_GATE_LOCK_NO]),
                CONF_RELAY_PORT: int(user_input.get(CONF_RELAY_PORT) or 0),
                CONF_RELAY_BIND: str(user_input.get(CONF_RELAY_BIND) or DEFAULT_RELAY_BIND).strip(),
                CONF_AGGRESSIVE_MPEGTS: bool(user_input.get(CONF_AGGRESSIVE_MPEGTS, False)),
                CONF_VIDEO_CODEC: user_input[CONF_VIDEO_CODEC],
                CONF_STREAM_SOURCE: user_input[CONF_STREAM_SOURCE],
                CONF_STREAM_MODE: user_input[CONF_STREAM_MODE],
                CONF_STREAM_QUALITY: user_input[CONF_STREAM_QUALITY],
                CONF_ENCRYPTION_KEY: str(user_input.get(CONF_ENCRYPTION_KEY) or "").strip(),
                CONF_DIAGNOSTIC_DUMPS: bool(user_input.get(CONF_DIAGNOSTIC_DUMPS, False)),
            }
            if self.config_entry.options.get(CONF_CREATE_BUTTONS):
                data[CONF_CREATE_BUTTONS] = True  # keeps the 0.1.0 buttons
            return self.async_create_entry(data=data)
        opts = self.config_entry.options
        number = lambda low, high: NumberSelector(  # noqa: E731
            NumberSelectorConfig(min=low, max=high, step=1, mode=NumberSelectorMode.BOX)
        )

        def choice(values: list[str]) -> SelectSelector:
            return SelectSelector(SelectSelectorConfig(options=values, mode=SelectSelectorMode.DROPDOWN))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL, default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
                    ): number(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL),
                    vol.Optional(
                        CONF_MONITOR_SERIALS, description={"suggested_value": opts.get(CONF_MONITOR_SERIALS, "")}
                    ): TextSelector(),
                    vol.Required(
                        CONF_DOOR_LOCK_NO, default=opts.get(CONF_DOOR_LOCK_NO, DEFAULT_DOOR_LOCK_NO)
                    ): number(1, 8),
                    vol.Required(
                        CONF_GATE_LOCK_NO, default=opts.get(CONF_GATE_LOCK_NO, DEFAULT_GATE_LOCK_NO)
                    ): number(1, 8),
                    vol.Required(CONF_STREAM_SOURCE, default=opts.get(CONF_STREAM_SOURCE, "cloud")): choice(
                        STREAM_SOURCES
                    ),
                    vol.Required(CONF_STREAM_MODE, default=opts.get(CONF_STREAM_MODE, "auto")): choice(
                        STREAM_MODES
                    ),
                    vol.Required(CONF_VIDEO_CODEC, default=opts.get(CONF_VIDEO_CODEC, "auto")): choice(
                        VIDEO_CODECS
                    ),
                    vol.Required(CONF_STREAM_QUALITY, default=opts.get(CONF_STREAM_QUALITY, "main")): choice(
                        STREAM_QUALITIES
                    ),
                    vol.Required(CONF_RELAY_PORT, default=int(opts.get(CONF_RELAY_PORT) or 0)): number(0, 65535),
                    vol.Required(CONF_RELAY_BIND, default=opts.get(CONF_RELAY_BIND, DEFAULT_RELAY_BIND)): TextSelector(),
                    vol.Required(
                        CONF_AGGRESSIVE_MPEGTS, default=bool(opts.get(CONF_AGGRESSIVE_MPEGTS, False))
                    ): bool,
                    vol.Optional(
                        CONF_ENCRYPTION_KEY, description={"suggested_value": opts.get(CONF_ENCRYPTION_KEY, "")}
                    ): TextSelector(),
                    vol.Required(
                        CONF_DIAGNOSTIC_DUMPS, default=bool(opts.get(CONF_DIAGNOSTIC_DUMPS, False))
                    ): bool,
                }
            ),
        )
