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
    """One kind of alarm that gets a binary sensor."""

    key: str
    names: tuple[str, ...]
    icon: str


ALARM_KINDS: tuple[AlarmKind, ...] = (
    AlarmKind("smart_detection_alarm", ("Smart Detection Alarm",), "mdi:run"),
    AlarmKind("intelligent_detection_alarm", ("Intelligent Detection Alarm",), "mdi:account-search"),
    AlarmKind("doorbell_ringing", ("Your doorbell is ringing",), "mdi:doorbell"),
    AlarmKind("gate_open", ("EZVIZ app open the gate", "Monitor open the gate"), "mdi:gate-open"),
    # "The lock just opened" however it happened: app, monitor, card, face,
    # palm, code or exit button. The kinds below say how.
    AlarmKind("unlock_lock", ("unlock the lock",), "mdi:lock-open-variant"),
    AlarmKind("unlock_rfid", ("RFID card unlock", "Unlock with RFID", "Card unlock"), "mdi:card-account-details"),
    AlarmKind("unlock_face", ("Face recognition unlock", "Face unlock", "Unlock with face"), "mdi:face-recognition"),
    AlarmKind("unlock_palm", ("Palm vein unlock", "Palm unlock", "Unlock with palm"), "mdi:hand-front-right"),
    AlarmKind("unlock_code", ("Passcode unlock", "Code unlock", "Password unlock"), "mdi:dialpad"),
    AlarmKind("unlock_app", ("EZVIZ app unlock", "App unlock"), "mdi:cellphone"),
)

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
