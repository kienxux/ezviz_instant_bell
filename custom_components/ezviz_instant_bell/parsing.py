"""Turn EZVIZ push messages and cloud message-list entries into bell events.

Pure Python: no Home Assistant imports, so it can be tested on its own.

What the HP7 sends (observed on a real device):

* Push (MQTT): unlock and gate actions arrive as alert codes 10242 and
  10243 within a few seconds, with a picture link. A doorbell ring is NOT
  pushed on this channel.
* Cloud message list: a ring is an entry with subType 2701
  ("Your doorbell is ringing") and a picture link. Its ``time`` field is local
  wall-clock time written as if it were UTC, so it must be re-labelled with the
  local time zone, not converted.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

KIND_RING = "ring"
KIND_DOOR = "door_unlock"
KIND_GATE = "gate_open"
KIND_OTHER = "other"
EVENT_KINDS = [KIND_RING, KIND_DOOR, KIND_GATE, KIND_OTHER]

RING_SUBTYPE = 2701
PUSH_CODE_KINDS = {10242: KIND_DOOR, 10243: KIND_GATE, RING_SUBTYPE: KIND_RING}

# Events older than this when we first see them are not announced again.
MAX_EVENT_AGE = timedelta(seconds=120)
# Two events of the same kind for the same device this close are one event.
DUPLICATE_WINDOW = timedelta(seconds=10)

# Positions in the comma separated ``ext`` string of a push message, as seen on
# real messages: 1 = time, 2 = device serial, 4 = alert type code, 16 = picture
# link, 17 = device name. The reader below tolerates a shorter string (a missing
# field is None), so a change of EZVIZ's format loses a field, it does not crash.
_EXT_TIME, _EXT_SERIAL, _EXT_CODE, _EXT_IMAGE, _EXT_NAME = 1, 2, 4, 16, 17


@dataclass(frozen=True)
class BellEvent:
    """One thing that happened at a doorbell."""

    serial: str
    kind: str
    occurred: datetime | None
    title: str
    picture_url: str | None
    source: str  # "push" or "poll"
    code: int | None = None
    message_id: str | None = None


def parse_local_time(text: Any, tz: tzinfo) -> datetime | None:
    """Parse ``YYYY-MM-DD HH:MM:SS`` written in local time."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None


def local_ms_to_datetime(value: Any, tz: tzinfo) -> datetime | None:
    """Convert the cloud list's ``time`` (ms) to an aware local datetime.

    The number is local wall-clock time counted as if it were UTC, so the
    clock reading is kept and only the zone label is replaced.
    """
    try:
        seconds = float(value) / 1000
    except (TypeError, ValueError):
        return None
    wall = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    return wall.replace(tzinfo=tz)


def _ext_fields(raw: Any) -> dict[str, Any]:
    """Return the needed fields from either a decoded dict or the raw string."""
    if isinstance(raw, dict):
        return {
            "time": raw.get("time"),
            "serial": raw.get("device_serial"),
            "code": raw.get("alert_type_code"),
            "image": raw.get("image") or raw.get("default_pic_url"),
            "name": raw.get("device_name"),
        }
    if isinstance(raw, str):
        parts = raw.split(",")

        def at(i: int) -> Any:
            return parts[i] if i < len(parts) and parts[i] != "" else None

        return {
            "time": at(_EXT_TIME),
            "serial": at(_EXT_SERIAL),
            "code": at(_EXT_CODE),
            "image": at(_EXT_IMAGE),
            "name": at(_EXT_NAME),
        }
    return {}


def event_from_push(decoded: dict[str, Any], tz: tzinfo) -> BellEvent | None:
    """Build an event from a decoded push message, or None if unusable."""
    fields = _ext_fields(decoded.get("ext"))
    serial = fields.get("serial")
    if not serial:
        return None
    try:
        code = int(fields["code"])
    except (KeyError, TypeError, ValueError):
        code = None
    picture = fields.get("image")
    return BellEvent(
        serial=str(serial),
        kind=PUSH_CODE_KINDS.get(code, KIND_OTHER),
        occurred=parse_local_time(fields.get("time"), tz),
        title=str(decoded.get("alert") or ""),
        picture_url=picture if isinstance(picture, str) and picture.startswith("http") else None,
        source="push",
        code=code,
        message_id=str(decoded["id"]) if decoded.get("id") is not None else None,
    )


def event_from_message(message: dict[str, Any], tz: tzinfo) -> BellEvent | None:
    """Build a ring event from a cloud list entry. Other entry types return None."""
    try:
        subtype = int(message.get("subType"))
    except (TypeError, ValueError):
        return None
    if subtype != RING_SUBTYPE:
        return None
    serial = message.get("from") or message.get("deviceSerial")
    if not serial:
        return None
    occurred = local_ms_to_datetime(message.get("time"), tz) or parse_local_time(
        message.get("timeStr"), tz
    )
    picture = message.get("pic")
    return BellEvent(
        serial=str(serial),
        kind=KIND_RING,
        occurred=occurred,
        title=str(message.get("title") or ""),
        picture_url=picture if isinstance(picture, str) and picture.startswith("http") else None,
        source="poll",
        code=subtype,
        message_id=str(message["msgId"]) if message.get("msgId") else None,
    )


def same_device(a: str, b: str) -> bool:
    """True if two serials name the same doorbell.

    The HP7 appears as ``DOORBELL-MONITOR``; the bare doorbell part is enough
    to match a push serial against a list serial. Only the part before the
    first dash is compared, so a bare serial matches the composite that starts
    with it. This is meant: the cloud names the same device both ways.
    """
    a, b = a.strip().upper(), b.strip().upper()
    return a == b or a.split("-")[0] == b.split("-")[0]


def is_stale(event: BellEvent, now: datetime) -> bool:
    """True if the event is too old to announce."""
    return event.occurred is not None and now - event.occurred > MAX_EVENT_AGE


class EventDeduper:
    """Remember recent events so the same one is announced only once."""

    def __init__(self, keep: int = 200) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._recent: list[tuple[str, str, datetime]] = []
        self._keep = keep

    def is_new(self, event: BellEvent, now: datetime) -> bool:
        """Return True and remember the event if it has not been seen before."""
        if event.message_id and event.message_id in self._ids:
            return False
        stamp = event.occurred or now
        serial = event.serial.split("-")[0].upper()
        self._recent = [r for r in self._recent if now - r[2] < MAX_EVENT_AGE * 2]
        for r_serial, r_kind, r_stamp in self._recent:
            if (
                r_serial == serial
                and r_kind == event.kind
                and abs(r_stamp - stamp) <= DUPLICATE_WINDOW
            ):
                self._remember_id(event)
                return False
        self._recent.append((serial, event.kind, stamp))
        self._remember_id(event)
        return True

    def _remember_id(self, event: BellEvent) -> None:
        if event.message_id:
            self._ids[event.message_id] = None
            while len(self._ids) > self._keep:
                self._ids.popitem(last=False)


class NewMessageTracker:
    """Pick out entries of the cloud list that were not there before.

    The first list only fills the memory, so old entries are never announced
    when the integration starts.
    """

    def __init__(self, keep: int = 500) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seeded = False
        self._keep = keep

    def fresh(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        new: list[dict[str, Any]] = []
        for message in messages:
            msg_id = message.get("msgId")
            if not msg_id or msg_id in self._seen:
                continue
            self._seen[msg_id] = None
            new.append(message)
        while len(self._seen) > self._keep:
            self._seen.popitem(last=False)
        if not self._seeded:
            self._seeded = True
            return []
        # Oldest first, so events fire in the order they happened.
        return list(reversed(new))


def messages_from_answer(answer: Any) -> list[dict[str, Any]]:
    """Return the list of entries from a get_device_messages_list answer."""
    if isinstance(answer, dict):
        items = answer.get("message")
        if isinstance(items, list):
            return [m for m in items if isinstance(m, dict)]
    return []
