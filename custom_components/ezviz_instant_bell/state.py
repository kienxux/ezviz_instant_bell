"""Turn the raw answers of the EZVIZ cloud into the values the entities show.

Pure Python: no Home Assistant imports, so it can be tested on its own.

The input is what ``pyezvizapi`` returns: the per-device record of
``get_device_infos`` (keys such as ``STATUS``, ``SWITCH``, ``NODISTURB``,
``FEATURE_INFO``) and the status dictionary of ``EzvizCamera.status()``.
Keys are only written when the device reports them, so a device without a
feature does not get a phantom entity.
"""

from __future__ import annotations

import json
from typing import Any

PRIVACY_SWITCH_TYPE = 7
LABEL_LIGHT_SWITCH_TYPE = 611

# Values of the ``ChimeMusic`` setting that are used when the device does not
# report them (all fields are written back together, so the rest is preserved).
CHIME_DEFAULTS: dict[str, int] = {
    "doorbell": 10,
    "pir": 0,
    "volume": 7,
    "doorbell_enable": 1,
    "pir_enable": 0,
}

KNOWN_MODELS = ("HP7", "CP7", "HP4", "DP1", "DP2")
DEFAULT_MODEL = "HP7"


def coerce_bool(value: Any) -> bool | None:
    """Loose bool for cloud fields that arrive as int, str or bool."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "on", "yes", "y", "enable", "enabled"):
            return True
        if text in ("0", "false", "off", "no", "n", "disable", "disabled"):
            return False
    return None


def detect_model(info: dict[str, Any] | None) -> str:
    """Short model label (``HP7``, ``CP7``, ``CPD5`` ...) from a device record."""
    details = (info or {}).get("deviceInfos") or {}
    for candidate in (
        details.get("deviceSubCategory"),
        details.get("deviceCategory"),
        details.get("deviceType"),
        details.get("model"),
    ):
        if not candidate or not isinstance(candidate, str):
            continue
        upper = candidate.upper()
        for known in KNOWN_MODELS:
            if known in upper:
                return known
        token = upper.replace("-", " ").split()[-1]
        if 2 <= len(token) <= 8 and any(c.isdigit() for c in token):
            return token
    return DEFAULT_MODEL


def bare_serial(serial: str) -> str:
    """``BE123-BE456`` -> ``BE123``."""
    return serial.split("-", 1)[0]


def find_monitors(serial: str, devices: dict[str, Any]) -> list[str]:
    """Serials of the indoor monitors that belong to a door station.

    A composite serial ``MONITOR-DOOR`` means the monitor is the part before
    the dash, if the account lists it as a device of its own.
    """
    if "-" not in serial:
        return []
    bare = bare_serial(serial)
    return [bare] if bare in devices else []


# ---------------------------------------------------------------------------
# Feature tree (FEATURE_INFO)
# ---------------------------------------------------------------------------


def _find_first(tree: Any, key: str) -> Any:
    stack: list[Any] = [tree]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == key:
                    return v
                stack.append(v)
        elif isinstance(obj, list):
            stack.extend(obj)
    return None


def read_night_light(info: dict[str, Any]) -> bool | None:
    """Name-plate LED state from the IoT feature tree (its nesting varies)."""
    found: list[bool] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "NightLightEnable" and not isinstance(v, (dict, list)):
                    found.append(bool(v))
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(info.get("FEATURE_INFO"))
    return found[0] if found else None


def read_feature_states(info: dict[str, Any]) -> dict[str, Any]:
    """Read-only settings: mute, microphone volume, loitering, stranger and human detection."""
    out: dict[str, Any] = {}
    tree = info.get("FEATURE_INFO")
    if not isinstance(tree, dict):
        return out

    mute = _find_first(tree, "MuteEnabled")
    if isinstance(mute, dict) and "enabled" in mute:
        out["mute_on"] = bool(mute["enabled"])

    volume = _find_first(tree, "MicrophoneVolume")
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        out["mic_volume"] = int(volume)

    loitering = _find_first(tree, "LoiteringEnable")
    if isinstance(loitering, dict) and "enable" in loitering:
        out["loitering_on"] = bool(loitering["enable"])

    stranger = _find_first(tree, "StrangerDetectionCfg")
    if isinstance(stranger, dict):
        contrast = stranger.get("faceContrastList")
        if isinstance(contrast, list) and contrast and isinstance(contrast[0], dict):
            face = contrast[0].get("faceContrast")
            if isinstance(face, dict) and "enable" in face:
                out["stranger_detection_on"] = bool(face["enable"])

    downloaded = _find_first(tree, "DownloadedAPP")
    if isinstance(downloaded, dict) and isinstance(downloaded.get("APP"), list):
        for app in downloaded["APP"]:
            if isinstance(app, dict) and "human_detect" in str(app.get("APPID", "")):
                out["human_detection_on"] = bool(app.get("enabled"))
                break
    return out


def read_key_list(info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Enrolled RFID cards, faces and palms (``KeyMgr.CardKeyInfo.KeyInfo``)."""
    keys = _find_first(info.get("FEATURE_INFO"), "KeyInfo")
    if isinstance(keys, list):
        return [k for k in keys if isinstance(k, dict)]
    return None


# ---------------------------------------------------------------------------
# Switches, do-not-disturb, defence
# ---------------------------------------------------------------------------


def _switch_list(info: dict[str, Any]) -> list[dict[str, Any]]:
    switches = info.get("SWITCH")
    if isinstance(switches, list):
        return [s for s in switches if isinstance(s, dict)]
    if isinstance(switches, dict):
        if "switchStatusInfos" in switches:
            return [s for s in switches.get("switchStatusInfos") or [] if isinstance(s, dict)]
        out = []
        for k, v in switches.items():
            try:
                out.append({"type": int(k), "enable": v.get("enable") if isinstance(v, dict) else v})
            except (TypeError, ValueError):
                continue
        return out
    return []


def read_extra_states(info: dict[str, Any]) -> dict[str, Any]:
    """Do-not-disturb, privacy, defence, label light and read-only feature states."""
    out: dict[str, Any] = {}

    nodisturb = info.get("NODISTURB")
    if isinstance(nodisturb, dict):
        dnd = coerce_bool(nodisturb.get("enable"))
        if dnd is None:
            # The cloud reports it as two flags: no alarm push, no call push.
            parts = [coerce_bool(nodisturb.get(k)) for k in ("alarmEnable", "callingEnable")]
            parts = [p for p in parts if p is not None]
            dnd = any(parts) if parts else None
        if dnd is not None:
            out["dnd_on"] = dnd

    for item in _switch_list(info):
        try:
            kind = int(item.get("type", -1))
        except (TypeError, ValueError):
            continue
        value = coerce_bool(item.get("enable"))
        if value is None:
            continue
        if kind == PRIVACY_SWITCH_TYPE:
            out["privacy_on"] = value
        elif kind == LABEL_LIGHT_SWITCH_TYPE:
            out["label_light_on"] = value

    night = read_night_light(info)
    if night is not None:
        out["label_light_on"] = night

    out.update(read_feature_states(info))

    status = info.get("STATUS")
    if isinstance(status, dict):
        defence = coerce_bool(status.get("globalStatus"))
        if defence is not None:
            out["defence_on"] = defence
    return out


def base_status(cam_status: dict[str, Any]) -> dict[str, Any]:
    """The plain values of ``EzvizCamera.status()`` the sensors show."""
    wifi = cam_status.get("WIFI") or {}
    return {
        "name": cam_status.get("name"),
        "version": cam_status.get("version"),
        "upgrade_available": cam_status.get("upgrade_available"),
        "status": cam_status.get("status"),
        "wan_ip": cam_status.get("wan_ip"),
        "pir_status": cam_status.get("PIR_Status"),
        "motion": cam_status.get("Motion_Trigger"),
        "seconds_last_trigger": cam_status.get("Seconds_Last_Trigger"),
        "last_alarm_time": cam_status.get("last_alarm_time"),
        "last_alarm_pic": cam_status.get("last_alarm_pic"),
        "alarm_name": cam_status.get("last_alarm_type_name"),
        "ssid": wifi.get("ssid"),
        "signal": wifi.get("signal"),
        "local_ip": cam_status.get("local_ip") or wifi.get("address"),
    }


# ---------------------------------------------------------------------------
# Chime (the ``ChimeMusic`` setting of the indoor monitor)
# ---------------------------------------------------------------------------


def parse_chime_answer(answer: Any) -> dict[str, Any] | None:
    """Return the ChimeMusic settings from a ``get_dev_config`` answer."""
    if not isinstance(answer, dict):
        return None
    value = answer.get("valueInfo") or answer.get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def chime_values(config: dict[str, Any]) -> dict[str, Any]:
    """Readable values from a ChimeMusic dictionary (missing ones are left out)."""
    out: dict[str, Any] = {}
    if "doorbell_enable" in config:
        out["on"] = config["doorbell_enable"] in (1, "1", True)
    if "pir_enable" in config:
        out["pir_on"] = config["pir_enable"] in (1, "1", True)
    for source, target in (("volume", "volume"), ("doorbell", "ringtone"), ("pir", "pir_ringtone")):
        try:
            out[target] = int(config[source])
        except (KeyError, TypeError, ValueError):
            pass
    return out


def merge_chime(current: dict[str, Any] | None, **fields: int) -> dict[str, Any]:
    """New ChimeMusic dictionary: defaults, then what the device has, then the change."""
    return {**CHIME_DEFAULTS, **(current or {}), **fields}


def chime_status(per_serial: dict[str, dict[str, Any]], door: str, monitors: list[str]) -> dict[str, Any]:
    """Arrange chime values the way the entities look them up.

    ``per_serial`` maps a serial to :func:`chime_values`. The door station's
    own values are stored flat, the monitors' values in dictionaries by serial.
    """
    out: dict[str, Any] = {}
    own = per_serial.get(door) or {}
    for key, name in (
        ("on", "chime_is_on"),
        ("volume", "chime_volume"),
        ("pir_on", "chime_pir_is_on"),
        ("ringtone", "chime_ringtone"),
        ("pir_ringtone", "chime_pir_ringtone"),
    ):
        if key in own:
            out[name] = own[key]

    for key, name in (
        ("on", "chime_is_on_monitors"),
        ("volume", "chime_volume_monitors"),
        ("pir_on", "chime_pir_is_on_monitors"),
        ("ringtone", "chime_ringtone_monitors"),
        ("pir_ringtone", "chime_pir_ringtone_monitors"),
    ):
        values = {m: per_serial[m][key] for m in monitors if key in (per_serial.get(m) or {})}
        if values:
            out[name] = values
    return out
