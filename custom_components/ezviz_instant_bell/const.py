"""Constants for Instant Bell for EZVIZ."""

from __future__ import annotations

DOMAIN = "ezviz_instant_bell"

CONF_REGION = "region"
CONF_DEVICES = "devices"
CONF_NAMES = "names"
CONF_POLL_INTERVAL = "poll_interval"
CONF_DOOR_LOCK_NO = "door_lock_no"
CONF_GATE_LOCK_NO = "gate_lock_no"
CONF_MONITOR_SERIALS = "monitor_serials"
# Option of version 0.1.0: switched on the "Open door" / "Open gate" buttons.
CONF_CREATE_BUTTONS = "create_buttons"

DEFAULT_REGION = "apiieu.ezvizlife.com"
REGIONS = {
    "apiieu.ezvizlife.com": "Europe",
    "apiius.ezvizlife.com": "North America",
    "apiisgp.ezvizlife.com": "Asia",
}

DEFAULT_POLL_INTERVAL = 5  # seconds between checks of the cloud message list
MIN_POLL_INTERVAL = 2
MAX_POLL_INTERVAL = 60  # a ring is announced up to this late; longer is not useful

# Lock numbers seen on an HP7: 2 opens the door, 1 opens the gate.
DEFAULT_DOOR_LOCK_NO = 2
DEFAULT_GATE_LOCK_NO = 1

# Status (sensors, switches ...) is read this often; a push or a ring refreshes it at once.
UPDATE_INTERVAL = 15
# Failed status reads in a row that are hidden by keeping the last values.
STATUS_GRACE_CYCLES = 4

# Unique IDs of the entities that came from the ezviz_hp7 integration start
# with this. The new integration keeps them, so that moving over keeps every
# entity ID (and the dashboards and phone widgets that use them).
LEGACY_DOMAIN = "ezviz_hp7"

# How long the binary sensors for an alarm stay on.
PULSE_SECONDS = 3

# Home Assistant events fired when the lock opens (both names carry the same data).
EVENT_UNLOCK = f"{DOMAIN}_unlock"
EVENT_UNLOCK_LEGACY = f"{LEGACY_DOMAIN}_unlock"

# Ignore a second press of the same button within this many seconds.
UNLOCK_COOLDOWN = 3.0

PICTURE_ATTEMPTS = 4
PICTURE_RETRY_DELAY = 2.0

SIGNAL_EVENT = f"{DOMAIN}_event_{{entry_id}}"
SIGNAL_PICTURE = f"{DOMAIN}_picture_{{entry_id}}"


# -- live video (options of the entry) ---------------------------------------
# Port the stream relay listens on (0 = any free port). Set a fixed value so
# that go2rtc, mediamtx or Frigate can keep a stable URL.
CONF_RELAY_PORT = "relay_port"
# Host the relay listens on. 127.0.0.1 keeps the raw stream local; 0.0.0.0 lets
# another machine (Frigate) read it. There is no password on this socket.
CONF_RELAY_BIND = "relay_bind"
DEFAULT_RELAY_BIND = "127.0.0.1"
# Repeat SPS/PPS before every keyframe (helps firmware that sends them once).
CONF_AGGRESSIVE_MPEGTS = "aggressive_mpegts"
# Video codec of the doorbell: auto looks at the stream.
CONF_VIDEO_CODEC = "video_codec"
VIDEO_CODEC_H264 = "h264"
VIDEO_CODEC_HEVC = "hevc"
VIDEO_CODEC_AUTO = "auto"
VIDEO_CODEC_HEVC_COPY = "hevc_copy"  # no transcode; for players that can decode HEVC
VIDEO_CODECS = [VIDEO_CODEC_AUTO, VIDEO_CODEC_H264, VIDEO_CODEC_HEVC, VIDEO_CODEC_HEVC_COPY]
# Where the video comes from: the EZVIZ cloud, the local network, or local first.
CONF_STREAM_SOURCE = "stream_source"
STREAM_SOURCE_CLOUD = "cloud"
STREAM_SOURCE_LOCAL = "local"
STREAM_SOURCE_AUTO = "auto"
STREAM_SOURCES = [STREAM_SOURCE_CLOUD, STREAM_SOURCE_LOCAL, STREAM_SOURCE_AUTO]
# How the picture reaches the browser: Home Assistant stream (audio, low delay;
# no HEVC), motion JPEG (no audio, every codec), or chosen by the codec found.
CONF_STREAM_MODE = "stream_mode"
STREAM_MODE_AUTO = "auto"
STREAM_MODE_WEBRTC = "webrtc"
STREAM_MODE_MJPEG = "mjpeg"
STREAM_MODES = [STREAM_MODE_AUTO, STREAM_MODE_WEBRTC, STREAM_MODE_MJPEG]
# Which picture the local connection asks for: full size or the small one.
CONF_STREAM_QUALITY = "stream_quality"
STREAM_QUALITY_MAIN = "main"
STREAM_QUALITY_SUB = "sub"
STREAM_QUALITIES = [STREAM_QUALITY_MAIN, STREAM_QUALITY_SUB]
# Write raw stream captures to the config folder, for bug reports only.
CONF_DIAGNOSTIC_DUMPS = "diagnostic_dumps"
# Key that decrypts the video when Image/Video Encryption is on.
CONF_ENCRYPTION_KEY = "encryption_key"

# -- history browser (Media > EZVIZ Video Intercom) ---------------------------
HISTORY_DAYS = 14  # how many days back the browser offers
HISTORY_PAGE_SIZE = 50  # the cloud list gives at most 50 entries per request
HISTORY_MAX_PAGES = 4
HISTORY_TODAY_TTL = 60.0  # seconds a list of today is kept before asking again
HISTORY_PAST_TTL = 900.0
HISTORY_PICTURE_CACHE = 100  # pictures kept in memory (nothing is written to disk)

# -- recordings of the SD card (Media > EZVIZ Video Intercom > Recordings) -------
RECORDING_DAYS = HISTORY_DAYS
RECORDING_PAGE_SIZE = 50
RECORDING_MAX_PAGES = 6
RECORDING_LIST_TTL_TODAY = 60.0
RECORDING_LIST_TTL_PAST = 900.0
CLIP_CACHE_DIR = "ezviz_instant_bell_clips"  # inside .storage
CLIP_CACHE_MAX_BYTES = 500 * 1024 * 1024
