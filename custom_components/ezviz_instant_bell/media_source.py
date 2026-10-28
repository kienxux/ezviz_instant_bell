"""Media browser: pictures of past events (rings, unlocks, motion) of each door station.

Media > EZVIZ Video Intercom > door station > day > event. Only what the EZVIZ
cloud keeps in its message list is offered (a picture per event).
"""

from __future__ import annotations

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

from .const import DOMAIN, HISTORY_DAYS
from .history import day_text, parse_day
from .hub import InstantBellHub

PICTURE_PATH = "/api/" + DOMAIN + "/picture/{entry_id}/{serial}/{day}/{message_id}"


def _hub_for(hass: HomeAssistant, entry_id: str) -> InstantBellHub | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    hub = getattr(entry, "runtime_data", None) if entry is not None else None
    return hub if isinstance(hub, InstantBellHub) else None


async def async_get_media_source(hass: HomeAssistant) -> InstantBellMediaSource:
    hass.http.register_view(EventPictureView())
    return InstantBellMediaSource(hass)


class InstantBellMediaSource(MediaSource):
    """Browse the event pictures."""

    name = "EZVIZ Video Intercom"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        if len(parts) != 4:
            raise Unresolvable("Not a picture")
        entry_id, serial, day, message_id = parts
        if _hub_for(self.hass, entry_id) is None:
            raise Unresolvable("The integration is not loaded")
        return PlayMedia(
            PICTURE_PATH.format(entry_id=entry_id, serial=serial, day=day, message_id=message_id), "image/jpeg"
        )

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
            return self._days(parts[0], serial, hub)
        day = parse_day(parts[2])
        if day is None:
            raise BrowseError("Unknown day")
        if len(parts) == 3:
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

    def _days(self, entry_id: str, serial: str, hub: InstantBellHub) -> BrowseMediaSource:
        today = dt_util.now().date()
        children = []
        for back in range(HISTORY_DAYS):
            day = today - timedelta(days=back)
            children.append(self._child_folder(f"{entry_id}/{serial}/{day_text(day)}", _day_title(day, today)))
        return self._folder(f"{entry_id}/{serial}", hub.names.get(serial, serial), children)

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
                    identifier=f"{entry_id}/{serial}/{day_text(day)}/{event.message_id}",
                    media_class=MediaClass.IMAGE,
                    media_content_type="image/jpeg",
                    title=event.label(),
                    can_play=True,
                    can_expand=False,
                    thumbnail=path,
                )
            )
        folder = self._folder(
            f"{entry_id}/{serial}/{day_text(day)}",
            _day_title(day, dt_util.now().date()),
            children,
        )
        folder.children_media_class = MediaClass.IMAGE
        return folder


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
