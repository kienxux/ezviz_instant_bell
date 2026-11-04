"""Websocket call that feeds the visitor log card, and the card's script file."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN, HISTORY_DAYS
from .history import day_text
from .hub import InstantBellHub
from .media_source import CLIP_PATH, PICTURE_PATH, register_views
from .parsing import same_device
from .visitors import clip_for, kind_of

_LOGGER = logging.getLogger(__name__)

CARD_URL = f"/{DOMAIN}_static/ezviz-visitor-log-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / "ezviz-visitor-log-card.js"
SIGN_SECONDS = 600
MAX_DAYS = 7


async def async_setup_card(hass: HomeAssistant) -> None:
    """Register the websocket call and serve the card (the card is added to the dashboards' resources)."""
    register_views(hass)
    websocket_api.async_register_command(hass, ws_visitor_log)
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), False)])
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, CARD_URL)
    else:  # headless install: the file is still served
        _LOGGER.debug("Frontend not loaded, the visitor log card is not added to the pages")


def _hubs(hass: HomeAssistant) -> list[InstantBellHub]:
    found = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        hub = getattr(entry, "runtime_data", None)
        if isinstance(hub, InstantBellHub):
            found.append(hub)
    return found


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/visitor_log",
        vol.Optional("serial"): str,
        vol.Optional("days", default=1): vol.All(int, vol.Range(min=1, max=MAX_DAYS)),
    }
)
@websocket_api.async_response
async def ws_visitor_log(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Events of the last days (newest first), each with a picture and a recording when there is one."""
    wanted = str(msg.get("serial") or "").strip()
    doors: list[tuple[InstantBellHub, str]] = []
    for hub in _hubs(hass):
        for serial in hub.serials:
            if not wanted or serial == wanted or same_device(serial, wanted):
                doors.append((hub, serial))
    if not doors:
        connection.send_error(msg["id"], "not_found", "No such door station")
        return

    today = dt_util.now().date()
    days = min(int(msg["days"]), HISTORY_DAYS)

    def signed(path: str) -> str:
        return async_sign_path(
            hass, path, timedelta(seconds=SIGN_SECONDS), refresh_token_id=connection.refresh_token_id
        )

    items: list[dict] = []
    failed = False
    for hub, serial in doors:
        entry_id = hub.entry.entry_id
        for back in range(days):
            day = today - timedelta(days=back)
            try:
                events = await hub.async_history_day(serial, day)
            except HomeAssistantError:
                failed = True
                continue
            try:  # without the SD-card list the log still works, just without clips
                recordings = await hub.async_recordings_day(serial, day)
            except HomeAssistantError:
                recordings = []
            for event in events:
                picture = clip = None
                if event.picture_url:
                    picture = signed(
                        PICTURE_PATH.format(
                            entry_id=entry_id, serial=serial, day=day_text(day), message_id=event.message_id
                        )
                    )
                rec = clip_for(event.occurred, recordings)
                if rec is not None:
                    clip = signed(CLIP_PATH.format(entry_id=entry_id, serial=serial, clip=rec.clip_id))
                items.append(
                    {
                        "id": event.message_id,
                        "serial": serial,
                        "name": hub.names.get(serial, serial),
                        "kind": kind_of(event),
                        "time": event.occurred.isoformat() if event.occurred else None,
                        "title": event.title,
                        "picture": picture,
                        "clip": clip,
                        "clip_seconds": rec.seconds if rec else None,
                    }
                )
    items.sort(key=lambda i: i["time"] or "", reverse=True)
    connection.send_result(
        msg["id"],
        {
            "doors": [{"serial": s, "name": h.names.get(s, s)} for h, s in doors],
            "items": items,
            "partial": failed,
        },
    )
