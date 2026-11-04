"""Pairing events with recordings and sorting them into kinds (no Home Assistant needed)."""

from datetime import datetime, timezone

from history import HistoryItem
from recordings import Recording
from visitors import KIND_OTHER, KIND_RING, KIND_UNLOCK, clip_for, kind_of

def item(subtype):
    return HistoryItem("m", "S", None, "t", None, subtype)


def test_kinds():
    assert kind_of(item(2701)) == KIND_RING
    assert kind_of(item(2401)) == KIND_UNLOCK
    assert kind_of(item(None)) == KIND_OTHER
    assert kind_of(item(9999)) == KIND_OTHER


def recording(begin, end):
    return Recording(begin, end, "")


def test_event_inside_a_recording_gets_it_even_far_from_its_start():
    r = recording(datetime(2026, 10, 28, 15, 0, 0), datetime(2026, 10, 28, 15, 10, 0))
    event_time = datetime(2026, 10, 28, 15, 8, 0, tzinfo=timezone.utc)  # the zone is ignored
    assert clip_for(event_time, [r]) is r


def test_event_just_before_a_recording_gets_it_but_not_a_distant_one():
    r = recording(datetime(2026, 10, 28, 15, 42, 3), datetime(2026, 10, 28, 15, 42, 52))
    assert clip_for(datetime(2026, 10, 28, 15, 41, 40), [r]) is r  # 23 s before
    assert clip_for(datetime(2026, 10, 28, 15, 40, 0), [r]) is None  # 123 s before
    assert clip_for(datetime(2026, 10, 28, 15, 44, 0), [r]) is None  # after it ended


def test_a_running_recording_beats_one_that_is_about_to_start_and_the_nearest_start_wins():
    running = recording(datetime(2026, 10, 28, 15, 0, 0), datetime(2026, 10, 28, 15, 5, 0))
    soon = recording(datetime(2026, 10, 28, 15, 4, 20), datetime(2026, 10, 28, 15, 6, 0))
    assert clip_for(datetime(2026, 10, 28, 15, 4, 0), [soon, running]) is running
    both = clip_for(datetime(2026, 10, 28, 15, 4, 50), [running, soon])
    assert both is soon  # both are running: the one that began closest to the event
    assert clip_for(None, [running]) is None
    assert clip_for(datetime(2026, 10, 28, 15, 1, 0), []) is None
