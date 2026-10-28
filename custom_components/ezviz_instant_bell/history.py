"""Past events of a door station (rings, unlocks, motion ...) from the cloud message list.

Pure Python (no Home Assistant imports). The cloud keeps a list of messages per
day; each has a title, a time and, mostly, a picture link.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any

try:
    from .parsing import local_ms_to_datetime, parse_local_time
except ImportError:  # imported on its own by the tests that do not load Home Assistant
    from parsing import local_ms_to_datetime, parse_local_time


@dataclass(frozen=True)
class HistoryItem:
    """One entry of the message list."""

    message_id: str
    serial: str | None
    occurred: datetime | None
    title: str
    picture_url: str | None
    subtype: int | None

    def label(self) -> str:
        """Short text for lists: ``14:03:12  Your doorbell is ringing``."""
        clock = self.occurred.strftime("%H:%M:%S") if self.occurred else "--:--:--"
        return f"{clock}  {self.title}".rstrip() if self.title else clock


def item_from_message(message: dict[str, Any], tz: tzinfo) -> HistoryItem | None:
    """Build an item from a cloud list entry; None if it has no ID."""
    message_id = message.get("msgId")
    if message_id in (None, ""):
        return None
    try:
        subtype = int(message.get("subType"))
    except (TypeError, ValueError):
        subtype = None
    picture = message.get("pic")
    # "from" is the NAME of the device on most entries; "deviceSerial" is the serial.
    serial = message.get("deviceSerial") or message.get("from")
    return HistoryItem(
        message_id=str(message_id),
        serial=str(serial) if serial else None,
        occurred=local_ms_to_datetime(message.get("time"), tz) or parse_local_time(message.get("timeStr"), tz),
        title=str(message.get("title") or "").strip(),
        picture_url=picture if isinstance(picture, str) and picture.startswith("http") else None,
        subtype=subtype,
    )


def items_from_answer(answer: Any, tz: tzinfo) -> list[HistoryItem]:
    """Items of a ``get_device_messages_list`` answer, newest first, without repeats."""
    entries = answer.get("message") if isinstance(answer, dict) else None
    seen: set[str] = set()
    items: list[HistoryItem] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        item = item_from_message(entry, tz)
        if item is None or item.message_id in seen:
            continue
        seen.add(item.message_id)
        items.append(item)
    items.sort(key=lambda i: i.occurred.timestamp() if i.occurred else 0, reverse=True)
    return items


def day_text(day: date) -> str:
    """The date the cloud list wants: ``YYYYMMDD``."""
    return day.strftime("%Y%m%d")


def parse_day(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None
