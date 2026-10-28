"""Constants for Instant Bell for EZVIZ."""

from __future__ import annotations

DOMAIN = "ezviz_instant_bell"

CONF_REGION = "region"
CONF_DEVICES = "devices"
CONF_NAMES = "names"
CONF_POLL_INTERVAL = "poll_interval"
CONF_DOOR_LOCK_NO = "door_lock_no"
CONF_GATE_LOCK_NO = "gate_lock_no"
CONF_CREATE_BUTTONS = "create_buttons"

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

# Ignore a second press of the same button within this many seconds.
UNLOCK_COOLDOWN = 3.0

PICTURE_ATTEMPTS = 4
PICTURE_RETRY_DELAY = 2.0

SIGNAL_EVENT = f"{DOMAIN}_event_{{entry_id}}"
SIGNAL_PICTURE = f"{DOMAIN}_picture_{{entry_id}}"
