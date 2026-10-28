"""Parsing of the real shapes of the EZVIZ device records (anonymised copy)."""

import json
from pathlib import Path

import alarms
import state

DATA = json.loads((Path(__file__).parent / "fixtures" / "device_infos.json").read_text())
DOOR_SERIAL = "BE0000001-BE0000002"
DOOR = DATA[DOOR_SERIAL]
MONITOR = DATA["BE0000001"]


def test_model_labels():
    assert state.detect_model(DOOR) == "CPD5"
    assert state.detect_model(MONITOR) == "CP5"
    assert state.detect_model({}) == "HP7"
    assert state.detect_model({"deviceInfos": {"deviceSubCategory": "HP7 Pro"}}) == "HP7"


def test_monitor_is_found_from_the_composite_serial():
    assert state.find_monitors(DOOR_SERIAL, DATA) == ["BE0000001"]
    assert state.find_monitors("BE0000001", DATA) == []
    assert state.find_monitors("XX1-XX2", DATA) == []


def test_extra_states_of_the_door_station():
    extra = state.read_extra_states(DOOR)
    assert extra["dnd_on"] is False  # alarmEnable 0, callingEnable 0
    assert extra["privacy_on"] is False
    assert extra["defence_on"] is True
    assert extra["mic_volume"] == 90
    assert extra["mute_on"] is False
    assert extra["loitering_on"] is True
    assert extra["stranger_detection_on"] is False
    assert "human_detection_on" not in extra  # not reported by this device


def test_do_not_disturb_from_either_flag():
    assert state.read_extra_states({"NODISTURB": {"alarmEnable": 1, "callingEnable": 0}})["dnd_on"] is True
    assert state.read_extra_states({"NODISTURB": {"enable": 0}})["dnd_on"] is False
    assert "dnd_on" not in state.read_extra_states({"NODISTURB": {}})


def test_privacy_and_label_light_from_switch_list():
    info = {"SWITCH": [{"type": 7, "enable": True}, {"type": 611, "enable": False}, {"type": 99, "enable": True}]}
    out = state.read_extra_states(info)
    assert out == {"privacy_on": True, "label_light_on": False}


def test_switch_list_in_other_shapes():
    assert state.read_extra_states({"SWITCH": {"7": {"enable": 1}}})["privacy_on"] is True
    assert state.read_extra_states({"SWITCH": {"switchStatusInfos": [{"type": 7, "enable": 0}]}})["privacy_on"] is False


def test_key_list():
    keys = state.read_key_list(DOOR)
    assert [k["keyName"] for k in keys] == ["Alice", "Bob"]
    assert state.read_key_list(MONITOR) is None


def test_base_status_uses_wifi_for_missing_ip():
    out = state.base_status({"name": "Tor", "WIFI": {"ssid": "S", "signal": 54, "address": "192.0.2.1"}})
    assert out["ssid"] == "S" and out["signal"] == 54 and out["local_ip"] == "192.0.2.1"


# --- chime --------------------------------------------------------------


def test_chime_answer_as_text_or_dict():
    text = json.dumps({"volume": 3, "doorbell": 4, "doorbell_enable": 1})
    assert state.parse_chime_answer({"valueInfo": text}) == {"volume": 3, "doorbell": 4, "doorbell_enable": 1}
    assert state.parse_chime_answer({"value": {"volume": 1}}) == {"volume": 1}
    assert state.parse_chime_answer({"valueInfo": "not json"}) is None
    assert state.parse_chime_answer(None) is None


def test_chime_values():
    out = state.chime_values({"volume": "5", "doorbell": 2, "pir": 0, "doorbell_enable": 0, "pir_enable": 1})
    assert out == {"on": False, "pir_on": True, "volume": 5, "ringtone": 2, "pir_ringtone": 0}
    assert state.chime_values({}) == {}


def test_merge_chime_keeps_other_fields():
    merged = state.merge_chime({"volume": 2, "extra": 9}, doorbell_enable=0)
    assert merged["volume"] == 2 and merged["extra"] == 9 and merged["doorbell_enable"] == 0
    assert merged["doorbell"] == state.CHIME_DEFAULTS["doorbell"]


def test_chime_status_layout():
    values = {
        "DOOR": {"volume": 7},
        "MON": {"on": True, "volume": 3, "ringtone": 1},
    }
    out = state.chime_status(values, "DOOR", ["MON"])
    assert out["chime_volume"] == 7
    assert "chime_is_on" not in out
    assert out["chime_is_on_monitors"] == {"MON": True}
    assert out["chime_volume_monitors"] == {"MON": 3}
    assert out["chime_ringtone_monitors"] == {"MON": 1}


# --- alarms -------------------------------------------------------------


def test_alarm_names_match_in_any_case():
    kinds = {k.key: k for k in alarms.ALARM_KINDS}
    assert alarms.matches(kinds["doorbell_ringing"], "YOUR DOORBELL IS RINGING")
    assert alarms.matches(kinds["gate_open"], "Monitor open the gate")
    assert not alarms.matches(kinds["gate_open"], "Monitor open the door")
    assert not alarms.matches(kinds["gate_open"], None)


def test_event_kind_stands_for_an_alarm_kind():
    assert "doorbell_ringing" in alarms.kinds_for("anything", "ring")
    assert "unlock_lock" in alarms.kinds_for("", "door_unlock")
    assert alarms.kinds_for("x", "other") == []


def test_card_id_and_name():
    assert alarms.extract_card_id({"cardNo": "17"}) == "17"
    assert alarms.extract_card_id({"relationInfo": json.dumps({"cardId": 5})}) == "5"
    assert alarms.extract_card_id({"cardNo": "0"}) is None
    keys = state.read_key_list(DOOR)
    assert alarms.resolve_card_name(keys, "2") == "Bob"
    assert alarms.resolve_card_name(keys, None) is None  # two enabled keys: no guess
    assert alarms.resolve_card_name(keys[:1], None) == "Alice"


def test_alarm_from_answer():
    assert alarms.alarm_from_answer({"alarms": [{"a": 1}]}) == {"a": 1}
    assert alarms.alarm_from_answer({"alarms": []}) is None
    assert alarms.alarm_from_answer(None) is None
