from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import parsing as p

BERLIN = ZoneInfo("Europe/Berlin")
SERIAL = "BE0000001-BE0000002"

# Shaped like real messages seen from an HP7 (identifiers changed).
PUSH_UNLOCK = {
    "id": 2115394649430458370,
    "alert": "EZVIZ app unlock the lock",
    "ext": {
        "device_serial": SERIAL,
        "alert_type_code": 10242,
        "time": "2026-10-28 11:45:47",
        "default_pic_url": "",
        "image": "https://apiieu.example/pic/get?fileId=abc",
        "device_name": "Tor",
    },
}
PUSH_RAW_STRING = {
    "id": 2,
    "alert": "EZVIZ app open the gate",
    "ext": "1,2026-10-28 11:46:03," + SERIAL + ",1,10243,,,eyJ=,EZVIZ app,0,id,0,,0,,msg,https://x/pic,Tor,,2401",
}
RING_MESSAGE = {
    "subType": 2701,
    "msgId": "20261028115213270-BE0000001-0-00012",
    "title": "Your doorbell is ringing",
    "from": SERIAL,
    "pic": "https://apiieu.example/pic/get?fileId=def",
    "time": 1793189298000,  # 2026-10-28 12:08:18 as local wall clock counted as UTC
}


def test_push_unlock_event():
    ev = p.event_from_push(PUSH_UNLOCK, BERLIN)
    assert ev.kind == p.KIND_DOOR and ev.code == 10242
    assert ev.serial == SERIAL and ev.source == "push"
    assert ev.picture_url.startswith("https://")
    assert ev.occurred == datetime(2026, 10, 28, 11, 45, 47, tzinfo=BERLIN)


def test_push_from_raw_string_gate():
    ev = p.event_from_push(PUSH_RAW_STRING, BERLIN)
    assert ev.kind == p.KIND_GATE and ev.serial == SERIAL
    assert ev.picture_url == "https://x/pic"


def test_push_unknown_code_is_other_and_missing_serial_is_none():
    odd = {"alert": "x", "ext": {"device_serial": SERIAL, "alert_type_code": 99999}}
    assert p.event_from_push(odd, BERLIN).kind == p.KIND_OTHER
    assert p.event_from_push({"alert": "x", "ext": {}}, BERLIN) is None
    assert p.event_from_push({"alert": "x"}, BERLIN) is None


def test_ring_time_is_relabelled_not_converted():
    ev = p.event_from_message(RING_MESSAGE, BERLIN)
    assert ev.kind == p.KIND_RING and ev.source == "poll"
    # The clock reading is kept: 12:08:18 local, not shifted by the UTC offset.
    assert ev.occurred == datetime(2026, 10, 28, 12, 8, 18, tzinfo=BERLIN)


def test_other_list_entries_are_ignored():
    unlock = dict(RING_MESSAGE, subType=2401, title="EZVIZ app unlock the lock")
    assert p.event_from_message(unlock, BERLIN) is None
    assert p.event_from_message({"subType": "abc"}, BERLIN) is None
    assert p.event_from_message(dict(RING_MESSAGE, **{"from": ""}, deviceSerial=None), BERLIN) is None


def test_same_device_matches_bare_and_composite():
    assert p.same_device("BE0000001-BE0000002", "be0000001-be0000002")
    assert p.same_device("BE0000001", "BE0000001-BE0000002")
    assert not p.same_device("BE0000009-BE0000002", SERIAL)


def test_stale_events():
    ev = p.event_from_message(RING_MESSAGE, BERLIN)
    assert not p.is_stale(ev, ev.occurred + timedelta(seconds=30))
    assert p.is_stale(ev, ev.occurred + timedelta(minutes=10))
    undated = p.BellEvent(SERIAL, p.KIND_RING, None, "", None, "poll")
    assert not p.is_stale(undated, datetime.now(timezone.utc))


def test_deduper_same_message_id_and_close_events():
    d = p.EventDeduper()
    ev = p.event_from_message(RING_MESSAGE, BERLIN)
    now = ev.occurred + timedelta(seconds=5)
    assert d.is_new(ev, now)
    assert not d.is_new(ev, now)  # same message id
    again = p.BellEvent(SERIAL, p.KIND_RING, ev.occurred + timedelta(seconds=4), "", None, "push")
    assert not d.is_new(again, now)  # same ring seen through another channel
    later = p.BellEvent(SERIAL, p.KIND_RING, ev.occurred + timedelta(seconds=40), "", None, "poll", message_id="other")
    assert d.is_new(later, now + timedelta(seconds=40))  # a second ring 40 s later


def test_deduper_kinds_are_independent():
    d = p.EventDeduper()
    t = datetime(2026, 10, 28, 12, 0, 0, tzinfo=BERLIN)
    ring = p.BellEvent(SERIAL, p.KIND_RING, t, "", None, "poll")
    door = p.BellEvent(SERIAL, p.KIND_DOOR, t, "", None, "push")
    assert d.is_new(ring, t) and d.is_new(door, t)


def test_tracker_seeds_first_then_returns_new_oldest_first():
    tr = p.NewMessageTracker()
    old = {"msgId": "1"}
    assert tr.fresh([old]) == []  # first list only fills the memory
    n2, n3 = {"msgId": "2"}, {"msgId": "3"}
    assert tr.fresh([n3, n2, old]) == [n2, n3]  # newest first in, oldest first out
    assert tr.fresh([n3, n2, old]) == []


def test_messages_from_answer():
    assert p.messages_from_answer({"message": [{"a": 1}, "x"]}) == [{"a": 1}]
    assert p.messages_from_answer(None) == []
    assert p.messages_from_answer({"message": None}) == []
