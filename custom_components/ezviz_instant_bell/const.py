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

DEFAULT_REGION = "apiieu.ezvizlife.com"
REGIONS = {
    "apiieu.ezvizlife.com": "Europe",
    "apiius.ezvizlife.com": "North America",
    "apiisgp.ezvizlife.com": "Asia",
}

DEFAULT_POLL_INTERVAL = 5  # seconds between checks of the cloud message list
MIN_POLL_INTERVAL = 2
MAX_POLL_INTERVAL = 60

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
