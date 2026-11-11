"""Live video camera entity for the HP7 / CP7, fed by the stream relay (see ``live_relay``)."""
# Adapted for EZVIZ HP7 / CP7 Video Intercom from the ezviz_hp7 integration by Bobsilvio
# (MIT, https://github.com/Bobsilvio/ezviz_hp7); see THIRD_PARTY_LICENSES.md.
# The local-network video protocol and the motion-JPEG view are by albrzmr (MIT).
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, LEGACY_DOMAIN
from .live_relay import StreamRelay

if TYPE_CHECKING:
    from .hub import InstantBellHub

_LOGGER = logging.getLogger(__name__)


class LiveCamera(Camera):
    """Live H.264 + AAC stream from the HP7/CP7 via the VTM cloud relay."""

    _attr_has_entity_name = True
    _attr_translation_key = "live"

    def __init__(
        self,
        hub: "InstantBellHub",
        serial: str,
        relay: StreamRelay,
        stream_mode: str = "mjpeg",
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        super().__init__()
        self._hub = hub
        self._serial = serial
        self._relay = relay
        self._stream_mode = (stream_mode or "mjpeg").lower()
        self._ffmpeg_path = ffmpeg_path
        # Same unique ID as the live video entity of the earlier ezviz_hp7 integration.
        self._attr_unique_id = f"{LEGACY_DOMAIN}_{serial}_live"

    @property
    def device_info(self) -> DeviceInfo:
        from .entity import door_device_info

        return door_device_info(self._hub, self._serial)

    @property
    def supported_features(self) -> int:
        from homeassistant.components.camera import CameraEntityFeature

        # In MJPEG mode we don't advertise STREAM: HA would otherwise try to
        # run its HLS/WebRTC pipeline (go2rtc) instead of calling our
        # handle_async_mjpeg_stream. The MJPEG path is codec-agnostic and
        # avoids the go2rtc/HEVC issues entirely.
        if self._stream_mode == "mjpeg":
            return CameraEntityFeature(0)
        return CameraEntityFeature.STREAM

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Surface what's actually driving the live view (source/mode/codec).

        Lets the user see — and put an on-screen badge for — whether they're
        on local vs cloud, WebRTC vs MJPEG, and H.264 vs HEVC, which is
        otherwise invisible with source=auto / mode=auto.
        """
        codec = self._relay.detected_codec
        if not codec:
            cfg = self._relay._video_codec
            codec = cfg if cfg and cfg != "auto" else "unknown"
        return {
            "stream_source": self._relay.active_source,
            "stream_mode": self._stream_mode,
            "video_codec": codec,
        }

    async def stream_source(self) -> Optional[str]:
        if self._stream_mode == "mjpeg":
            return None
        if self._relay.port == 0:
            return None
        return self._relay.stream_url

    async def handle_async_mjpeg_stream(self, request):
        """Serve a per-viewer MJPEG transcode of the live stream.

        Used when stream_mode == 'mjpeg' — ffmpeg decodes whatever codec
        the relay carries (H.264 or HEVC) into motion-JPEG, sidestepping
        go2rtc/WebRTC and the HEVC grey-screen. Falls back to the base
        implementation otherwise.
        """
        if self._stream_mode != "mjpeg":
            return await super().handle_async_mjpeg_stream(request)
        # Make sure the relay (and its shared upstream) is warm before we
        # point ffmpeg at it.
        try:
            await self._relay.prewarm()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("LiveCamera: mjpeg prewarm failed: %s", exc)
        if self._relay.port == 0:
            return None
        from .mjpeg import serve_mjpeg

        return await serve_mjpeg(
            request,
            ffmpeg_path=self._ffmpeg_path,
            upstream_url=self._relay.stream_url,
        )

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a snapshot grabbed from the live stream.

        The frontend tile / Lovelace previews ask for a still image even on
        stream-only cameras; the base Camera class raises NotImplementedError
        if we don't override this. The relay encodes the latest keyframe of
        the running session into a JPEG once per keyframe (#22), so cards
        that refresh every second no longer start an ffmpeg each time, and
        no second cloud session is opened just for a thumbnail.
        """
        if self._relay.port == 0:
            return None
        return await self._relay.still_image()


async def async_setup_live_entities(
    hass: HomeAssistant,
    hub: "InstantBellHub",
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Start one stream relay per door station and add its live camera."""
    from .keys import key_for
    from .const import (
        CONF_AGGRESSIVE_MPEGTS,
        CONF_DIAGNOSTIC_DUMPS,
        CONF_KEEP_STREAM_OPEN,
        CONF_RELAY_BIND,
        CONF_RELAY_PORT,
        CONF_STREAM_MODE,
        CONF_STREAM_QUALITY,
        CONF_STREAM_SOURCE,
        CONF_VIDEO_CODEC,
    )

    options = hub.entry.options
    stream_mode = str(options.get(CONF_STREAM_MODE) or "auto")
    entities: list[LiveCamera] = []
    # A fixed relay port can only serve one door station.
    for index, serial in enumerate(hub.serials):
        relay = StreamRelay(
            api=hub.stream,
            serial=serial,
            channel=1,
            listen_port=int(options.get(CONF_RELAY_PORT) or 0) if index == 0 else 0,
            bind_host=str(options.get(CONF_RELAY_BIND) or "127.0.0.1"),
            aggressive_mpegts=bool(options.get(CONF_AGGRESSIVE_MPEGTS, False)),
            video_codec=str(options.get(CONF_VIDEO_CODEC) or "auto"),
            stream_source=str(options.get(CONF_STREAM_SOURCE) or "cloud"),
            stream_mode=stream_mode,
            stream_quality=str(options.get(CONF_STREAM_QUALITY) or "main"),
            diagnostic_dumps=bool(options.get(CONF_DIAGNOSTIC_DUMPS, False)),
            encryption_key=key_for(options, serial),
            keep_open=bool(options.get(CONF_KEEP_STREAM_OPEN, False)),
            hass=hass,
        )
        await relay.start()
        hub.live_relays[serial] = relay

        # "auto": probe the video codec once and pick the delivery mode:
        # H.264 -> webrtc (audio, low latency), HEVC -> mjpeg (browsers cannot
        # show HEVC over WebRTC). If the codec cannot be found out (device
        # offline), mjpeg always works.
        effective_mode = stream_mode
        if stream_mode == "auto":
            effective_mode = await _resolve_auto_stream_mode(relay)
            relay._stream_mode = effective_mode
            hub.stream_modes[serial] = effective_mode

        # Only WebRTC can hit the HEVC-can't-play situation.
        if effective_mode != "webrtc":
            try:
                from homeassistant.helpers import issue_registry as ir

                ir.async_delete_issue(hass, DOMAIN, f"hevc_webrtc_{serial}")
            except Exception:  # noqa: BLE001
                pass
        entities.append(LiveCamera(hub, serial, relay, stream_mode=effective_mode))
    async_add_entities(entities)


async def _resolve_auto_stream_mode(relay: "StreamRelay") -> str:
    """Probe the video codec and map it to a delivery mode.

    Returns "webrtc" for H.264, "mjpeg" for HEVC or when the codec can't be
    sniffed within the timeout (mjpeg is the safe, codec-agnostic default).
    """
    from .const import STREAM_MODE_MJPEG, STREAM_MODE_WEBRTC

    try:
        await relay.prewarm()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("StreamRelay: auto-probe prewarm failed: %s", exc)
        return STREAM_MODE_MJPEG

    # Poll up to ~10 s for the broadcast reader to sniff the first keyframe.
    codec: Optional[str] = None
    for _ in range(50):
        codec = relay.detected_codec
        if codec is not None:
            break
        await asyncio.sleep(0.2)

    mode = STREAM_MODE_WEBRTC if codec == "h264" else STREAM_MODE_MJPEG
    _LOGGER.info(
        "StreamRelay: auto stream mode — detected codec=%s → %s (serial=%s)",
        codec or "unknown", mode, relay._serial,
    )
    return mode
