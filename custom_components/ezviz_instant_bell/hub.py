"""Connection to the EZVIZ cloud: push, polling, pictures and unlocking."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICES,
    CONF_DOOR_LOCK_NO,
    CONF_GATE_LOCK_NO,
    CONF_MONITOR_SERIALS,
    CONF_NAMES,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_SAVE_CLIPS,
    CLIP_SAVE_FIRST_WAIT,
    CLIP_SAVE_GIVE_UP,
    CLIP_SAVE_POLL,
    CLOUD_RETRY_SECONDS,
    DEFAULT_DOOR_LOCK_NO,
    EVENT_CLIP_SAVED,
    EVENT_DOOR_OPENED,
    DEFAULT_GATE_LOCK_NO,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DOMAIN,
    HISTORY_MAX_PAGES,
    HISTORY_PAGE_SIZE,
    HISTORY_PAST_TTL,
    HISTORY_PICTURE_CACHE,
    HISTORY_TODAY_TTL,
    ISSUE_CLOUD_OFFLINE,
    PICTURE_ATTEMPTS,
    PICTURE_RETRY_DELAY,
    RECORDING_LIST_TTL_PAST,
    RECORDING_LIST_TTL_TODAY,
    RECORDING_MAX_PAGES,
    RECORDING_PAGE_SIZE,
    SIGNAL_CLIP,
    SIGNAL_EVENT,
    SIGNAL_PICTURE,
    UNLOCK_COOLDOWN,
)
from . import ezviz, state
from .device_api import DeviceApi
from .clips import ClipStore
from .recordings import Recording, channel_serial, clip_id, day_window, recordings_from_answer
from .visitors import clip_for
from .stream_support import StreamSupport
from .feature_code import resolve_feature_code
from .history import HistoryItem, day_text, items_from_answer
from .parsing import (
    KIND_RING,
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
        self.signal_clip = SIGNAL_CLIP.format(entry_id=entry.entry_id)
        # The newest saved ring clip of each door station (see ``_async_save_ring_clip``).
        self.last_clip: dict[str, dict[str, Any]] = {}
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
            on_learned=self._learned_from_thread,
        )
        # Kept on disk so that the integration starts without the cloud: the LAN
        # key and address (via ``stream``), the monitors and the model of each door station.
        self._local_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.local.{entry.entry_id}")
        self._models: dict[str, str] = {}
        self.cloud_online = True
        self._unsub_cloud_retry: Any = None
        self._tracker = NewMessageTracker()
        self._dedupe = EventDeduper()
        self._unsub_poll: Any = None
        self._polling = False
        self._poll_failures = 0
        self._last_unlock: dict[tuple[str, int], float] = {}
        self._stopping = False
        self._history: dict[tuple[str, str], tuple[float, list[HistoryItem]]] = {}
        self._history_pictures: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._recordings: dict[tuple[str, str], tuple[float, list[Recording]]] = {}
        self.clips = ClipStore(hass, self)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Log in, start push and polling.

        If the cloud cannot be reached but this entry has run before, start
        anyway with what is stored (the local live video works) and keep
        trying to log in; the entry reloads once the cloud answers.
        """
        stored = await self._local_store.async_load() or {}
        self.stream.load_remembered(stored.get("doors"))
        self._models = {str(k): str(v) for k, v in (stored.get("models") or {}).items()}
        storage = Path(self.hass.config.path(".storage"))
        code = await self.hass.async_add_executor_job(resolve_feature_code, storage)
        try:
            await self.hass.async_add_executor_job(self._login, code)
        except ConfigEntryNotReady as err:
            if not self.stream.remembered:
                raise  # nothing stored yet: nothing useful can run
            self._start_without_cloud(err, stored.get("monitors"))
            return

        await self.hass.async_add_executor_job(self._start_push)
        await self._async_find_monitors()
        self._save_local()

        interval = self.entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        await self._async_poll(None)  # first list only remembers what is there
        self._unsub_poll = async_track_time_interval(
            self.hass, self._async_poll, timedelta(seconds=interval)
        )

    async def async_stop(self) -> None:
        """Stop push and polling."""
        self._stopping = True
        if self._unsub_cloud_retry:
            self._unsub_cloud_retry()
            self._unsub_cloud_retry = None
        await self.clips.async_stop()
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

    # ------------------------------------------------------------------
    # Without the cloud
    # ------------------------------------------------------------------

    def _start_without_cloud(self, err: Exception, monitors: Any) -> None:
        self.cloud_online = False
        if isinstance(monitors, dict):
            self.monitors = {s: [str(m) for m in monitors.get(s, [])] for s in self.serials}
        _LOGGER.warning(
            "EZVIZ cannot be reached (%s). Starting with the local connection only: the local live video "
            "works, rings, unlocking and settings come back when EZVIZ answers again",
            err,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_CLOUD_OFFLINE}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_CLOUD_OFFLINE,
            translation_placeholders={"title": self.entry.title},
        )
        self._unsub_cloud_retry = async_track_time_interval(
            self.hass, self._async_retry_cloud, timedelta(seconds=CLOUD_RETRY_SECONDS)
        )

    async def _async_retry_cloud(self, _now: Any) -> None:
        if self._stopping or self.cloud_online:
            return
        storage = Path(self.hass.config.path(".storage"))
        try:
            code = await self.hass.async_add_executor_job(resolve_feature_code, storage)
            await self.hass.async_add_executor_job(self._login, code)
        except ConfigEntryAuthFailed:
            self.entry.async_start_reauth(self.hass)
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("EZVIZ still not reachable: %s", err)
            return
        _LOGGER.info("EZVIZ answers again; reloading to start everything")
        self.cloud_online = True
        if self._unsub_cloud_retry:
            self._unsub_cloud_retry()
            self._unsub_cloud_retry = None
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_CLOUD_OFFLINE}_{self.entry.entry_id}")
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    def remembered_model(self, serial: str) -> str | None:
        return self._models.get(serial)

    def remember_models(self, models: dict[str, str]) -> None:
        """Called with the models of a good status read; stored when they changed."""
        if all(self._models.get(s) == m for s, m in models.items()):
            return
        self._models.update(models)
        self._save_local()

    def _learned_from_thread(self) -> None:
        self.hass.loop.call_soon_threadsafe(self._save_local)

    @callback
    def _save_local(self) -> None:
        def data() -> dict[str, Any]:
            return {
                "doors": {k: dict(v) for k, v in list(self.stream.remembered.items())},
                "models": dict(self._models),
                "monitors": {k: list(v) for k, v in self.monitors.items()},
            }

        self._local_store.async_delay_save(data, 5)

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
    def client(self) -> Any:
        return self._client

    @property
    def call_lock(self) -> threading.Lock:
        return self._call_lock

    @property
    def door_lock_no(self) -> int:
        return int(self.entry.options.get(CONF_DOOR_LOCK_NO, DEFAULT_DOOR_LOCK_NO))

    @property
    def gate_lock_no(self) -> int:
        return int(self.entry.options.get(CONF_GATE_LOCK_NO, DEFAULT_GATE_LOCK_NO))

    def _require_cloud(self) -> None:
        if self._client is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="cloud_unreachable")

    async def async_call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking call on the shared session, one at a time."""
        self._require_cloud()

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
        if self._polling or self._stopping or self._client is None:
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
        if event.kind == KIND_RING and self.entry.options.get(CONF_SAVE_CLIPS):
            serial = next(s for s in self.serials if same_device(event.serial, s))
            self.entry.async_create_background_task(
                self.hass, self._async_save_ring_clip(event, serial), f"{DOMAIN}_ring_clip"
            )

    async def _async_save_ring_clip(self, event: BellEvent, serial: str) -> None:
        """Keep the SD-card recording of a ring and tell where it is.

        The door station is still recording when the ring arrives, so the list is
        looked at every few seconds until the recording that covers the ring has
        ended. The clip is then fetched like one a person plays, and kept.
        """
        moment = event.occurred or dt_util.now()
        day = moment.date()
        await asyncio.sleep(CLIP_SAVE_FIRST_WAIT)
        deadline = time.monotonic() + CLIP_SAVE_GIVE_UP
        rec: Recording | None = None
        previous_end = None
        while True:
            try:
                recordings = await self.async_recordings_day(serial, day, refresh=True)
            except HomeAssistantError:
                recordings = []
            found = clip_for(moment, recordings)
            if found is not None:
                # The list uses the door station's clock; a recording whose end stopped moving is over too.
                over = dt_util.now().replace(tzinfo=None) >= found.end + timedelta(seconds=5)
                if over or found.end == previous_end:
                    rec = found
                    break
                previous_end = found.end
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(CLIP_SAVE_POLL)
        if rec is None:
            _LOGGER.debug("No recording found for the %s ring of %s", moment, serial)
            return
        wanted = clip_id(rec.begin, rec.end)
        if self.last_clip.get(serial, {}).get("clip_id") == wanted:
            return  # an earlier ring of the same visit already saved and announced this recording
        path = await self.clips.async_save(serial, rec.begin, rec.end)
        if path is None:
            _LOGGER.debug("The clip of the ring at %s could not be fetched", moment)
            return
        size = await self.hass.async_add_executor_job(lambda: path.stat().st_size)
        info = {
            "serial": serial,
            "clip_id": wanted,
            "media_content_id": (
                f"media-source://{DOMAIN}/{self.entry.entry_id}/{serial}/rec/{day_text(rec.begin.date())}/{wanted}"
            ),
            "begin": rec.begin.isoformat(),
            "end": rec.end.isoformat(),
            "seconds": rec.seconds,
            "bytes": size,
            "message_id": event.message_id,
        }
        self.last_clip[serial] = info
        async_dispatcher_send(self.hass, self.signal_clip, serial, info)
        self.hass.bus.async_fire(EVENT_CLIP_SAVED, {**info, "entry_id": self.entry.entry_id})

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

    # ------------------------------------------------------------------
    # History (Media browser)
    # ------------------------------------------------------------------

    def _fetch_history_day(self, serial: str, day: date) -> list[HistoryItem]:
        """Blocking: all list entries of one day (a few pages at most)."""
        tz = dt_util.DEFAULT_TIME_ZONE
        items: list[HistoryItem] = []
        seen: set[str] = set()
        end_time: str = ""
        for _ in range(HISTORY_MAX_PAGES):
            with self._call_lock:
                answer = self._client.get_device_messages_list(
                    serial, limit=HISTORY_PAGE_SIZE, date=day_text(day), end_time=end_time
                )
            page = items_from_answer(answer, tz)
            fresh = [i for i in page if i.message_id not in seen]
            if not fresh:
                break
            seen.update(i.message_id for i in fresh)
            items.extend(fresh)
            if len(page) < HISTORY_PAGE_SIZE:
                break
            # The list is paged by the ID of the last (oldest) entry.
            end_time = min(page, key=lambda i: i.occurred.timestamp() if i.occurred else 0).message_id
        wanted = [i for i in items if i.serial is None or same_device(i.serial, serial)]
        wanted.sort(key=lambda i: i.occurred.timestamp() if i.occurred else 0, reverse=True)
        return wanted

    async def async_history_day(self, serial: str, day: date) -> list[HistoryItem]:
        """Events of one day, newest first. Kept for a while, so that browsing is quick."""
        key = (serial, day_text(day))
        cached = self._history.get(key)
        ttl = HISTORY_TODAY_TTL if day >= dt_util.now().date() else HISTORY_PAST_TTL
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
        try:
            items = await self.hass.async_add_executor_job(self._fetch_history_day, serial, day)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("History of %s for %s failed: %s", serial, day, err)
            if cached:
                return cached[1]
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="history_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._history[key] = (time.monotonic(), items)
        return items

    async def async_history_picture(self, serial: str, day: date, message_id: str) -> bytes | None:
        """Picture of one past event (kept in memory only)."""
        key = (serial, message_id)
        if key in self._history_pictures:
            self._history_pictures.move_to_end(key)
            return self._history_pictures[key]
        items = await self.async_history_day(serial, day)
        item = next((i for i in items if i.message_id == message_id), None)
        if item is None or not item.picture_url:
            return None
        data = await self.async_download_picture(item.picture_url, serial)
        if data:
            self._history_pictures[key] = data
            while len(self._history_pictures) > HISTORY_PICTURE_CACHE:
                self._history_pictures.popitem(last=False)
        return data

    # ------------------------------------------------------------------
    # Recordings on the SD card (Media browser)
    # ------------------------------------------------------------------

    def _fetch_recordings_day(self, serial: str, day: date) -> list[Recording]:
        """Blocking: every recording of one day (the list comes in pages of at most 50)."""
        start, stop = day_window(day)
        found: dict[Any, Recording] = {}
        channel = channel_serial(serial)
        for _ in range(RECORDING_MAX_PAGES):
            with self._call_lock:
                answer = self._client.search_records(serial, 1, channel, start, stop, size=RECORDING_PAGE_SIZE)
            page = recordings_from_answer(answer)
            fresh = [r for r in page if r.begin not in found]
            if not fresh:
                break
            found.update((r.begin, r) for r in fresh)
            if len(page) < RECORDING_PAGE_SIZE:
                break
            start = max(r.end for r in page).strftime("%Y-%m-%dT%H:%M:%S")
        return [found[key] for key in sorted(found)]

    async def async_recordings_day(self, serial: str, day: date, *, refresh: bool = False) -> list[Recording]:
        """Recordings of one day, oldest first. Kept for a while, so that browsing is quick."""
        key = (serial, day_text(day))
        cached = self._recordings.get(key)
        ttl = RECORDING_LIST_TTL_TODAY if day >= dt_util.now().date() else RECORDING_LIST_TTL_PAST
        if cached and not refresh and time.monotonic() - cached[0] < ttl:
            return cached[1]
        try:
            items = await self.hass.async_add_executor_job(self._fetch_recordings_day, serial, day)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Recordings of %s for %s failed: %s", serial, day, err)
            if cached:
                return cached[1]
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="recordings_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._recordings[key] = (time.monotonic(), items)
        return items

    async def async_terminal_names(self) -> list[str]:
        """Names of the terminals registered on the account."""

        def _read() -> list[str]:
            with self._call_lock:
                return ezviz.terminal_names(self._client)

        return await self.hass.async_add_executor_job(_read)

    async def async_unlock(self, serial: str, lock_no: int, context: Context | None = None) -> None:
        """Open one specific lock. Never falls back to another lock.

        ``context`` says who asked; a successful opening is announced with it.
        """
        self._require_cloud()
        key = (serial, lock_no)
        now = time.monotonic()
        if now - self._last_unlock.get(key, -UNLOCK_COOLDOWN) < UNLOCK_COOLDOWN:
            return  # ignore an accidental double press
        self._last_unlock[key] = now
        try:
            await self.hass.async_add_executor_job(self._do_unlock, serial, lock_no)
        except Exception as err:  # noqa: BLE001
            self._last_unlock.pop(key, None)  # a failed press must not block the retry
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unlock_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.hass.bus.async_fire(
            EVENT_DOOR_OPENED,
            {
                "serial": serial,
                "lock": "door" if lock_no == self.door_lock_no else "gate" if lock_no == self.gate_lock_no else f"lock {lock_no}",
                "lock_no": lock_no,
                "user_id": context.user_id if context else None,
            },
            context=context,
        )
