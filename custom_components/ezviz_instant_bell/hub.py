"""Connection to the EZVIZ cloud: push, polling, pictures and unlocking."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICES,
    CONF_DOOR_LOCK_NO,
    CONF_GATE_LOCK_NO,
    CONF_MONITOR_SERIALS,
    CONF_NAMES,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    DEFAULT_DOOR_LOCK_NO,
    DEFAULT_GATE_LOCK_NO,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DOMAIN,
    PICTURE_ATTEMPTS,
    PICTURE_RETRY_DELAY,
    SIGNAL_EVENT,
    SIGNAL_PICTURE,
    UNLOCK_COOLDOWN,
)
from . import ezviz, state
from .device_api import DeviceApi
from .stream_support import StreamSupport
from .feature_code import resolve_feature_code
from .parsing import (
    BellEvent,
    EventDeduper,
    NewMessageTracker,
    event_from_message,
    event_from_push,
    is_stale,
    messages_from_answer,
    same_device,
)

_LOGGER = logging.getLogger(__name__)

POLL_WARN_AFTER = 5  # consecutive poll failures before a warning is logged


class InstantBellHub:
    """Owns the EZVIZ client for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.serials: list[str] = list(entry.data.get(CONF_DEVICES, []))
        self.names: dict[str, str] = dict(entry.data.get(CONF_NAMES, {}))
        self.signal_event = SIGNAL_EVENT.format(entry_id=entry.entry_id)
        self.signal_picture = SIGNAL_PICTURE.format(entry_id=entry.entry_id)
        self.push_connected = False
        # Indoor monitors that belong to each door station, by door serial.
        self.monitors: dict[str, list[str]] = {}
        self.api = DeviceApi(lambda: self._client)
        self.coordinator: Any = None  # StatusCoordinator, set by async_setup_entry
        # Live video: keys and addresses, and the stream relay of each door station.
        self.live_relays: dict[str, Any] = {}
        self.stream_modes: dict[str, str] = {}

        self._client: Any = None
        self._mqtt: Any = None
        self._account: str = entry.data["username"]
        self._call_lock = threading.Lock()  # one request at a time on the shared session
        self.stream = StreamSupport(
            lambda: self._client,
            entry.data.get(CONF_REGION, DEFAULT_REGION),
            self._call_lock,
        )
        self._tracker = NewMessageTracker()
        self._dedupe = EventDeduper()
        self._unsub_poll: Any = None
        self._polling = False
        self._poll_failures = 0
        self._last_unlock: dict[tuple[str, int], float] = {}
        self._stopping = False

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Log in, start push and polling."""
        storage = Path(self.hass.config.path(".storage"))
        code = await self.hass.async_add_executor_job(resolve_feature_code, storage)
        await self.hass.async_add_executor_job(self._login, code)

        await self.hass.async_add_executor_job(self._start_push)
        await self._async_find_monitors()

        interval = self.entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        await self._async_poll(None)  # first list only remembers what is there
        self._unsub_poll = async_track_time_interval(
            self.hass, self._async_poll, timedelta(seconds=interval)
        )

    async def async_stop(self) -> None:
        """Stop push and polling."""
        self._stopping = True
        relays = list(self.live_relays.values())
        self.live_relays.clear()
        for relay in relays:
            try:
                await relay.stop()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Stopping a stream relay failed: %s", err)
        if self._unsub_poll:
            self._unsub_poll()
            self._unsub_poll = None
        mqtt, self._mqtt = self._mqtt, None
        if mqtt is not None:
            await self.hass.async_add_executor_job(self._stop_push, mqtt)

    async def _async_find_monitors(self) -> None:
        """Work out which indoor monitors belong to the chosen door stations.

        The option lists them by hand; empty means: take the part of a
        composite serial (``MONITOR-DOOR``) that the account lists as a device.
        """
        text = str(self.entry.options.get(CONF_MONITOR_SERIALS) or "")
        manual = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
        if manual:
            self.monitors = {serial: list(manual) for serial in self.serials}
            return
        try:
            devices = await self.hass.async_add_executor_job(self._list_serials)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not look for indoor monitors: %s", err)
            devices = {}
        self.monitors = {s: state.find_monitors(s, devices) for s in self.serials}

    def _list_serials(self) -> dict[str, Any]:
        with self._call_lock:
            return ezviz.list_devices(self._client)

    @property
    def door_lock_no(self) -> int:
        return int(self.entry.options.get(CONF_DOOR_LOCK_NO, DEFAULT_DOOR_LOCK_NO))

    @property
    def gate_lock_no(self) -> int:
        return int(self.entry.options.get(CONF_GATE_LOCK_NO, DEFAULT_GATE_LOCK_NO))

    async def async_call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking call on the shared session, one at a time."""

        def _run() -> Any:
            with self._call_lock:
                return func(*args, **kwargs)

        return await self.hass.async_add_executor_job(_run)

    async def async_relogin(self) -> None:
        """Log in again with the stored password (an expired session otherwise stays broken)."""
        storage = Path(self.hass.config.path(".storage"))
        code = await self.hass.async_add_executor_job(resolve_feature_code, storage)
        await self.hass.async_add_executor_job(self._login, code)

    # ------------------------------------------------------------------
    # Blocking helpers (run in the executor)
    # ------------------------------------------------------------------

    def _login(self, feature_code: str) -> None:
        try:
            self._client = ezviz.login(
                feature_code,
                self._account,
                self.entry.data["password"],
                self.entry.data.get(CONF_REGION, DEFAULT_REGION),
            )
        except (ezviz.InvalidAuth, ezviz.NeedsCode) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ezviz.CannotConnect as err:
            raise ConfigEntryNotReady(f"Cannot reach EZVIZ: {err}") from err

    def _start_push(self) -> None:
        """Start the push connection. Push is a bonus: failure only logs."""
        try:
            self._mqtt = self._client.get_mqtt_client(on_message_callback=self._on_push)
            self._mqtt.connect()
            self.push_connected = True
        except Exception as err:  # noqa: BLE001 - library raises several types
            self._mqtt = None
            _LOGGER.warning("EZVIZ push could not start, rings still work: %s", err)

    @staticmethod
    def _stop_push(mqtt: Any) -> None:
        try:
            mqtt.stop()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Stopping EZVIZ push failed: %s", err)

    def _fetch_messages(self) -> Any:
        with self._call_lock:
            return self._client.get_device_messages_list(limit=5)

    def _download_picture(self, url: str, serial: str) -> bytes:
        with self._call_lock:
            return self._client.download_alarm_image(url, serial)

    def _do_unlock(self, serial: str, lock_no: int) -> None:
        token = getattr(self._client, "_token", None) or {}
        user_id = token.get("username") or self._account
        with self._call_lock:
            self._client.remote_unlock(serial, user_id, lock_no)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _on_push(self, decoded: dict[str, Any]) -> None:
        """Called from the push thread."""
        if self._stopping:
            return
        event = event_from_push(decoded, dt_util.DEFAULT_TIME_ZONE)
        if event is None:
            return
        self.hass.loop.call_soon_threadsafe(self._handle_event, event)

    async def _async_poll(self, _now: Any) -> None:
        if self._polling or self._stopping:
            return
        self._polling = True
        try:
            answer = await self.hass.async_add_executor_job(self._fetch_messages)
        except Exception as err:  # noqa: BLE001
            self._poll_failures += 1
            level = logging.WARNING if self._poll_failures == POLL_WARN_AFTER else logging.DEBUG
            _LOGGER.log(level, "EZVIZ message check failed (%s in a row): %s", self._poll_failures, err)
            return
        finally:
            self._polling = False
        self._poll_failures = 0
        tz = dt_util.DEFAULT_TIME_ZONE
        for message in self._tracker.fresh(messages_from_answer(answer)):
            event = event_from_message(message, tz)
            if event is not None:
                self._handle_event(event)

    @callback
    def _handle_event(self, event: BellEvent) -> None:
        if not any(same_device(event.serial, s) for s in self.serials):
            return
        now = dt_util.now()
        if is_stale(event, now) or not self._dedupe.is_new(event, now):
            return
        _LOGGER.debug("EZVIZ event: %s from %s via %s", event.kind, event.serial, event.source)
        async_dispatcher_send(self.hass, self.signal_event, event)
        if event.picture_url:
            self.entry.async_create_background_task(
                self.hass, self._async_load_picture(event), f"{DOMAIN}_picture"
            )

    async def _async_load_picture(self, event: BellEvent) -> None:
        """Download the picture, retrying: it can lag behind the event."""
        for attempt in range(PICTURE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(PICTURE_RETRY_DELAY)
            try:
                data = await self.hass.async_add_executor_job(
                    self._download_picture, event.picture_url, event.serial
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Picture attempt %s failed: %s", attempt + 1, err)
                continue
            if data:
                async_dispatcher_send(self.hass, self.signal_picture, event, data)
                return
        _LOGGER.debug("No picture for %s event from %s", event.kind, event.serial)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_download_picture(self, url: str, serial: str) -> bytes | None:
        """Download a picture of an alarm (or None if EZVIZ does not give it)."""
        try:
            return await self.hass.async_add_executor_job(self._download_picture, url, serial)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Picture download failed: %s", err)
            return None

    async def async_terminal_names(self) -> list[str]:
        """Names of the terminals registered on the account."""

        def _read() -> list[str]:
            with self._call_lock:
                return ezviz.terminal_names(self._client)

        return await self.hass.async_add_executor_job(_read)

    async def async_unlock(self, serial: str, lock_no: int) -> None:
        """Open one specific lock. Never falls back to another lock."""
        key = (serial, lock_no)
        now = time.monotonic()
        if now - self._last_unlock.get(key, -UNLOCK_COOLDOWN) < UNLOCK_COOLDOWN:
            return  # ignore an accidental double press
        self._last_unlock[key] = now
        try:
            await self.hass.async_add_executor_job(self._do_unlock, serial, lock_no)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unlock_failed",
                translation_placeholders={"error": str(err)},
            ) from err
