"""Media browser of each door station.

Media > EZVIZ Video Intercom > door station > Recordings (SD card) > day > clip
                                            > Pictures of events   > day > event

Recordings are the videos on the SD card of the door station; a clip is fetched
through the EZVIZ cloud when it is played. Pictures are what the EZVIZ message
list keeps for each event (ring, unlock, motion).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .clips import ClipJob, part_size, read_chunk
from .const import DOMAIN, HISTORY_DAYS, RECORDING_DAYS
from .history import HistoryItem, day_text, parse_day
from .hub import InstantBellHub
from .recordings import Recording, parse_clip_id

PICTURE_PATH = "/api/" + DOMAIN + "/picture/{entry_id}/{serial}/{day}/{message_id}"
CLIP_PATH = "/api/" + DOMAIN + "/clip/{entry_id}/{serial}/{clip}"
CLIP_START_WAIT = 45.0  # seconds the first viewer waits for the first video to arrive
RECORDINGS = "rec"
PICTURES = "pic"


def _hub_for(hass: HomeAssistant, entry_id: str) -> InstantBellHub | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    hub = getattr(entry, "runtime_data", None) if entry is not None else None
    return hub if isinstance(hub, InstantBellHub) else None


async def async_get_media_source(hass: HomeAssistant) -> InstantBellMediaSource:
    hass.http.register_view(EventPictureView())
    hass.http.register_view(ClipView())
    return InstantBellMediaSource(hass)


class InstantBellMediaSource(MediaSource):
    """Browse the event pictures."""

    name = "EZVIZ Video Intercom"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        if len(parts) == 5 and parts[2] == PICTURES:
            entry_id, serial, _kind, day, message_id = parts
            if _hub_for(self.hass, entry_id) is None:
                raise Unresolvable("The integration is not loaded")
            return PlayMedia(
                PICTURE_PATH.format(entry_id=entry_id, serial=serial, day=day, message_id=message_id),
                "image/jpeg",
            )
        if len(parts) == 5 and parts[2] == RECORDINGS:
            entry_id, serial, _kind, _day, clip = parts
            hub = _hub_for(self.hass, entry_id)
            if hub is None or serial not in hub.serials or parse_clip_id(clip) is None:
                raise Unresolvable("Unknown recording")
            return PlayMedia(CLIP_PATH.format(entry_id=entry_id, serial=serial, clip=clip), "video/mp4")
        raise Unresolvable("Nothing to play here")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        parts = [p for p in (item.identifier or "").split("/") if p]
        if not parts:
            return self._root()
        hub = _hub_for(self.hass, parts[0])
        if hub is None:
            raise BrowseError("The integration is not loaded")
        if len(parts) == 1:
            return self._doors(parts[0], hub)
        serial = parts[1]
        if serial not in hub.serials:
            raise BrowseError("Unknown door station")
        if len(parts) == 2:
            return self._kinds(parts[0], serial, hub)
        kind = parts[2]
        if kind not in (RECORDINGS, PICTURES):
            raise BrowseError("Nothing to browse here")
        if len(parts) == 3:
            return self._days(parts[0], serial, kind, hub)
        day = parse_day(parts[3])
        if day is None:
            raise BrowseError("Unknown day")
        if len(parts) == 4:
            if kind == RECORDINGS:
                return await self._clips(parts[0], serial, day, hub)
            return await self._events(parts[0], serial, day, hub)
        raise BrowseError("Nothing to browse here")

    # -- levels ---------------------------------------------------------

    def _folder(self, identifier: str, title: str, children: list[BrowseMediaSource]) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _child_folder(self, identifier: str, title: str) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
        )

    def _root(self) -> BrowseMediaSource:
        children = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            hub = getattr(entry, "runtime_data", None)
            if isinstance(hub, InstantBellHub):
                children.append(self._child_folder(entry.entry_id, entry.title))
        return self._folder("", self.name, children)

    def _doors(self, entry_id: str, hub: InstantBellHub) -> BrowseMediaSource:
        children = [
            self._child_folder(f"{entry_id}/{serial}", hub.names.get(serial, serial)) for serial in hub.serials
        ]
        return self._folder(entry_id, self.name, children)

    def _kinds(self, entry_id: str, serial: str, hub: InstantBellHub) -> BrowseMediaSource:
        children = [
            self._child_folder(f"{entry_id}/{serial}/{RECORDINGS}", "Recordings (SD card)"),
            self._child_folder(f"{entry_id}/{serial}/{PICTURES}", "Pictures of events"),
        ]
        return self._folder(f"{entry_id}/{serial}", hub.names.get(serial, serial), children)

    def _days(self, entry_id: str, serial: str, kind: str, hub: InstantBellHub) -> BrowseMediaSource:
        today = dt_util.now().date()
        count = RECORDING_DAYS if kind == RECORDINGS else HISTORY_DAYS
        children = []
        for back in range(count):
            day = today - timedelta(days=back)
            children.append(
                self._child_folder(f"{entry_id}/{serial}/{kind}/{day_text(day)}", _day_title(day, today))
            )
        title = "Recordings (SD card)" if kind == RECORDINGS else "Pictures of events"
        return self._folder(f"{entry_id}/{serial}/{kind}", title, children)

    async def _events(self, entry_id: str, serial: str, day: date, hub: InstantBellHub) -> BrowseMediaSource:
        try:
            items = await hub.async_history_day(serial, day)
        except HomeAssistantError as err:
            raise BrowseError(str(err)) from err
        children = []
        for event in items:
            if not event.picture_url:
                continue
            path = PICTURE_PATH.format(
                entry_id=entry_id, serial=serial, day=day_text(day), message_id=event.message_id
            )
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{entry_id}/{serial}/{PICTURES}/{day_text(day)}/{event.message_id}",
                    media_class=MediaClass.IMAGE,
                    media_content_type="image/jpeg",
                    title=event.label(),
                    can_play=True,
                    can_expand=False,
                    thumbnail=path,
                )
            )
        folder = self._folder(
            f"{entry_id}/{serial}/{PICTURES}/{day_text(day)}",
            _day_title(day, dt_util.now().date()),
            children,
        )
        folder.children_media_class = MediaClass.IMAGE
        return folder

    async def _clips(self, entry_id: str, serial: str, day: date, hub: InstantBellHub) -> BrowseMediaSource:
        try:
            recordings = await hub.async_recordings_day(serial, day)
        except HomeAssistantError as err:
            raise BrowseError(str(err)) from err
        try:  # a picture of an event next to the recording serves as its thumbnail
            events = await hub.async_history_day(serial, day)
        except HomeAssistantError:
            events = []
        children = []
        for rec in reversed(recordings):  # newest first, like the pictures
            thumb = _thumbnail(entry_id, serial, day, rec, events)
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{entry_id}/{serial}/{RECORDINGS}/{day_text(day)}/{rec.clip_id}",
                    media_class=MediaClass.VIDEO,
                    media_content_type="video/mp4",
                    title=rec.label(),
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumb,
                )
            )
        folder = self._folder(
            f"{entry_id}/{serial}/{RECORDINGS}/{day_text(day)}",
            _day_title(day, dt_util.now().date()),
            children,
        )
        folder.children_media_class = MediaClass.VIDEO
        return folder


def _thumbnail(
    entry_id: str, serial: str, day: date, rec: Recording, events: list[HistoryItem]
) -> str | None:
    """Picture of the event closest to the start of the recording (within a minute)."""
    best: tuple[float, HistoryItem] | None = None
    for event in events:
        if not event.picture_url or event.occurred is None:
            continue
        gap = abs((event.occurred.replace(tzinfo=None) - rec.begin).total_seconds())
        if gap <= 60 and (best is None or gap < best[0]):
            best = (gap, event)
    if best is None:
        return None
    return PICTURE_PATH.format(
        entry_id=entry_id, serial=serial, day=day_text(day), message_id=best[1].message_id
    )


def _day_title(day: date, today: date) -> str:
    if day == today:
        return f"Today ({day.strftime('%d.%m.')})"
    if day == today - timedelta(days=1):
        return f"Yesterday ({day.strftime('%d.%m.')})"
    return day.strftime("%a %d.%m.%Y")


class EventPictureView(HomeAssistantView):
    """Serves the picture of a past event (needs a login or a signed link)."""

    url = PICTURE_PATH
    name = f"api:{DOMAIN}:picture"
    requires_auth = True

    async def get(
        self, request: web.Request, entry_id: str, serial: str, day: str, message_id: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        hub = _hub_for(hass, entry_id)
        parsed = parse_day(day)
        if hub is None or serial not in hub.serials or parsed is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            data = await hub.async_history_picture(serial, parsed, message_id)
        except HomeAssistantError:
            return web.Response(status=HTTPStatus.BAD_GATEWAY)
        if not data:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


class ClipView(HomeAssistantView):
    """Plays a recording of the SD card (needs a login or a signed link)."""

    url = CLIP_PATH
    name = f"api:{DOMAIN}:clip"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, serial: str, clip: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        hub = _hub_for(hass, entry_id)
        span = parse_clip_id(clip)
        if hub is None or serial not in hub.serials or span is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        begin, end = span
        ready = hub.clips.ready_file(serial, begin, end)
        if ready is not None:
            return web.FileResponse(ready, headers={"Content-Type": "video/mp4"})

        job = hub.clips.job_for(serial, begin, end)
        waited = 0.0
        while part_size(job.part_path) == 0 and job.state != "failed" and not job.done.is_set():
            if waited >= CLIP_START_WAIT:
                return web.Response(status=HTTPStatus.GATEWAY_TIMEOUT, text="The recording did not start in time")
            await asyncio.sleep(0.3)
            waited += 0.3
        ready = hub.clips.ready_file(serial, begin, end)
        if ready is not None:
            return web.FileResponse(ready, headers={"Content-Type": "video/mp4"})
        if job.state == "failed" or part_size(job.part_path) == 0:
            return web.Response(status=HTTPStatus.BAD_GATEWAY, text=job.error or "No video")
        return await _tail(request, hass, job)


async def _tail(request: web.Request, hass: HomeAssistant, job: ClipJob) -> web.StreamResponse:
    """Hand out the clip while it is still arriving."""
    try:
        handle = await hass.async_add_executor_job(job.part_path.open, "rb")
    except OSError:
        return web.Response(status=HTTPStatus.BAD_GATEWAY, text="The recording is not available")
    response = web.StreamResponse(headers={"Content-Type": "video/mp4", "Cache-Control": "no-store"})
    try:
        await response.prepare(request)
        while True:
            finished = job.streaming_finished  # read before the data: nothing is missed
            chunk = await hass.async_add_executor_job(read_chunk, handle)
            if chunk:
                await response.write(chunk)
            elif finished:
                break
            else:
                await asyncio.sleep(0.25)
    finally:
        await hass.async_add_executor_job(handle.close)
    await response.write_eof()
    return response
