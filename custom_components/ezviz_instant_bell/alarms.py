"""Recognise what happened at the door from the text of an alarm.

Pure Python. EZVIZ names an alarm with a sentence that differs between
firmware versions and languages, so the names below are matched as lower-case
parts of the sentence rather than exactly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AlarmKind:
    """One kind of alarm that gets a binary sensor.

    A ``learned`` kind depends on the model and its setup (card reader, face
    camera, keypad, AI detection) and EZVIZ has no reliable flag for it, so its
    sensor is made only once the door station has reported such an alarm.
    """

    key: str
    names: tuple[str, ...]
    icon: str
    learned: bool = False


ALARM_KINDS: tuple[AlarmKind, ...] = (
    AlarmKind("smart_detection_alarm", ("Smart Detection Alarm",), "mdi:run", learned=True),
    AlarmKind("intelligent_detection_alarm", ("Intelligent Detection Alarm",), "mdi:account-search", learned=True),
    AlarmKind("doorbell_ringing", ("Your doorbell is ringing",), "mdi:doorbell"),
    AlarmKind("gate_open", ("EZVIZ app open the gate", "Monitor open the gate"), "mdi:gate-open"),
    # "The lock just opened" however it happened: app, monitor, card, face,
    # palm, code or exit button. The kinds below say how.
    AlarmKind("unlock_lock", ("unlock the lock",), "mdi:lock-open-variant"),
    AlarmKind(
        "unlock_rfid", ("RFID card unlock", "Unlock with RFID", "Card unlock"), "mdi:card-account-details", learned=True
    ),
    AlarmKind(
        "unlock_face", ("Face recognition unlock", "Face unlock", "Unlock with face"), "mdi:face-recognition", learned=True
    ),
    AlarmKind("unlock_palm", ("Palm vein unlock", "Palm unlock", "Unlock with palm"), "mdi:hand-front-right", learned=True),
    AlarmKind("unlock_code", ("Passcode unlock", "Code unlock", "Password unlock"), "mdi:dialpad", learned=True),
    AlarmKind("unlock_app", ("EZVIZ app unlock", "App unlock"), "mdi:cellphone"),
)

KINDS_BY_KEY = {k.key: k for k in ALARM_KINDS}
LEARNED_KEYS = frozenset(k.key for k in ALARM_KINDS if k.learned)

# Kinds that also fire the ``ezviz_instant_bell_unlock`` event.
UNLOCK_KEYS = frozenset(
    {"unlock_lock", "unlock_rfid", "unlock_face", "unlock_palm", "unlock_code", "unlock_app", "gate_open"}
)

# Event kinds of the push / message list that stand for a whole alarm kind.
KIND_TO_ALARM = {
    "ring": "doorbell_ringing",
    "door_unlock": "unlock_lock",
    "gate_open": "gate_open",
}


# Kinds that are about the door, not about someone moving in front of it.
DOOR_KEYS = UNLOCK_KEYS | {"doorbell_ringing"}
# ``subType`` of list entries for a ring, a door and a gate opening.
DOOR_SUBTYPES = frozenset({2701, 10242, 10243})


def is_detection(title: Any, subtype: int | None) -> bool:
    """True for a list entry about movement or a person: anything that is not a ring or an opening.

    The motion and person entries are not matched by their own names, because
    those differ between firmware and languages; a ring or an opening is
    recognised reliably, so everything else is taken.
    """
    if subtype in DOOR_SUBTYPES:
        return False
    return not any(key in DOOR_KEYS for key in kinds_for(title))


# Codes that pyezvizapi uses as filters of the message list for motion and for
# a person. Not yet seen on a real entry, so the words below decide as well.
MOTION_CODES = frozenset({2402})
PERSON_CODES = frozenset({2403})
# "Smart" and "intelligent detection" are EZVIZ's names for human shape
# detection (assumed from the app; the plain motion alarm says "motion").
PERSON_WORDS = ("person", "people", "human", "smart detection", "intelligent detection")
MOTION_WORDS = ("motion", "movement", "pir")


def detection_kind(title: Any, subtype: int | None, alarm_type: int | None = None) -> str | None:
    """``"person"``, ``"motion"`` or None for a message that is neither (or not recognised)."""
    if not is_detection(title, subtype):
        return None
    codes = {subtype, alarm_type}
    lowered = title.lower() if isinstance(title, str) else ""
    if codes & PERSON_CODES or any(word in lowered for word in PERSON_WORDS):
        return "person"
    if codes & MOTION_CODES or any(word in lowered for word in MOTION_WORDS):
        return "motion"
    return None


def matches(kind: AlarmKind, text: Any) -> bool:
    """True if ``text`` contains one of the kind's names (any letter case)."""
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(name.lower() in lowered for name in kind.names)


def kinds_for(text: Any, event_kind: str | None = None) -> list[str]:
    """Keys of all alarm kinds that a title (and optionally an event kind) stand for."""
    keys = [k.key for k in ALARM_KINDS if matches(k, text)]
    extra = KIND_TO_ALARM.get(event_kind or "")
    if extra and extra not in keys:
        keys.append(extra)
    return keys


_CARD_FIELDS = ("cardNo", "cardID", "cardId", "rfidCardNo", "rfidId", "userNo", "tagId", "doorlockCardNo")
_JSON_FIELDS = ("relationInfo", "recExtraInfo", "remark", "alarmExtInfo")


def _clean(value: Any) -> str | None:
    if isinstance(value, (str, int)) and str(value).strip() not in ("", "0"):
        return str(value).strip()
    return None


def extract_card_id(detail: Any) -> str | None:
    """Best guess at the card number inside an alarm record (firmware moves it around)."""
    if not isinstance(detail, dict):
        return None
    for name in _CARD_FIELDS:
        found = _clean(detail.get(name))
        if found:
            return found
    for name in _JSON_FIELDS:
        raw = detail.get(name)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            for card in _CARD_FIELDS:
                found = _clean(parsed.get(card))
                if found:
                    return found
    return None


def resolve_card_name(keys: list[dict[str, Any]], card_id: str | None) -> str | None:
    """Name of the person a card belongs to, or the only enabled key's name."""
    if card_id is not None:
        for key in keys:
            if str(key.get("keyID")) == str(card_id) and isinstance(key.get("keyName"), str):
                return key["keyName"]
    enabled = [k for k in keys if isinstance(k, dict) and k.get("enabled") in (1, "1", True)]
    if len(enabled) == 1:
        name = enabled[0].get("keyName")
        if isinstance(name, str) and name.strip():
            return name
    return None


def alarm_from_answer(answer: Any) -> dict[str, Any] | None:
    """First alarm record of a ``get_alarminfo`` answer."""
    if not isinstance(answer, dict):
        return None
    for name in ("alarms", "alarmLogs", "alarmList", "alarmInfos"):
        items = answer.get(name)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    return None
