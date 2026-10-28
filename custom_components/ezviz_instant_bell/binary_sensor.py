"""Binary sensors: motion, alarms (ring, unlock ...) and read-only feature states."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import InstantBellConfigEntry
from .alarms import ALARM_KINDS, UNLOCK_KEYS, AlarmKind, extract_card_id, kinds_for, matches, resolve_card_name
from .const import EVENT_UNLOCK, EVENT_UNLOCK_LEGACY, PULSE_SECONDS
from .coordinator import StatusCoordinator
from .entity import StatusEntity
from .hub import InstantBellHub
from .parsing import BellEvent, parse_local_time, same_device
from .state import coerce_bool

_LOGGER = logging.getLogger(__name__)

# Read-only settings, created only when the device reports them.
FEATURES: tuple[tuple[str, str], ...] = (
    ("mute_on", "feature_mute"),
    ("loitering_on", "feature_loitering"),
    ("stranger_detection_on", "feature_stranger_detection"),
    ("human_detection_on", "feature_human_detection"),
)

# Two reports of one alarm kind this close together are the same alarm.
SAME_ALARM_SECONDS = 10.0


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    coordinator = hub.coordinator
    entities: list[BinarySensorEntity] = []
    for serial in hub.serials:
        entities.append(MotionBinary(hub, coordinator, serial))
        entities.extend(AlarmBinary(hub, coordinator, serial, kind) for kind in ALARM_KINDS)
        reported = (coordinator.data or {}).get(serial, {})
        entities.extend(
            FeatureBinary(hub, coordinator, serial, data_key, key)
            for data_key, key in FEATURES
            if reported.get(data_key) is not None
        )
    async_add_entities(entities)


class MotionBinary(StatusEntity, BinarySensorEntity):
    """Motion reported by the doorbell."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, hub: InstantBellHub, coordinator: StatusCoordinator, serial: str) -> None:
        super().__init__(hub, coordinator, serial, "motion_trigger", unique_suffix="binary_motion")

    @property
    def is_on(self) -> bool:
        return bool(coerce_bool(self.status.get("motion")))


class FeatureBinary(StatusEntity, BinarySensorEntity):
    """A setting of the device that can be read but not changed from the cloud."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, hub: InstantBellHub, coordinator: StatusCoordinator, serial: str, data_key: str, key: str
    ) -> None:
        super().__init__(hub, coordinator, serial, key, unique_suffix=f"binary_{data_key}")
        self._data_key = data_key

    @property
    def is_on(self) -> bool:
        return bool(coerce_bool(self.status.get(self._data_key)))


class AlarmBinary(StatusEntity, BinarySensorEntity):
    """On for a few seconds when an alarm of one kind has just happened.

    Two sources feed it: push and ring messages, which arrive within seconds
    and are used for the ring and the unlock events, and the regularly read
    status with the name of the latest alarm, which also covers the others.
    The same alarm is only counted once.
    """

    def __init__(
        self, hub: InstantBellHub, coordinator: StatusCoordinator, serial: str, kind: AlarmKind
    ) -> None:
        super().__init__(hub, coordinator, serial, kind.key, unique_suffix=f"alarm_{kind.key}")
        self._kind = kind
        self._attr_icon = kind.icon
        self._on = False
        self._off_unsub: CALLBACK_TYPE | None = None
        self._seen: list[float] = []
        self._primed = False

    # -- state ----------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._on

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self._hub.signal_event, self._handle_hub_event))
        self.async_on_remove(self._cancel_off)

    @callback
    def _cancel_off(self) -> None:
        if self._off_unsub:
            self._off_unsub()
            self._off_unsub = None

    # -- sources --------------------------------------------------------

    @callback
    def _handle_hub_event(self, event: BellEvent) -> None:
        if not same_device(event.serial, self._serial):
            return
        if self._kind.key not in kinds_for(event.title, event.kind):
            return
        stamp = event.occurred.timestamp() if event.occurred else time.time()
        if self._is_new(stamp):
            self._trigger(self._status_fields(event.title, event.occurred.isoformat() if event.occurred else None))

    @callback
    def _handle_coordinator_update(self) -> None:
        status = self.status
        alarm_name = status.get("alarm_name")
        alarm_time = status.get("last_alarm_time")
        stamp = None
        parsed = parse_local_time(alarm_time, dt_util.DEFAULT_TIME_ZONE)
        if parsed is not None:
            stamp = parsed.timestamp()

        if not self._primed:
            # The cloud always reports the LAST alarm, however old. Remember
            # it, but do not announce it as if it had just happened.
            self._primed = True
            if stamp is not None:
                self._seen.append(stamp)
        elif matches(self._kind, alarm_name) and stamp is not None and self._is_new(stamp):
            self._trigger(self._status_fields(alarm_name, alarm_time))
        super()._handle_coordinator_update()

    def _is_new(self, stamp: float) -> bool:
        if any(abs(stamp - old) <= SAME_ALARM_SECONDS for old in self._seen):
            return False
        self._seen = [*self._seen[-19:], stamp]
        return True

    # -- action ---------------------------------------------------------

    def _status_fields(self, alarm_name: Any, alarm_time: Any) -> dict[str, Any]:
        return {"alarm_name": alarm_name, "alarm_time": alarm_time}

    @callback
    def _trigger(self, fields: dict[str, Any]) -> None:
        self._on = True
        self._cancel_off()

        @callback
        def _off(_now: Any) -> None:
            self._off_unsub = None
            self._on = False
            self.async_write_ha_state()

        self._off_unsub = async_call_later(self.hass, PULSE_SECONDS, _off)
        self.async_write_ha_state()
        if self._kind.key in UNLOCK_KEYS:
            self._fire_unlock(fields)

    @callback
    def _fire_unlock(self, fields: dict[str, Any]) -> None:
        """Tell automations which way the lock was opened."""
        status = self.status
        detail = status.get("latest_alarm_detail")
        keys = status.get("keys")
        payload: dict[str, Any] = {
            "category": self._kind.key,
            "alarm_name": fields.get("alarm_name"),
            "alarm_time": fields.get("alarm_time"),
            "serial": self._serial,
        }
        if detail is not None:
            payload["details"] = detail
        card_id = extract_card_id(detail)
        if card_id is not None:
            payload["card_id"] = card_id
        if isinstance(keys, list) and keys:
            payload["keys"] = keys
            card_name = resolve_card_name(keys, card_id)
            if card_name:
                payload["card_name"] = card_name
        self.hass.bus.async_fire(EVENT_UNLOCK, payload)
        self.hass.bus.async_fire(EVENT_UNLOCK_LEGACY, payload)
