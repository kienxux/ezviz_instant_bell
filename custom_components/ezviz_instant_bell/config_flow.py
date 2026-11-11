"""Config flow: log in, answer a verification code if asked, pick doorbells."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
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
    CONF_SAVE_CLIPS,
    CONF_NEW_STREAM_TOKEN,
    CONF_SHARE_STREAM,
    CONF_STREAM_TOKEN,
    CONF_DETECTION_PICTURE_DAYS,
    CONF_SAVE_DETECTION_PICTURES,
    CONF_MOTION_EVENTS,
    CONF_PERSON_EVENTS,
    DEFAULT_DETECTION_PICTURE_DAYS,
    MAX_DETECTION_PICTURE_DAYS,
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
    DOORBELL_CATEGORIES,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    REGIONS,
    STREAM_MODES,
    STREAM_QUALITIES,
    STREAM_SOURCES,
    VIDEO_CODECS,
)
from .feature_code import resolve_feature_code
from .keys import CONF_KEYS, kept_keys
from .stream_share import new_token, stream_addresses

_LOGGER = logging.getLogger(__name__)


class InstantBellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one EZVIZ account."""

    VERSION = 1

    def __init__(self) -> None:
        self._username = ""
        self._password = ""
        self._region = DEFAULT_REGION
        self._client: Any = None
        self._devices: dict[str, dict[str, str]] = {}
        self._preselect: list[str] = []

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
            if self.source == SOURCE_RECONFIGURE:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data_updates={
                        "password": self._password,
                        CONF_REGION: self._region,
                        CONF_DEVICES: chosen,
                        CONF_NAMES: {s: self._devices[s]["name"] for s in chosen},
                    },
                )
            return self.async_create_entry(
                title=self._username,
                data={
                    "username": self._username,
                    "password": self._password,
                    CONF_REGION: self._region,
                    CONF_DEVICES: chosen,
                    CONF_NAMES: {s: self._devices[s]["name"] for s in chosen},
                },
                options={},
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

    # -- reconfigure --------------------------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the region, the password or the chosen doorbells of an account that is set up."""
        entry = self._get_reconfigure_entry()
        self._username = entry.data["username"]
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = str(user_input.get("password") or "") or entry.data["password"]
            self._region = user_input[CONF_REGION]
            self._preselect = list(entry.data.get(CONF_DEVICES, []))
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
                return await self._async_after_login()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Optional("password"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                    ),
                    vol.Required(CONF_REGION, default=entry.data.get(CONF_REGION, DEFAULT_REGION)): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=k, label=v) for k, v in REGIONS.items()],
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            description_placeholders={"username": self._username},
            errors=errors,
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
    def async_get_options_flow(config_entry: Any) -> OptionsFlowWithReload:
        return InstantBellOptionsFlow()


# The options form groups its fields in these sections; the options are kept flat.
SECTION_EVENTS = "events"
SECTION_LIVE_VIDEO = "live_video"
SECTION_OTHER_APPS = "other_apps"
SECTION_TROUBLESHOOTING = "troubleshooting"
OPTION_SECTIONS = (SECTION_EVENTS, SECTION_LIVE_VIDEO, SECTION_OTHER_APPS, SECTION_TROUBLESHOOTING)


class InstantBellOptionsFlow(OptionsFlowWithReload):
    """Ring check interval, indoor monitors, lock numbers and live video."""

    _token = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # The addresses in the form already show the token that is saved with it.
        if not self._token:
            self._token = self.config_entry.options.get(CONF_STREAM_TOKEN) or new_token()
        if user_input is not None:
            # Each section arrives as a dictionary of its own.
            sections = [user_input.get(name) or {} for name in OPTION_SECTIONS]
            user_input = {k: v for k, v in user_input.items() if k not in OPTION_SECTIONS}
            for values in sections:
                user_input.update(values)
            data: dict[str, Any] = {
                CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                CONF_MONITOR_SERIALS: str(user_input.get(CONF_MONITOR_SERIALS) or "").strip(),
                CONF_DOOR_LOCK_NO: int(user_input[CONF_DOOR_LOCK_NO]),
                CONF_GATE_LOCK_NO: int(user_input[CONF_GATE_LOCK_NO]),
                CONF_SAVE_CLIPS: bool(user_input.get(CONF_SAVE_CLIPS, False)),
                CONF_SAVE_DETECTION_PICTURES: bool(user_input.get(CONF_SAVE_DETECTION_PICTURES, False)),
                CONF_DETECTION_PICTURE_DAYS: int(
                    user_input.get(CONF_DETECTION_PICTURE_DAYS) or DEFAULT_DETECTION_PICTURE_DAYS
                ),
                CONF_MOTION_EVENTS: bool(user_input.get(CONF_MOTION_EVENTS, False)),
                CONF_PERSON_EVENTS: bool(user_input.get(CONF_PERSON_EVENTS, False)),
                CONF_RELAY_PORT: int(user_input.get(CONF_RELAY_PORT) or 0),
                CONF_RELAY_BIND: str(user_input.get(CONF_RELAY_BIND) or DEFAULT_RELAY_BIND).strip(),
                CONF_SHARE_STREAM: bool(user_input.get(CONF_SHARE_STREAM, False)),
                CONF_STREAM_TOKEN: new_token() if user_input.get(CONF_NEW_STREAM_TOKEN) else self._token,
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
            data[CONF_KEYS] = kept_keys(self.config_entry.options, data[CONF_ENCRYPTION_KEY])
            return self.async_create_entry(data=data)
        opts = self.config_entry.options
        serials = list(getattr(getattr(self.config_entry, "runtime_data", None), "serials", None) or [])
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
                    vol.Required(SECTION_EVENTS): section(
                        vol.Schema(
                            {
                                vol.Required(CONF_SAVE_CLIPS, default=bool(opts.get(CONF_SAVE_CLIPS, False))): bool,
                                vol.Required(
                                    CONF_SAVE_DETECTION_PICTURES,
                                    default=bool(opts.get(CONF_SAVE_DETECTION_PICTURES, False)),
                                ): bool,
                                vol.Required(
                                    CONF_DETECTION_PICTURE_DAYS,
                                    default=int(opts.get(CONF_DETECTION_PICTURE_DAYS, DEFAULT_DETECTION_PICTURE_DAYS)),
                                ): number(1, MAX_DETECTION_PICTURE_DAYS),
                                vol.Required(
                                    CONF_MOTION_EVENTS, default=bool(opts.get(CONF_MOTION_EVENTS, False))
                                ): bool,
                                vol.Required(
                                    CONF_PERSON_EVENTS, default=bool(opts.get(CONF_PERSON_EVENTS, False))
                                ): bool,
                            }
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required(SECTION_LIVE_VIDEO): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_STREAM_SOURCE, default=opts.get(CONF_STREAM_SOURCE, "cloud")
                                ): choice(STREAM_SOURCES),
                                vol.Required(CONF_STREAM_MODE, default=opts.get(CONF_STREAM_MODE, "auto")): choice(
                                    STREAM_MODES
                                ),
                                vol.Required(CONF_VIDEO_CODEC, default=opts.get(CONF_VIDEO_CODEC, "auto")): choice(
                                    VIDEO_CODECS
                                ),
                                vol.Required(
                                    CONF_STREAM_QUALITY, default=opts.get(CONF_STREAM_QUALITY, "main")
                                ): choice(STREAM_QUALITIES),
                                vol.Optional(
                                    CONF_ENCRYPTION_KEY,
                                    description={"suggested_value": opts.get(CONF_ENCRYPTION_KEY, "")},
                                ): TextSelector(),
                            }
                        ),
                        {"collapsed": True},
                    ),
                    vol.Required(SECTION_OTHER_APPS): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_SHARE_STREAM, default=bool(opts.get(CONF_SHARE_STREAM, False))
                                ): bool,
                                vol.Required(CONF_NEW_STREAM_TOKEN, default=False): bool,
                                vol.Required(
                                    CONF_RELAY_PORT, default=int(opts.get(CONF_RELAY_PORT) or 0)
                                ): number(0, 65535),
                                vol.Required(
                                    CONF_RELAY_BIND, default=opts.get(CONF_RELAY_BIND, DEFAULT_RELAY_BIND)
                                ): TextSelector(),
                            }
                        ),
                        {"collapsed": True},
                    ),
                    vol.Required(SECTION_TROUBLESHOOTING): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_AGGRESSIVE_MPEGTS, default=bool(opts.get(CONF_AGGRESSIVE_MPEGTS, False))
                                ): bool,
                                vol.Required(
                                    CONF_DIAGNOSTIC_DUMPS, default=bool(opts.get(CONF_DIAGNOSTIC_DUMPS, False))
                                ): bool,
                            }
                        ),
                        {"collapsed": True},
                    ),
                }
            ),
            description_placeholders={"stream_addresses": stream_addresses(self.hass, serials, self._token)},
        )
