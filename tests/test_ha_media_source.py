"""The media browser: levels, wrong addresses, thumbnails, and the picture and clip views."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source import MediaSourceItem, Unresolvable
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.ezviz_instant_bell import media_source
from custom_components.ezviz_instant_bell.clips import ClipJob
from custom_components.ezviz_instant_bell.const import DOMAIN, HISTORY_DAYS
from custom_components.ezviz_instant_bell.history import HistoryItem
from custom_components.ezviz_instant_bell.media_source import InstantBellMediaSource, _day_title, _thumbnail
from custom_components.ezviz_instant_bell.recordings import Recording

ENTRY = "entry1"
SERIAL = "BE0000001-BE0000002"
CLIP = "20261028T154203Z_20261028T154252Z"
DAY = date(2026, 10, 28)


def event(message_id: str, when: datetime | None, picture: str | None = "https://example.invalid/p") -> HistoryItem:
    return HistoryItem(message_id, SERIAL, when, "Your doorbell is ringing", picture, 2701)


class FakeClips:
    def __init__(self, tmp_path) -> None:
        self.job = ClipJob(part_path=tmp_path / "c.part.mp4", final_path=tmp_path / "c.mp4")

    async def async_ready_file(self, serial, begin, end):
        return None

    def job_for(self, serial, begin, end):
        return self.job


class FakeHub:
    def __init__(self, tmp_path) -> None:
        self.serials = [SERIAL]
        self.names = {SERIAL: "Front door"}
        self.events: list[HistoryItem] = []
        self.recordings: list[Recording] = []
        self.fail_history = self.fail_recordings = False
        self.picture: bytes | None = b"JPEG"
        self.fail_picture = False
        self.clips = FakeClips(tmp_path)

    async def async_history_day(self, serial, day):
        if self.fail_history:
            raise HomeAssistantError("message list unavailable")
        return list(self.events)

    async def async_recordings_day(self, serial, day):
        if self.fail_recordings:
            raise HomeAssistantError("recordings unavailable")
        return list(self.recordings)

    async def async_history_picture(self, serial, day, message_id):
        if self.fail_picture:
            raise HomeAssistantError("cloud down")
        return self.picture


@pytest.fixture
def hub(tmp_path):
    fake = FakeHub(tmp_path)
    with patch.object(media_source, "_hub_for", lambda _hass, entry_id: fake if entry_id == ENTRY else None):
        yield fake


async def browse(hass: HomeAssistant, identifier: str):
    return await InstantBellMediaSource(hass).async_browse_media(MediaSourceItem(hass, DOMAIN, identifier, None))


async def resolve(hass: HomeAssistant, identifier: str):
    return await InstantBellMediaSource(hass).async_resolve_media(MediaSourceItem(hass, DOMAIN, identifier, None))


@pytest.mark.parametrize(
    "identifier",
    [
        "other-entry",
        f"{ENTRY}/UNKNOWN",
        f"{ENTRY}/{SERIAL}/video",
        f"{ENTRY}/{SERIAL}/pic/2026-10-28",
        f"{ENTRY}/{SERIAL}/pic/20261028/m1",
    ],
)
async def test_wrong_addresses_cannot_be_browsed(hass: HomeAssistant, hub: FakeHub, identifier: str) -> None:
    with pytest.raises(BrowseError):
        await browse(hass, identifier)


async def test_door_stations_are_listed_by_their_names(hass: HomeAssistant, hub: FakeHub) -> None:
    doors = await browse(hass, ENTRY)
    assert [(c.identifier, c.title) for c in doors.children] == [(f"{ENTRY}/{SERIAL}", "Front door")]


async def test_recordings_offer_the_last_days_newest_first(hass: HomeAssistant, hub: FakeHub) -> None:
    days = await browse(hass, f"{ENTRY}/{SERIAL}/rec")
    today = dt_util.now().date()
    assert len(days.children) == HISTORY_DAYS and days.title == "Recordings (SD card)"
    assert days.children[0].identifier == f"{ENTRY}/{SERIAL}/rec/{today.strftime('%Y%m%d')}"
    assert days.children[0].title.startswith("Today") and days.children[1].title.startswith("Yesterday")


async def test_events_without_a_picture_are_left_out(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.events = [event("with", datetime(2026, 10, 28, 9, 0)), event("without", datetime(2026, 10, 28, 8, 0), None)]
    pictures = await browse(hass, f"{ENTRY}/{SERIAL}/pic/20261028")
    assert [c.identifier.rsplit("/", 1)[1] for c in pictures.children] == ["with"]
    assert pictures.children_media_class == "image"


async def test_a_failing_message_list_is_a_browse_error(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.fail_history = True
    with pytest.raises(BrowseError, match="message list unavailable"):
        await browse(hass, f"{ENTRY}/{SERIAL}/pic/20261028")


async def test_recordings_are_listed_without_thumbnails_when_the_message_list_fails(
    hass: HomeAssistant, hub: FakeHub
) -> None:
    hub.fail_history = True
    hub.recordings = [Recording(datetime(2026, 10, 28, 15, 42, 3), datetime(2026, 10, 28, 15, 42, 52))]
    clips = await browse(hass, f"{ENTRY}/{SERIAL}/rec/20261028")
    assert [c.identifier for c in clips.children] == [f"{ENTRY}/{SERIAL}/rec/20261028/{CLIP}"]
    assert clips.children[0].thumbnail is None


async def test_a_failing_recordings_list_is_a_browse_error(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.fail_recordings = True
    with pytest.raises(BrowseError, match="recordings unavailable"):
        await browse(hass, f"{ENTRY}/{SERIAL}/rec/20261028")


def test_the_thumbnail_is_the_picture_closest_to_the_start_of_the_recording() -> None:
    rec = Recording(datetime(2026, 10, 28, 15, 42, 3), datetime(2026, 10, 28, 15, 42, 52))
    events = [
        event("far", datetime(2026, 10, 28, 15, 41, 30)),
        event("close", datetime(2026, 10, 28, 15, 42, 5, tzinfo=dt_util.UTC)),
        event("closest-no-picture", datetime(2026, 10, 28, 15, 42, 3), None),
        event("no-time", None),
    ]
    assert _thumbnail(ENTRY, SERIAL, DAY, rec, events) == f"/api/{DOMAIN}/picture/{ENTRY}/{SERIAL}/20261028/close"


def test_no_thumbnail_when_no_picture_is_within_a_minute() -> None:
    rec = Recording(datetime(2026, 10, 28, 15, 42, 3), datetime(2026, 10, 28, 15, 42, 52))
    events = [event("early", datetime(2026, 10, 28, 15, 41, 2)), event("late", datetime(2026, 10, 28, 15, 43, 4))]
    assert _thumbnail(ENTRY, SERIAL, DAY, rec, events) is None


def test_day_titles() -> None:
    assert _day_title(DAY, DAY) == "Today (28.10.)"
    assert _day_title(DAY - timedelta(days=1), DAY) == "Yesterday (27.10.)"
    assert _day_title(DAY - timedelta(days=2), DAY) == "Mon 26.10.2026"


@pytest.mark.parametrize(
    "identifier",
    [
        f"{ENTRY}/{SERIAL}/rec/20261028",
        f"{ENTRY}/UNKNOWN/rec/20261028/{CLIP}",
        f"{ENTRY}/{SERIAL}/rec/20261028/not-a-clip",
        f"{ENTRY}/{SERIAL}/rec/20261028/20261028T154252Z_20261028T154203Z",
        f"other-entry/{SERIAL}/pic/20261028/m1",
        f"other-entry/{SERIAL}/rec/20261028/{CLIP}",
    ],
)
async def test_wrong_addresses_cannot_be_played(hass: HomeAssistant, hub: FakeHub, identifier: str) -> None:
    with pytest.raises(Unresolvable):
        await resolve(hass, identifier)


# --- the web views -------------------------------------------------------------------


@pytest.fixture
async def http(hass: HomeAssistant, hub: FakeHub, hass_client):
    assert await async_setup_component(hass, "http", {})
    media_source.register_views(hass)
    media_source.register_views(hass)  # a second call adds nothing
    return await hass_client()


async def test_picture_view_answers_for_cloud_errors_and_missing_pictures(http, hub: FakeHub) -> None:
    url = f"/api/{DOMAIN}/picture/{ENTRY}/{SERIAL}/20261028/m1"
    response = await http.get(url)
    assert response.status == 200 and await response.read() == b"JPEG"
    hub.fail_picture = True
    assert (await http.get(url)).status == 502
    hub.fail_picture, hub.picture = False, None
    assert (await http.get(url)).status == 404
    assert (await http.get(f"/api/{DOMAIN}/picture/{ENTRY}/{SERIAL}/yesterday/m1")).status == 404


async def test_clip_view_gives_up_when_the_recording_does_not_start(http, hub: FakeHub) -> None:
    with patch.object(media_source, "CLIP_START_WAIT", 0.5):
        response = await http.get(f"/api/{DOMAIN}/clip/{ENTRY}/{SERIAL}/{CLIP}")
    assert response.status == 504


async def test_clip_view_reports_why_a_fetch_failed(http, hub: FakeHub) -> None:
    hub.clips.job.state, hub.clips.job.error = "failed", "The door station is busy"
    response = await http.get(f"/api/{DOMAIN}/clip/{ENTRY}/{SERIAL}/{CLIP}")
    assert response.status == 502 and await response.text() == "The door station is busy"


async def test_clip_view_hands_out_a_clip_that_ended_while_arriving(http, hub: FakeHub) -> None:
    job = hub.clips.job
    job.part_path.write_bytes(b"GROWING")
    job.state = "finalizing"
    response = await http.get(f"/api/{DOMAIN}/clip/{ENTRY}/{SERIAL}/{CLIP}")
    assert response.status == 200 and await response.read() == b"GROWING"
    assert response.headers["Cache-Control"] == "no-store"
