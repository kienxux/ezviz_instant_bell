"""Recordings on the SD card of a door station (pure Python, no Home Assistant imports).

The EZVIZ cloud lists what the door station recorded (``search_records``) and
can relay a recording back through the same video servers as the live view.
This module knows the list format and how the playback address is built.

Credit: the playback address and the client settings follow pyEzvizApi pull
request #266 (https://github.com/RenierM26/pyEzvizApi/pull/266, Apache-2.0),
which found that the EZVIZ servers want the begin/end times as compact text
and a few extra fields; they were confirmed against a real door station.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

TIME_FORMAT = "%Y%m%dT%H%M%SZ"
_LIST_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
CLIP_ID_SEPARATOR = "_"
MAX_CLIP_SECONDS = 15 * 60  # a recording longer than this is not fetched in one go


@dataclass(frozen=True)
class Recording:
    """One recording on the SD card.

    The times are the door station's local clock as plain (naive) datetimes, not
    UTC: that is what the list gives and what the video servers want back.
    """

    begin: datetime
    end: datetime
    kind: str = ""

    @property
    def seconds(self) -> int:
        return max(0, int((self.end - self.begin).total_seconds()))

    @property
    def clip_id(self) -> str:
        return clip_id(self.begin, self.end)

    def label(self) -> str:
        return f"{self.begin.strftime('%H:%M:%S')}  ({format_length(self.seconds)})"


def format_length(seconds: int) -> str:
    minutes, secs = divmod(max(0, seconds), 60)
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"


def compact_time(value: datetime) -> str:
    """``2026-10-28 15:42:03`` -> ``20261028T154203Z``.

    The ``Z`` is only part of the format: the servers expect the local clock
    time of the door station here, not UTC (checked on real hardware).
    """
    return value.strftime(TIME_FORMAT)


def parse_compact_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def clip_id(begin: datetime, end: datetime) -> str:
    return f"{compact_time(begin)}{CLIP_ID_SEPARATOR}{compact_time(end)}"


def parse_clip_id(text: str) -> tuple[datetime, datetime] | None:
    """Inverse of :func:`clip_id`; None for anything that is not a sane time span."""
    begin_text, sep, end_text = (text or "").partition(CLIP_ID_SEPARATOR)
    if not sep:
        return None
    begin, end = parse_compact_time(begin_text), parse_compact_time(end_text)
    if begin is None or end is None or end <= begin:
        return None
    if (end - begin).total_seconds() > MAX_CLIP_SECONDS:
        return None
    return begin, end


def recordings_from_answer(answer: Any) -> list[Recording]:
    """Recordings of a ``search_records`` answer, oldest first, without repeats."""
    entries = answer.get("records") if isinstance(answer, dict) else None
    found: dict[datetime, Recording] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            begin = datetime.strptime(str(entry.get("begin")), _LIST_TIME_FORMAT)
            end = datetime.strptime(str(entry.get("end")), _LIST_TIME_FORMAT)
        except ValueError:
            continue
        if end <= begin:
            continue
        found[begin] = Recording(begin, end, str(entry.get("type") or ""))
    return [found[key] for key in sorted(found)]


def day_window(day: date) -> tuple[str, str]:
    """Start and end of a day in the format ``search_records`` wants."""
    start = datetime(day.year, day.month, day.day)
    stop = start + timedelta(days=1) - timedelta(seconds=1)
    return start.strftime(_LIST_TIME_FORMAT), stop.strftime(_LIST_TIME_FORMAT)


def channel_serial(serial: str) -> str:
    """``MONITOR-CAMERA`` -> ``CAMERA`` (the part ``search_records`` wants as channel serial)."""
    return serial.partition("-")[2] or serial


def build_playback_url(
    live_url: str,
    serial: str,
    channel: int,
    begin: datetime,
    end: datetime,
    *,
    client_type: int = 3,
    timestamp_ms: int | None = None,
    rnd: int | None = None,
) -> str:
    """Playback address from the live address of the same camera.

    The live address carries the server, the session token and the business
    parameters; the playback one keeps them and asks for ``/playback``.
    """
    parsed = urlparse(live_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.pop("vip", None)
    params.update(
        {
            "dev": serial,
            "serial": serial,
            "chn": str(channel),
            "stream": "1",
            "begin": compact_time(begin),
            "end": compact_time(end),
            "cln": str(client_type),
            "isp": "0",
            "auth": "1",
            "rnd": str(random.getrandbits(31) if rnd is None else rnd),
            "timestamp": str(int(time.time() * 1000) if timestamp_ms is None else timestamp_ms),
            "etp": "1",
        }
    )
    return f"ysproto://{parsed.netloc}/playback?{urlencode(params)}"
