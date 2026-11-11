"""The live video for other apps (go2rtc, Frigate), behind a token.

The relay itself is a raw TCP socket without a password, so it stays on
127.0.0.1. These two views hand the same stream out through Home Assistant's
web server instead, and only when the option is on and the request carries the
entry's token:

    /api/ezviz_instant_bell/live/<serial>?token=...   MPEG-TS (video and sound)
    /api/ezviz_instant_bell/mjpeg/<serial>?token=...  motion JPEG

A wrong token counts as a failed login, so Home Assistant's IP ban applies.
The right one never does, also not while the account is still starting.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
from http import HTTPStatus
from typing import TYPE_CHECKING

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.ban import process_wrong_login
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import CONF_SHARE_STREAM, CONF_STREAM_TOKEN, DOMAIN
from .parsing import same_device

if TYPE_CHECKING:
    from .live_relay import StreamRelay

_LOGGER = logging.getLogger(__name__)

LIVE_PATH = f"/api/{DOMAIN}/live/{{serial}}"
MJPEG_PATH = f"/api/{DOMAIN}/mjpeg/{{serial}}"
CHUNK = 64 * 1024


def new_token() -> str:
    return secrets.token_urlsafe(32)


def stream_addresses(hass: HomeAssistant, serials: list[str], token: str) -> str:
    """The addresses of every door station, one per line, for the options form."""
    try:
        base = get_url(hass, allow_external=False)
    except NoURLAvailableError:
        base = "http://<home-assistant>:8123"
    lines = []
    for serial in serials:
        for path in (LIVE_PATH, MJPEG_PATH):
            lines.append(f"{base}{path.format(serial=serial)}?token={token}")
    return "\n".join(lines)


def register_share_views(hass: HomeAssistant) -> None:
    hass.http.register_view(LiveShareView())
    hass.http.register_view(MjpegShareView())


def _account(hass: HomeAssistant, token: str) -> ConfigEntry | None:
    """The account the token belongs to. It is kept in the options, so this works before setup."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        wanted = str(entry.options.get(CONF_STREAM_TOKEN) or "")
        if wanted and hmac.compare_digest(token.encode(), wanted.encode()):
            return entry
    return None


def _relay(entry: ConfigEntry, serial: str) -> StreamRelay | None:
    relays = getattr(getattr(entry, "runtime_data", None), "live_relays", None) or {}
    for known, relay in relays.items():
        if known == serial or same_device(known, serial):
            return relay
    return None


class _ShareView(HomeAssistantView):
    """Checks the token and the option; the subclass sends the stream."""

    requires_auth = False

    async def get(self, request: web.Request, serial: str) -> web.StreamResponse:
        entry = _account(request.app["hass"], request.query.get("token", ""))
        if entry is None:
            await process_wrong_login(request)
            return web.Response(status=HTTPStatus.UNAUTHORIZED)
        # From here on the token is right, so nothing counts as a failed login:
        # go2rtc and Frigate keep trying, and their host would get banned.
        if not entry.options.get(CONF_SHARE_STREAM):
            return web.Response(status=HTTPStatus.FORBIDDEN)
        if entry.state is not ConfigEntryState.LOADED:
            # Home Assistant is starting, or the account waits for the cloud.
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        if (relay := _relay(entry, serial)) is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return await self._send(request, relay)

    async def _send(self, request: web.Request, relay: StreamRelay) -> web.StreamResponse:
        raise NotImplementedError


class LiveShareView(_ShareView):
    """The relay's MPEG-TS, copied byte for byte."""

    url = LIVE_PATH
    name = f"api:{DOMAIN}:share_live"

    async def _send(self, request: web.Request, relay: StreamRelay) -> web.StreamResponse:
        if relay.port == 0:
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        except OSError as err:
            _LOGGER.debug("Shared stream: relay not reachable: %s", err)
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        response = web.StreamResponse(
            headers={"Content-Type": "video/mp2t", "Cache-Control": "no-cache, no-store"}
        )
        _LOGGER.info("Shared stream START client=%s", request.remote)
        try:
            await response.prepare(request)
            while chunk := await reader.read(CHUNK):
                await response.write(chunk)
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            _LOGGER.info("Shared stream END client=%s", request.remote)
        return response


class MjpegShareView(_ShareView):
    """Motion JPEG of the relay's stream, as the live camera serves it."""

    url = MJPEG_PATH
    name = f"api:{DOMAIN}:share_mjpeg"

    async def _send(self, request: web.Request, relay: StreamRelay) -> web.StreamResponse:
        try:
            await relay.prewarm()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Shared stream: mjpeg prewarm failed: %s", exc)
        if relay.port == 0:
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        from .mjpeg import serve_mjpeg

        return await serve_mjpeg(
            request, ffmpeg_path="ffmpeg", upstream_url=relay.stream_url, probe_us=relay.viewer_probe
        )
