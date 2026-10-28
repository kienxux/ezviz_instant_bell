"""Recordings of the SD card: list format and playback address (no Home Assistant needed)."""

from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from recordings import (
    MAX_CLIP_SECONDS,
    build_playback_url,
    channel_serial,
    clip_id,
    compact_time,
    day_window,
    format_length,
    parse_clip_id,
    recordings_from_answer,
)

LIVE = (
    "ysproto://203.0.113.5:8554/live?biz=1&dev=OLD&chn=1&stream=1&cln=9&isp=0&auth=1&ssn=TOKEN&vip=0&timestamp=1"
)


def rec(begin, end, kind="TIMING"):
    return {"channelType": "D", "begin": begin, "end": end, "type": kind}


def test_list_is_oldest_first_without_repeats_and_without_junk():
    answer = {
        "records": [
            rec("2026-10-28T15:42:03", "2026-10-28T15:42:52"),
            rec("2026-10-28T08:00:00", "2026-10-28T08:00:30"),
            rec("2026-10-28T15:42:03", "2026-10-28T15:42:52"),
            rec("nonsense", "2026-10-28T08:00:30"),
            rec("2026-10-28T09:00:00", "2026-10-28T08:59:00"),
            "junk",
        ]
    }
    items = recordings_from_answer(answer)
    assert [i.begin.strftime("%H:%M:%S") for i in items] == ["08:00:00", "15:42:03"]
    assert items[1].seconds == 49 and items[1].label() == "15:42:03  (49 s)"
    assert recordings_from_answer(None) == [] and recordings_from_answer({"records": None}) == []


def test_lengths():
    assert format_length(49) == "49 s" and format_length(125) == "2 min 05 s" and format_length(-3) == "0 s"


def test_times_are_the_local_clock_with_a_z():
    assert compact_time(datetime(2026, 10, 28, 15, 42, 3)) == "20261028T154203Z"


def test_clip_id_round_trip_and_checks():
    begin, end = datetime(2026, 10, 28, 15, 42, 3), datetime(2026, 10, 28, 15, 42, 52)
    assert clip_id(begin, end) == "20261028T154203Z_20261028T154252Z"
    assert parse_clip_id(clip_id(begin, end)) == (begin, end)
    assert parse_clip_id("") is None and parse_clip_id("abc") is None
    assert parse_clip_id("20261028T154252Z_20261028T154203Z") is None  # backwards
    too_long = datetime(2026, 10, 28, 16, 42, 3)
    assert (too_long - begin).total_seconds() > MAX_CLIP_SECONDS
    assert parse_clip_id(clip_id(begin, too_long)) is None


def test_day_window_and_channel_serial():
    assert day_window(date(2026, 10, 28)) == ("2026-10-28T00:00:00", "2026-10-28T23:59:59")
    assert channel_serial("BE9421436-BE9140874") == "BE9140874" and channel_serial("ABC") == "ABC"


def test_playback_address():
    begin, end = datetime(2026, 10, 28, 15, 42, 3), datetime(2026, 10, 28, 15, 42, 52)
    url = build_playback_url(LIVE, "MAIN-CAM", 1, begin, end, timestamp_ms=1234, rnd=99)
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("ysproto", "203.0.113.5:8554", "/playback")
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q["begin"] == "20261028T154203Z" and q["end"] == "20261028T154252Z"
    assert q["dev"] == q["serial"] == "MAIN-CAM" and q["chn"] == "1" and q["stream"] == "1"
    assert q["cln"] == "3" and q["etp"] == "1" and q["rnd"] == "99" and q["timestamp"] == "1234"
    assert q["ssn"] == "TOKEN" and q["biz"] == "1"  # kept from the live address
    assert "vip" not in q
