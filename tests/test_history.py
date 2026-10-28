"""Reading the cloud message list for the media browser (no Home Assistant needed)."""

from datetime import timezone

from history import day_text, item_from_message, items_from_answer, parse_day

TZ = timezone.utc


def message(msg_id, ms, title="Your doorbell is ringing", pic="https://example.invalid/p", sub=2701):
    return {"msgId": msg_id, "time": ms, "title": title, "pic": pic, "subType": sub, "from": "BE1-BE2"}


def test_newest_first_without_repeats():
    answer = {"message": [message("a", 1_000_000), message("b", 5_000_000), message("a", 1_000_000)]}
    items = items_from_answer(answer, TZ)
    assert [i.message_id for i in items] == ["b", "a"]


def test_entries_without_id_are_dropped():
    assert items_from_answer({"message": [{"title": "x"}, "junk"]}, TZ) == []
    assert items_from_answer(None, TZ) == []


def test_label_and_picture():
    item = item_from_message(message("a", 3_723_000), TZ)
    assert item.label() == "01:02:03  Your doorbell is ringing"
    assert item.picture_url and item.subtype == 2701
    assert item_from_message(message("b", 1, pic=""), TZ).picture_url is None


def test_day_text_round_trip():
    day = parse_day("20261028")
    assert day_text(day) == "20261028"
    assert parse_day("nonsense") is None
