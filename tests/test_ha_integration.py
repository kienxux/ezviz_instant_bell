"""Integration tests with a fake EZVIZ client (no network)."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ezviz_instant_bell import ezviz
from custom_components.ezviz_instant_bell.recordings import parse_clip_id
from custom_components.ezviz_instant_bell.const import DOMAIN, EVENT_UNLOCK, EVENT_UNLOCK_LEGACY, LEGACY_DOMAIN


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _local_sockets(socket_enabled):
    """The stream relay listens on a port of this computer."""
    yield


@pytest.fixture(autouse=True)
def _clip_folder(tmp_path):
    """Fetched clips go to a folder of their own, not into the shared test config."""
    with patch("custom_components.ezviz_instant_bell.clips.CLIP_CACHE_DIR", str(tmp_path / "clips")):
        yield


@pytest.fixture(autouse=True)
def _no_codec_probe():
    """The live video would try to open a real stream to look at the codec."""

    async def _mjpeg(_relay):
        return "mjpeg"

    with patch("custom_components.ezviz_instant_bell.live_camera._resolve_auto_stream_mode", _mjpeg):
        yield


INFOS = json.loads((Path(__file__).parent / "fixtures" / "device_infos.json").read_text())
SERIAL = "BE0000001-BE0000002"
MONITOR = "BE0000001"
DEVICES = {
    MONITOR: {"name": "Tor", "category": "DBScreen"},
    SERIAL: {"name": "Tor", "category": "BDoorBell"},
}
CHIME = {"volume": 3, "doorbell": 4, "pir": 1, "doorbell_enable": 1, "pir_enable": 0}


def ring_message(msg_id: str, age_seconds: int = 0) -> dict:
    """A cloud list entry. ``time`` is local wall clock counted as UTC."""
    wall = dt_util.now() - timedelta(seconds=age_seconds)
    ms = int(wall.replace(tzinfo=dt_util.UTC).timestamp() * 1000)
    return {
        "subType": 2701,
        "msgId": msg_id,
        "title": "Your doorbell is ringing",
        "from": SERIAL,
        "pic": "https://example.invalid/pic",
        "time": ms,
    }


class FakeMqtt:
    def __init__(self, callback):
        self.callback = callback
        self.connected = False
        self.stopped = False

    def connect(self):
        self.connected = True

    def stop(self):
        self.stopped = True


class FakeClient:
    def __init__(self):
        self._token = {"username": "internal-user"}
        self.messages: list[dict] = []
        self.unlocks: list[tuple] = []
        self.fail_unlock = False
        self.fail_infos = False
        self.mqtt = None
        self.calls: list[tuple] = []
        self.chime = dict(CHIME)
        self.infos = copy.deepcopy(INFOS)
        self.cam_key_answers: list = ["KEY123"]  # strings are returned, exceptions raised
        self.cam_key_calls: list[tuple] = []
        self.video_enc_calls: list[dict] = []
        self.fail_video_enc = False
        self.message_calls: list[tuple] = []
        self.records: list[dict] = []
        self.record_calls: list[tuple] = []
        self.fail_records = False

    # -- live video: keys and encryption
    def get_cam_key(self, serial, smscode=None):
        self.cam_key_calls.append((serial, smscode))
        answer = self.cam_key_answers.pop(0) if self.cam_key_answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer

    def set_video_enc(self, serial, enable=1, camera_verification_code=None, old_password=None):
        self.video_enc_calls.append(
            {"serial": serial, "enable": enable, "code": camera_verification_code, "old": old_password}
        )
        if self.fail_video_enc:
            raise RuntimeError("1011 verification code incorrect")

    def export_token(self):
        return {"session_id": "sid", "username": "internal-user"}

    # -- cloud list, pictures, push, unlock
    def get_device_messages_list(self, serials=None, **kwargs):
        self.message_calls.append((serials, kwargs))
        pages = getattr(self, "history_pages", None)
        if pages is not None and kwargs.get("end_time", "") in pages and serials:
            return {"message": list(pages[kwargs.get("end_time", "")])}
        return {"message": list(self.messages)}

    def download_alarm_image(self, url, serial=None):
        return b"JPEGDATA"

    def search_records(self, serial, channel, channel_serial, start, stop, size=20):
        self.record_calls.append((serial, channel, channel_serial, start, stop, size))
        if self.fail_records:
            raise RuntimeError("cloud down")
        inside = [r for r in self.records if start <= r["begin"] <= stop]
        return {"records": inside[:size]}

    def remote_unlock(self, serial, user_id, lock_no):
        if self.fail_unlock:
            raise RuntimeError("denied")
        self.unlocks.append((serial, user_id, lock_no))
        return True

    def get_terminals(self, limit=20):
        return {"terminals": [{"name": "Hassio"}, {"name": "Hassio"}, {"name": "Phone"}]}

    def get_mqtt_client(self, on_message_callback=None):
        self.mqtt = FakeMqtt(on_message_callback)
        return self.mqtt

    # -- device records and settings
    def get_device_infos(self, serial=None):
        if self.fail_infos:
            raise RuntimeError("cloud down")
        return copy.deepcopy(self.infos)

    def get_dev_config(self, serial, channel, key, **kwargs):
        if serial != MONITOR:
            raise RuntimeError("2000 device does not exist")
        return {"valueInfo": json.dumps(self.chime)}

    def _request_json(self, method, path, params=None, **kwargs):
        self.calls.append(("request", method, path, params))
        if params and params.get("key") == "ChimeMusic":
            self.chime = json.loads(params["value"])
        return {"meta": {"code": 200}}

    def _ensure_ok(self, answer, message):
        return None

    def set_dev_config_kv(self, serial, channel, key, value, **kwargs):
        self.calls.append(("kv", serial, key, value))

    def do_not_disturb(self, serial, enable=1, **kwargs):
        self.calls.append(("dnd", serial, enable))
        return True

    def switch_status(self, serial, status_type, enable, **kwargs):
        self.calls.append(("switch", serial, status_type, enable))
        return True

    def set_camera_defence(self, serial, enable, **kwargs):
        self.calls.append(("defence", serial, enable))
        return True

    def set_iot_feature(self, serial, **kwargs):
        self.calls.append(("iot", serial, kwargs.get("action_id"), kwargs.get("value")))
        return {}

    def get_alarminfo(self, serial, limit=1, **kwargs):
        return {"alarms": [{"cardNo": "2"}]}


def make_entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        options=options or {},
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            "username": "user@example.com",
            "password": "pw",
            "region": "apiieu.ezvizlife.com",
            "devices": [SERIAL],
            "names": {SERIAL: "Tor"},
        },
    )


@pytest.fixture
def client():
    return FakeClient()


def device_of(registry, identifiers):
    """The device with these identifiers (``async_get_device`` is refused by newer Home Assistant)."""
    if hasattr(registry, "async_get_devices"):
        return registry.async_get_devices(identifiers=identifiers)[0]
    return registry.async_get_device(identifiers=identifiers)


async def setup(hass: HomeAssistant, client: FakeClient, options: dict | None = None) -> MockConfigEntry:
    entry = make_entry(options)
    entry.add_to_hass(hass)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def entity_id(hass: HomeAssistant, platform: str, key: str, serial: str = SERIAL) -> str:
    """Entity of the new kind (event, image) by its unique ID."""
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id(platform, DOMAIN, f"{serial}_{key}")
    assert eid, f"no {platform} entity for {key}"
    return eid


def legacy_entity(hass: HomeAssistant, platform: str, suffix: str, serial: str = SERIAL) -> str:
    """Entity that has the unique ID the ezviz_hp7 integration used."""
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id(platform, DOMAIN, f"{LEGACY_DOMAIN}_{serial}_{suffix}")
    assert eid, f"no {platform} entity {suffix} for {serial}"
    return eid


async def tick(hass: HomeAssistant, seconds: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def poll_once(hass: HomeAssistant) -> None:
    await tick(hass, 6)


async def test_entities_are_created(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    for platform, key in (("event", "bell"), ("image", "last_picture")):
        entity_id(hass, platform, key)
    assert client.mqtt.connected


async def test_full_set_of_entities_with_the_old_unique_ids(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    expected = {
        "sensor": [f"sensor_{k}" for k in ("name", "version", "status", "motion", "last_alarm_time", "alarm_name",
                                           "signal", "ssid", "local_ip", "wan_ip", "seconds_last_trigger",
                                           "upgrade_available", "mic_volume")],
        "binary_sensor": ["binary_motion", "alarm_doorbell_ringing", "alarm_gate_open", "alarm_unlock_lock",
                          "alarm_unlock_app", "binary_mute_on", "binary_loitering_on", "binary_stranger_detection_on"],
        "switch": ["chime_sound", "dnd", "privacy", "defence", "label_light", "chime_pir"],
        "number": ["chime_volume", "chime_ringtone", "chime_pir_ringtone"],
        "button": ["unlock_door", "unlock_gate"],
        "camera": ["last_snapshot"],
    }
    for platform, suffixes in expected.items():
        for suffix in suffixes:
            legacy_entity(hass, platform, suffix)
    # the indoor monitor has its own chime controls
    for platform, suffix in (("switch", "chime_sound_monitor"), ("switch", "chime_pir_monitor"),
                             ("number", "chime_volume_monitor"), ("number", "chime_ringtone_monitor"),
                             ("number", "chime_pir_ringtone_monitor")):
        legacy_entity(hass, platform, suffix, serial=MONITOR)
    # features the device does not report get no entity
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("binary_sensor", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_binary_human_detection_on") is None
    # nor do alarms that depend on the model until the door station has reported one
    for kind in LEARNED_KINDS:
        assert reg.async_get_entity_id("binary_sensor", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_alarm_{kind}") is None


async def test_sensor_values_and_names(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    assert hass.states.get(legacy_entity(hass, "sensor", "sensor_status")).state == "online"
    assert hass.states.get(legacy_entity(hass, "sensor", "sensor_name")).state == "Tor"
    assert hass.states.get(legacy_entity(hass, "sensor", "sensor_mic_volume")).state == "90"
    assert hass.states.get(legacy_entity(hass, "switch", "defence")).state == "on"
    assert hass.states.get(legacy_entity(hass, "switch", "privacy")).state == "off"
    # devices named as in the EZVIZ app; the monitor has the same name there (#16)
    reg = dr.async_get(hass)
    device = device_of(reg, {(DOMAIN, SERIAL)})
    monitor = device_of(reg, {(DOMAIN, MONITOR)})
    assert device.name == "Tor" and device.model == "CPD5"
    assert monitor.name == "Tor Monitor" and monitor.via_device_id == device.id


async def test_device_names_without_an_ezviz_name(hass: HomeAssistant, client: FakeClient) -> None:
    devices = {MONITOR: {"name": MONITOR, "category": "DBScreen"}, SERIAL: {"name": SERIAL, "category": "BDoorBell"}}
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "names": {}})
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=devices
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    reg = dr.async_get(hass)
    assert device_of(reg, {(DOMAIN, SERIAL)}).name == "EZVIZ CPD5 (BE0000001-BE0000002)"
    assert device_of(reg, {(DOMAIN, MONITOR)}).name == "EZVIZ CPD5 Monitor (BE0000001)"


async def test_monitor_chime_values(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    assert hass.states.get(legacy_entity(hass, "switch", "chime_sound_monitor", MONITOR)).state == "on"
    assert hass.states.get(legacy_entity(hass, "switch", "chime_pir_monitor", MONITOR)).state == "off"
    assert hass.states.get(legacy_entity(hass, "number", "chime_volume_monitor", MONITOR)).state == "3.0"
    assert hass.states.get(legacy_entity(hass, "number", "chime_ringtone_monitor", MONITOR)).state == "4.0"
    # the door station has no chime: unknown, not an error
    assert hass.states.get(legacy_entity(hass, "number", "chime_volume")).state == "unknown"


async def test_chime_switch_writes_all_fields(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    eid = legacy_entity(hass, "switch", "chime_sound_monitor", MONITOR)
    await hass.services.async_call("switch", "turn_off", {"entity_id": eid}, blocking=True)
    write = next(c for c in client.calls if c[0] == "request")
    assert write[1] == "PUT" and write[2].endswith(f"{MONITOR}/1/op")
    sent = json.loads(write[3]["value"])
    assert sent["doorbell_enable"] == 0
    assert sent["volume"] == 3 and sent["doorbell"] == 4  # the rest is kept
    assert hass.states.get(eid).state == "off"  # shown at once, before the cloud catches up


async def test_chime_number_writes_one_field(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    eid = legacy_entity(hass, "number", "chime_volume_monitor", MONITOR)
    await hass.services.async_call("number", "set_value", {"entity_id": eid, "value": 6}, blocking=True)
    sent = json.loads(next(c for c in client.calls if c[0] == "request")[3]["value"])
    assert sent["volume"] == 6 and sent["doorbell"] == 4


async def test_chime_write_falls_back_to_the_public_call(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    def broken(*args, **kwargs):
        raise RuntimeError("no")
    client._request_json = broken
    eid = legacy_entity(hass, "number", "chime_volume_monitor", MONITOR)
    await hass.services.async_call("number", "set_value", {"entity_id": eid, "value": 2}, blocking=True)
    kv = next(c for c in client.calls if c[0] == "kv")
    assert kv[1] == MONITOR and kv[3]["volume"] == 2


async def test_other_switches_call_the_right_api(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    for suffix, service in (("dnd", "turn_on"), ("privacy", "turn_on"), ("defence", "turn_off"), ("label_light", "turn_on")):
        await hass.services.async_call("switch", service, {"entity_id": legacy_entity(hass, "switch", suffix)}, blocking=True)
    assert ("dnd", SERIAL, 1) in client.calls
    assert ("switch", SERIAL, 7, True) in client.calls
    assert ("defence", SERIAL, 0) in client.calls
    assert ("iot", SERIAL, "NightLightEnable", {"value": True}) in client.calls


async def test_doorbell_indicator_is_switch_3(hass: HomeAssistant, client: FakeClient) -> None:
    # The door station of the fixture reports switch 3; the indoor monitor does not.
    await setup(hass, client)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": legacy_entity(hass, "switch", "indicator_light")}, blocking=True
    )
    assert ("switch", SERIAL, 3, True) in client.calls


async def test_diagnostics_list_the_switch_types(hass: HomeAssistant, client: FakeClient) -> None:
    from custom_components.ezviz_instant_bell.diagnostics import async_get_config_entry_diagnostics

    entry = await setup(hass, client)
    door = (await async_get_config_entry_diagnostics(hass, entry))["doors"]["door_1"]
    assert 3 in door["status"]["switch_types"] and 611 in door["status"]["switch_types"]


async def test_label_light_falls_back_to_switch_611(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    def refuse(*args, **kwargs):
        raise RuntimeError("not supported")
    client.set_iot_feature = refuse
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": legacy_entity(hass, "switch", "label_light")}, blocking=True
    )
    assert ("switch", SERIAL, 611, True) in client.calls


async def test_failed_setting_raises(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    def refuse(*args, **kwargs):
        raise RuntimeError("nope")
    client.do_not_disturb = refuse
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": legacy_entity(hass, "switch", "dnd")}, blocking=True
        )


async def test_status_failures_are_hidden_for_a_while(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    eid = legacy_entity(hass, "sensor", "sensor_status")
    assert hass.states.get(eid).state == "online"
    client.fail_infos = True
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client):
        for _ in range(4):
            await tick(hass, 15 * (_ + 1))
            assert hass.states.get(eid).state == "online"
        for i in range(4, 8):
            await tick(hass, 15 * (i + 1))
    assert hass.states.get(eid).state == "unavailable"
    client.fail_infos = False
    await tick(hass, 15 * 10)
    assert hass.states.get(eid).state == "online"


async def test_new_ring_fires_event_and_updates_picture(hass: HomeAssistant, client: FakeClient) -> None:
    client.messages = [ring_message("old", age_seconds=600)]
    await setup(hass, client)
    bell = entity_id(hass, "event", "bell")
    assert hass.states.get(bell).attributes.get("event_type") is None  # old entry not announced

    client.messages = [ring_message("new"), ring_message("old", age_seconds=600)]
    await poll_once(hass)
    state = hass.states.get(bell)
    assert state.attributes["event_type"] == "ring"
    assert state.attributes["source"] == "poll"

    image = hass.data["image"].get_entity(entity_id(hass, "image", "last_picture"))
    assert await image.async_image() == b"JPEGDATA"


async def test_ring_pulses_the_doorbell_ringing_sensor(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    ringing = legacy_entity(hass, "binary_sensor", "alarm_doorbell_ringing")
    assert hass.states.get(ringing).state == "off"
    client.messages = [ring_message("a")]
    await poll_once(hass)
    assert hass.states.get(ringing).state == "on"
    await tick(hass, 12)
    assert hass.states.get(ringing).state == "off"


async def test_same_ring_is_announced_once(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    bell = entity_id(hass, "event", "bell")
    client.messages = [ring_message("a")]
    await poll_once(hass)
    first = hass.states.get(bell).state
    await poll_once(hass)
    assert hass.states.get(bell).state == first


def push_unlock(client: FakeClient, serial: str = SERIAL, msg_id: int = 1, title: str = "EZVIZ app unlock the lock") -> None:
    client.mqtt.callback(
        {
            "id": msg_id,
            "alert": title,
            "ext": {
                "device_serial": serial,
                "alert_type_code": 10242,
                "time": dt_util.now().strftime("%Y-%m-%d %H:%M:%S"),
                "image": "https://example.invalid/p",
            },
        }
    )


async def test_push_unlock_event(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    push_unlock(client)
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(entity_id(hass, "event", "bell"))
    assert state.attributes["event_type"] == "door_unlock"
    assert state.attributes["source"] == "push"


async def test_unlock_fires_one_home_assistant_event_per_unlock(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    seen, legacy_seen = [], []
    hass.bus.async_listen(EVENT_UNLOCK, lambda e: seen.append(e.data))
    hass.bus.async_listen(EVENT_UNLOCK_LEGACY, lambda e: legacy_seen.append(e.data))
    push_unlock(client)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(legacy_entity(hass, "binary_sensor", "alarm_unlock_lock")).state == "on"
    # the same unlock arrives again through the status read: still one event
    await tick(hass, 16)
    categories = [d["category"] for d in seen]
    assert categories.count("unlock_lock") == 1
    assert legacy_seen and legacy_seen[0]["serial"] == SERIAL
    unlock = [d for d in seen if d["category"] == "unlock_lock"][0]
    # no access credentials (card id, the raw alarm record, or the household's
    # enrolled-key list) go into the event
    assert "keys" not in unlock and "details" not in unlock and "card_id" not in unlock


async def test_push_for_other_device_is_ignored(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    push_unlock(client, serial="ZZ9999999-ZZ1", msg_id=2)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "event", "bell")).attributes.get("event_type") is None


LEARNED_KINDS = ("unlock_rfid", "unlock_face", "unlock_palm", "unlock_code",
                 "smart_detection_alarm", "intelligent_detection_alarm")


def alarm_sensor(hass: HomeAssistant, kind: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_alarm_{kind}")


async def test_a_card_unlock_makes_its_sensor_and_still_counts(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    assert alarm_sensor(hass, "unlock_rfid") is None
    seen = []
    hass.bus.async_listen(EVENT_UNLOCK, lambda e: seen.append(e.data["category"]))
    push_unlock(client, title="RFID card unlock")
    await hass.async_block_till_done(wait_background_tasks=True)
    rfid = alarm_sensor(hass, "unlock_rfid")
    assert rfid and hass.states.get(rfid).state == "on"  # the first card unlock is shown, not lost
    assert seen.count("unlock_rfid") == 1
    assert alarm_sensor(hass, "unlock_face") is None
    # kept across a restart
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get(rfid).state == "off"
    push_unlock(client, msg_id=2, title="RFID card unlock")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(rfid).state == "on"


async def test_a_smart_detection_in_the_status_makes_its_sensor(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    assert alarm_sensor(hass, "smart_detection_alarm") is None
    api = entry.runtime_data.api
    read = api.build_status

    def with_alarm(*args, **kwargs):
        status = read(*args, **kwargs)
        status["alarm_name"] = "Smart Detection Alarm"
        status["last_alarm_time"] = dt_util.now().strftime("%Y-%m-%d %H:%M:%S")
        return status

    with patch.object(api, "build_status", with_alarm):
        await tick(hass, 16)
    sensor = alarm_sensor(hass, "smart_detection_alarm")
    assert sensor and hass.states.get(sensor).state == "on"
    assert alarm_sensor(hass, "intelligent_detection_alarm") is None


async def test_sensors_of_alarms_never_reported_are_removed(hass: HomeAssistant, client: FakeClient) -> None:
    reg = er.async_get(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    for kind in ("unlock_face", "unlock_palm"):
        reg.async_get_or_create("binary_sensor", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_alarm_{kind}", config_entry=entry)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert alarm_sensor(hass, "unlock_face") is None and alarm_sensor(hass, "unlock_palm") is None
    assert alarm_sensor(hass, "unlock_lock") is not None


async def test_buttons_open_the_right_lock_only(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    for suffix in ("unlock_door", "unlock_gate"):
        await hass.services.async_call(
            "button", "press", {"entity_id": legacy_entity(hass, "button", suffix)}, blocking=True
        )
    assert client.unlocks == [(SERIAL, "internal-user", 2), (SERIAL, "internal-user", 1)]


async def test_lock_numbers_can_be_changed(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client, {"door_lock_no": 5, "gate_lock_no": 6})
    await hass.services.async_call(
        "button", "press", {"entity_id": legacy_entity(hass, "button", "unlock_door")}, blocking=True
    )
    assert client.unlocks == [(SERIAL, "internal-user", 5)]


async def test_double_press_is_ignored(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    door = legacy_entity(hass, "button", "unlock_door")
    for _ in range(2):
        await hass.services.async_call("button", "press", {"entity_id": door}, blocking=True)
    assert len(client.unlocks) == 1


async def test_failed_unlock_raises_and_never_tries_the_other_lock(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    client.fail_unlock = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": legacy_entity(hass, "button", "unlock_door")}, blocking=True
        )
    assert client.unlocks == []


async def test_unlock_services(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    await hass.services.async_call(DOMAIN, "unlock_door", {}, blocking=True)
    await hass.services.async_call(DOMAIN, "unlock_gate", {"serial": SERIAL}, blocking=True)
    assert client.unlocks == [(SERIAL, "internal-user", 2), (SERIAL, "internal-user", 1)]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "unlock_door", {"serial": "NOPE"}, blocking=True)


async def test_snapshot_camera(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    from homeassistant.components.camera import async_get_image

    client.messages = []
    state = hass.states.get(legacy_entity(hass, "camera", "last_snapshot"))
    assert state is not None


async def test_terminal_sensor_counts(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    reg = er.async_get(hass)
    eid = next(e.entity_id for e in reg.entities.values() if e.unique_id.endswith("_terminals"))
    assert hass.states.get(eid).state == "3"


async def test_unload(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert client.mqtt.stopped


async def test_monitor_can_be_given_by_hand(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client, {"monitor_serials": "BE0000001, BE0000003"})
    legacy_entity(hass, "number", "chime_volume_monitor", "BE0000003")
# --- config flow -------------------------------------------------------------


async def test_config_flow_happy_path(hass: HomeAssistant, client: FakeClient) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "user@example.com", "password": "pw", "region": "apiieu.ezvizlife.com"},
        )
        assert result["step_id"] == "devices"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"devices": [SERIAL]})
    assert result["type"] == "create_entry"
    assert result["data"]["devices"] == [SERIAL]
    assert result["data"]["names"] == {SERIAL: "Tor"}


async def test_config_flow_preselects_the_door_station(hass: HomeAssistant, client: FakeClient) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "user@example.com", "password": "pw", "region": "apiieu.ezvizlife.com"},
        )
    schema_key = next(k for k in result["data_schema"].schema if k == "devices")
    assert schema_key.default() == [SERIAL]


async def test_config_flow_wrong_password(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch("custom_components.ezviz_instant_bell.ezviz.login", side_effect=ezviz.InvalidAuth("Incorrect Password.")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "user@example.com", "password": "bad", "region": "apiieu.ezvizlife.com"},
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_config_flow_verification_code(hass: HomeAssistant, client: FakeClient) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch(
        "custom_components.ezviz_instant_bell.ezviz.login",
        side_effect=[ezviz.NeedsCode(client), client],
    ), patch("custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "user@example.com", "password": "pw", "region": "apiieu.ezvizlife.com"},
        )
        assert result["step_id"] == "code"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["step_id"] == "devices"


async def test_second_setup_of_same_account_is_refused(hass: HomeAssistant) -> None:
    make_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": "user@example.com", "password": "pw", "region": "apiieu.ezvizlife.com"},
    )
    assert result["type"] == "abort" and result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"poll_interval": 3, "monitor_serials": "", "door_lock_no": 2, "gate_lock_no": 1},
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options["poll_interval"] == 3


async def test_options_reload_without_update_listener(hass: HomeAssistant, client: FakeClient) -> None:
    # HA 2026.12 drops reloads through update listeners (#23); the options flow reloads itself.
    entry = await setup(hass, client)
    assert not entry.update_listeners
    hub = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"poll_interval": 3, "monitor_serials": "", "door_lock_no": 2, "gate_lock_no": 1},
        )
        await hass.async_block_till_done()
    assert entry.runtime_data is not hub


async def test_options_flow_keeps_the_fetched_keys(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "encryption_keys": {"AAA": "k1"}})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"poll_interval": 3, "monitor_serials": "", "door_lock_no": 2, "gate_lock_no": 1},
        )
        await hass.async_block_till_done()
    assert entry.options["encryption_keys"] == {"AAA": "k1"}


async def setup_as_camera(hass: HomeAssistant, client: FakeClient, entry: MockConfigEntry | None = None):
    """The chosen device, reported by EZVIZ as a camera instead of a door station."""
    if entry is None:
        entry = make_entry()
        entry.add_to_hass(hass)
    camera = {**DEVICES, SERIAL: {"name": "Garden", "category": "IPC"}}
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=camera
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_a_camera_gets_no_unlock_or_chime(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup_as_camera(hass, client)
    assert not entry.runtime_data.is_intercom(SERIAL)
    reg = er.async_get(hass)
    for platform, suffix in (("button", "unlock_door"), ("button", "unlock_gate"), ("switch", "chime_sound"),
                             ("switch", "label_light"), ("number", "chime_volume")):
        assert reg.async_get_entity_id(platform, DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_{suffix}") is None, suffix
    # what every EZVIZ device has stays
    assert reg.async_get_entity_id("switch", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_privacy")
    assert reg.async_get_entity_id("event", DOMAIN, f"{SERIAL}_bell")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "unlock_door", {"serial": SERIAL}, blocking=True)
    assert client.unlocks == []


async def test_unlock_and_chime_of_a_camera_are_removed_on_update(hass: HomeAssistant, client: FakeClient) -> None:
    # Earlier versions made them for every chosen device.
    entry = await setup(hass, client)
    door = legacy_entity(hass, "button", "unlock_door")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await setup_as_camera(hass, client, entry)
    reg = er.async_get(hass)
    assert reg.async_get(door) is None
    assert reg.async_get_entity_id("number", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_chime_volume") is None


async def test_an_unknown_category_keeps_the_intercom_entities(hass: HomeAssistant, client: FakeClient) -> None:
    # EZVIZ did not list the device (or an older install): nothing is taken away.
    entry = make_entry()
    entry.add_to_hass(hass)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value={}
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.runtime_data.is_intercom(SERIAL)
    assert legacy_entity(hass, "button", "unlock_door")


# --- live video -----------------------------------------------------------------


async def test_live_camera_has_the_old_unique_id(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    eid = legacy_entity(hass, "camera", "live")
    state = hass.states.get(eid)
    assert state is not None
    assert state.attributes["stream_mode"] == "mjpeg"
    assert SERIAL in entry.runtime_data.live_relays
    # the relay listens only on this computer unless told otherwise
    assert entry.runtime_data.live_relays[SERIAL]._bind_host == "127.0.0.1"


async def test_unload_stops_the_relay(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    hub = entry.runtime_data
    assert hub.live_relays
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hub.live_relays == {}


async def test_live_video_failure_does_not_break_the_rest(hass: HomeAssistant, client: FakeClient) -> None:
    async def boom(*_a, **_k):
        raise RuntimeError("no ffmpeg")

    with patch("custom_components.ezviz_instant_bell.live_camera.async_setup_live_entities", boom):
        await setup(hass, client)
    assert hass.states.get(legacy_entity(hass, "camera", "last_snapshot")) is not None


async def test_stream_options_reach_the_relay(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(
        hass, client, {"stream_source": "local", "stream_quality": "sub", "encryption_key": "ABC123", "relay_port": 0}
    )
    relay = entry.runtime_data.live_relays[SERIAL]
    assert relay._stream_source == "local" and relay._stream_quality == "sub"
    assert relay._configured_key == "ABC123"


async def options_with(hass: HomeAssistant, client: FakeClient, entry, **values) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"poll_interval": 3, "monitor_serials": "", "door_lock_no": 2, "gate_lock_no": 1, **values},
        )
        await hass.async_block_till_done()
    return result


async def test_sharing_the_stream_is_off_and_keeps_its_token(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    result = await options_with(hass, client, entry)
    token = entry.options["stream_token"]
    assert entry.options["share_stream"] is False and len(token) > 30
    # the form shows the address with the token that is saved
    assert f"/api/{DOMAIN}/live/{SERIAL}?token={token}" in result["description_placeholders"]["stream_addresses"]
    await options_with(hass, client, entry, share_stream=True)
    assert entry.options["share_stream"] is True and entry.options["stream_token"] == token
    await options_with(hass, client, entry, share_stream=True, new_stream_token=True)
    assert entry.options["stream_token"] != token


async def test_shared_stream_needs_the_option_and_the_token(
    hass: HomeAssistant, client: FakeClient, hass_client_no_auth
) -> None:
    async def fake_relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"MPEG-TS")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(fake_relay, "127.0.0.1", 0)
    entry = await setup(hass, client, {"stream_token": "secret-token"})
    relay = entry.runtime_data.live_relays[SERIAL]
    http = await hass_client_no_auth()
    url = f"/api/{DOMAIN}/live/{SERIAL}"
    assert (await http.get(f"{url}?token=secret-token")).status == 403  # off by default

    hass.config_entries.async_update_entry(entry, options={**entry.options, "share_stream": True})
    assert (await http.get(f"{url}?token=wrong")).status == 401
    assert (await http.get(url)).status == 401
    with patch.object(relay, "_port", server.sockets[0].getsockname()[1]):
        response = await http.get(f"{url}?token=secret-token")
        assert response.status == 200 and response.content_type == "video/mp2t"
        assert await response.read() == b"MPEG-TS"
    server.close()
    await server.wait_closed()


async def test_the_right_token_is_never_a_failed_login(
    hass: HomeAssistant, client: FakeClient, hass_client_no_auth
) -> None:
    # go2rtc and Frigate keep trying while Home Assistant starts or the cloud is down.
    entry = await setup(hass, client, {"stream_token": "secret-token", "share_stream": True})
    http = await hass_client_no_auth()
    with patch("custom_components.ezviz_instant_bell.stream_share.process_wrong_login") as wrong_login:
        assert (await http.get(f"/api/{DOMAIN}/live/OTHER123?token=secret-token")).status == 404
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert (await http.get(f"/api/{DOMAIN}/live/{SERIAL}?token=secret-token")).status == 503
        assert not wrong_login.called
        assert (await http.get(f"/api/{DOMAIN}/live/{SERIAL}?token=wrong")).status == 401
        assert wrong_login.called


async def test_shared_mjpeg_reads_the_relay(hass: HomeAssistant, client: FakeClient, hass_client_no_auth) -> None:
    from aiohttp import web

    entry = await setup(hass, client, {"stream_token": "secret-token", "share_stream": True})
    relay = entry.runtime_data.live_relays[SERIAL]
    http = await hass_client_no_auth()
    url = f"/api/{DOMAIN}/mjpeg/{SERIAL}"
    with patch.object(relay, "prewarm") as prewarm, patch(
        "custom_components.ezviz_instant_bell.mjpeg.serve_mjpeg", return_value=web.Response(text="frames")
    ) as serve:
        assert (await http.get(f"{url}?token=wrong")).status == 401
        assert not serve.called
        response = await http.get(f"{url}?token=secret-token")
        assert response.status == 200 and await response.text() == "frames"
    prewarm.assert_awaited_once()
    assert serve.call_args.kwargs["upstream_url"] == relay.stream_url


async def test_diagnostics_hide_the_stream_token(hass: HomeAssistant, client: FakeClient) -> None:
    from custom_components.ezviz_instant_bell.diagnostics import async_get_config_entry_diagnostics

    entry = await setup(hass, client, {"stream_token": "secret-token"})
    assert "secret-token" not in json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)


async def test_ring_wakes_up_the_stream(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    relay = entry.runtime_data.live_relays[SERIAL]
    with patch.object(relay, "prewarm", return_value=None) as prewarm:
        client.messages = [ring_message("m-live")]
        await poll_once(hass)
    assert prewarm.called


async def test_fetch_encryption_key_service_keeps_the_key(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    await hass.services.async_call(DOMAIN, "fetch_encryption_key", {"code": "1234"}, blocking=True)
    await hass.async_block_till_done()
    assert client.cam_key_calls[0][1] == "1234"
    assert entry.options["encryption_key"] == "KEY123"


async def test_fetch_encryption_key_without_code_asks_for_one(hass: HomeAssistant, client: FakeClient) -> None:
    from pyezvizapi.exceptions import EzvizAuthVerificationCode

    entry = await setup(hass, client)
    hub = entry.runtime_data
    client.cam_key_answers = [EzvizAuthVerificationCode("20002"), EzvizAuthVerificationCode("20002")]
    key = await hass.async_add_executor_job(hub.stream.get_camera_encryption_key, SERIAL)
    assert key is None
    assert "BE0000001" in hub.stream.key_needs_otp
    # a code later unlocks the key
    client.cam_key_answers = ["KEY999"]
    key = await hass.async_add_executor_job(hub.stream.fetch_camera_key_with_code, SERIAL, "4321")
    assert key == "KEY999" and not hub.stream.key_needs_otp
    assert await hass.async_add_executor_job(hub.stream.get_camera_encryption_key, SERIAL) == "KEY999"


async def test_encryption_repair_flow(hass: HomeAssistant, client: FakeClient) -> None:
    from custom_components.ezviz_instant_bell.repairs import async_create_fix_flow

    entry = await setup(hass, client)
    flow = await async_create_fix_flow(hass, f"encryption_otp_{SERIAL}", {"serial": SERIAL})
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "form" and result["step_id"] == "confirm"
    # a wrong code is refused (first: storing a key reloads the entry)
    client.cam_key_answers = [RuntimeError("bad"), RuntimeError("bad")]
    result = await flow.async_step_confirm({"code": "0000"})
    assert result["type"] == "form" and result["errors"] == {"code": "invalid_code"}
    client.cam_key_answers = ["KEY123"]
    result = await flow.async_step_confirm({"code": "1234"})
    assert result["type"] == "create_entry"
    assert entry.options["encryption_key"] == "KEY123"
    assert entry.options["encryption_keys"] == {"BE0000001": "KEY123"}


async def test_set_video_encryption_service(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    await hass.services.async_call(
        DOMAIN, "set_video_encryption", {"enable": False, "verification_code": "abcdef"}, blocking=True
    )
    first = client.video_enc_calls[0]
    assert first["serial"] == "BE0000001" and first["enable"] == 0 and first["code"] == "ABCDEF"
    client.fail_video_enc = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "set_video_encryption", {"enable": False, "verification_code": "abcdef"}, blocking=True
        )


async def test_lan_key_and_ip(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    support = entry.runtime_data.stream

    class FakeCas:
        def __init__(self, token):
            self.token = token

        def cas_get_encryption(self, serial):
            assert serial == "BE0000001"
            return {"Response": {"Session": {"@Key": "0123456789abcdef"}}}

    class Endpoint:
        host = "192.168.1.50"

    with patch("pyezvizapi.cas.EzvizCAS", FakeCas), patch("requests.post") as post, patch(
        "pyezvizapi.local_stream._local_sdk_endpoint_from_client", return_value=Endpoint()
    ):
        key = await hass.async_add_executor_job(support.fetch_lan_aes_key, SERIAL)
        ip = await hass.async_add_executor_job(support.get_local_ip, SERIAL)
    assert key == b"0123456789abcdef" and ip == "192.168.1.50"
    assert post.call_args.kwargs["data"] == {"sessionId": "sid"}
    assert support.get_related_device(SERIAL) == "BE0000002"
    # the address is remembered when the cloud does not answer
    with patch("pyezvizapi.local_stream._local_sdk_endpoint_from_client", side_effect=RuntimeError("504")):
        assert await hass.async_add_executor_job(support.get_local_ip, SERIAL) == "192.168.1.50"


async def test_lan_key_refused_gives_a_hint(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    support = entry.runtime_data.stream

    class FakeCas:
        def __init__(self, token):
            pass

        def cas_get_encryption(self, serial):
            return {"Response": {"Result": "1052175"}}

    with patch("pyezvizapi.cas.EzvizCAS", FakeCas), patch("requests.post"):
        with pytest.raises(RuntimeError, match="Encryption"):
            await hass.async_add_executor_job(support.fetch_lan_aes_key, SERIAL)


async def test_status_is_read_less_often_while_watching_locally(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    hub = entry.runtime_data
    relay = hub.live_relays[SERIAL]
    base = hub.coordinator.update_interval
    relay._active_lan = True
    relay._active_clients = 1
    hub.coordinator._apply_stream_aware_interval()
    assert hub.coordinator.update_interval == base * 4
    relay._active_clients = 0
    hub.coordinator._apply_stream_aware_interval()
    assert hub.coordinator.update_interval == base


# --- the optional buttons of version 0.1.0 ------------------------------------


async def test_old_open_buttons_come_back_with_their_entity_ids(hass: HomeAssistant, client: FakeClient) -> None:
    """A phone widget on button.<name>_open_gate must keep working."""
    await setup(hass, client, {"create_buttons": True})
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_open_gate") == "button.tor_open_gate"
    assert reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_open_door") == "button.tor_open_door"
    await hass.services.async_call("button", "press", {"entity_id": "button.tor_open_gate"}, blocking=True)
    assert client.unlocks == [(SERIAL, "internal-user", 1)]
    await hass.services.async_call("button", "press", {"entity_id": "button.tor_open_door"}, blocking=True)
    assert client.unlocks[-1] == (SERIAL, "internal-user", 2)


async def test_old_open_buttons_in_the_registry_are_kept(hass: HomeAssistant, client: FakeClient) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "button", DOMAIN, f"{SERIAL}_open_gate", config_entry=entry, suggested_object_id="hof_klingel_open_gate"
    )
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_open_gate") == "button.hof_klingel_open_gate"
    assert hass.states.get("button.hof_klingel_open_gate") is not None
    assert reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_open_door") is None


async def test_no_old_open_buttons_for_new_setups(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_open_gate") is None


# --- history in the media browser ---------------------------------------------


async def browse(hass, identifier: str = ""):
    from homeassistant.components.media_source import async_browse_media
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "media_source", {})
    await hass.async_block_till_done()
    return await async_browse_media(hass, f"media-source://{DOMAIN}/{identifier}".rstrip("/"))


async def test_media_browser_levels(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    root = await browse(hass)
    assert [c.title for c in root.children] == [entry.title]
    doors = await browse(hass, entry.entry_id)
    assert [c.title for c in doors.children] == ["Tor"]
    kinds = await browse(hass, f"{entry.entry_id}/{SERIAL}")
    assert [c.title for c in kinds.children] == ["Recordings (SD card)", "Pictures of events"]
    days = await browse(hass, f"{entry.entry_id}/{SERIAL}/pic")
    assert len(days.children) == 14 and days.children[0].title.startswith("Today")
    today = dt_util.now().strftime("%Y%m%d")
    client.messages = [ring_message("m-old", 600), ring_message("m-new", 5)]
    events = await browse(hass, f"{entry.entry_id}/{SERIAL}/pic/{today}")
    assert [c.identifier.rsplit("/", 1)[1] for c in events.children] == ["m-new", "m-old"]
    first = events.children[0]
    assert first.media_class == "image" and first.can_play and "Your doorbell is ringing" in first.title
    assert first.thumbnail == f"/api/{DOMAIN}/picture/{entry.entry_id}/{SERIAL}/{today}/m-new"
    # the list is read for this door station only
    assert client.message_calls[-1][0] == SERIAL


async def test_media_browser_resolves_and_serves_the_picture(
    hass: HomeAssistant, client: FakeClient, hass_client
) -> None:
    from homeassistant.components.media_source import async_resolve_media

    entry = await setup(hass, client)
    await browse(hass)
    today = dt_util.now().strftime("%Y%m%d")
    client.messages = [ring_message("m-1", 5)]
    played = await async_resolve_media(
        hass, f"media-source://{DOMAIN}/{entry.entry_id}/{SERIAL}/pic/{today}/m-1", None
    )
    assert played.mime_type == "image/jpeg"
    http = await hass_client()
    response = await http.get(played.url)
    assert response.status == 200 and await response.read() == b"JPEGDATA"
    # unknown event
    assert (await http.get(f"/api/{DOMAIN}/picture/{entry.entry_id}/{SERIAL}/{today}/nope")).status == 404
    # unknown door station
    assert (await http.get(f"/api/{DOMAIN}/picture/{entry.entry_id}/XXX/{today}/m-1")).status == 404


async def test_history_reads_several_pages_and_keeps_the_answer(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    hub = entry.runtime_data
    first_page = [ring_message(f"m{i}", 100 + i) for i in range(50)]
    second_page = [ring_message("m-last", 400)]
    client.history_pages = {"": first_page, "m49": second_page}
    items = await hub.async_history_day(SERIAL, dt_util.now().date())
    assert len(items) == 51 and items[0].message_id == "m0" and items[-1].message_id == "m-last"
    calls = len(client.message_calls)
    await hub.async_history_day(SERIAL, dt_util.now().date())
    assert len(client.message_calls) == calls  # kept for a minute


async def test_history_failure_gives_an_error(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    hub = entry.runtime_data

    def boom(*_a, **_k):
        raise RuntimeError("cloud down")

    client.get_device_messages_list = boom
    with pytest.raises(HomeAssistantError):
        await hub.async_history_day(SERIAL, dt_util.now().date() - timedelta(days=2))


async def test_history_includes_entries_that_name_the_device_in_from(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    unlock = ring_message("m-unlock", 30)
    unlock.update({"subType": 2401, "from": "Tor", "deviceSerial": SERIAL, "title": "EZVIZ app open the gate"})
    client.messages = [unlock]
    items = await entry.runtime_data.async_history_day(SERIAL, dt_util.now().date())
    assert [i.message_id for i in items] == ["m-unlock"]


# --- recordings of the SD card -------------------------------------------------


def record(day, hhmmss: str, seconds: int = 49) -> dict:
    begin = datetime.strptime(f"{day.strftime('%Y-%m-%d')}T{hhmmss}", "%Y-%m-%dT%H:%M:%S")
    return {
        "channelType": "D",
        "begin": begin.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": (begin + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S"),
        "type": "TIMING",
    }


async def test_media_browser_lists_recordings_of_the_day(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    day = dt_util.now().date()
    client.records = [record(day, "08:00:10"), record(day, "15:42:03")]
    clips = await browse(hass, f"{entry.entry_id}/{SERIAL}/rec/{day.strftime('%Y%m%d')}")
    assert [c.title for c in clips.children] == ["15:42:03  (49 s)", "08:00:10  (49 s)"]  # newest first
    first = clips.children[0]
    assert first.media_class == "video" and first.can_play and first.media_content_type == "video/mp4"
    assert first.identifier.endswith(f"/rec/{day.strftime('%Y%m%d')}/{day.strftime('%Y%m%d')}T154203Z_{day.strftime('%Y%m%d')}T154252Z")
    # asked for this door station, its camera part, the whole day
    serial, channel, cam, start, stop, _size = client.record_calls[-1]
    assert (serial, channel, cam) == (SERIAL, 1, "BE0000002")
    assert start.endswith("T00:00:00") and stop.endswith("T23:59:59")


async def test_recordings_use_the_picture_of_the_event_as_thumbnail(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    day = dt_util.now().date()
    ring = ring_message("m-ring", 5)
    ring_time = dt_util.now() - timedelta(seconds=5)
    client.messages = [ring]
    client.records = [record(day, ring_time.strftime("%H:%M:%S"))]
    clips = await browse(hass, f"{entry.entry_id}/{SERIAL}/rec/{day.strftime('%Y%m%d')}")
    if day == ring_time.date():  # near midnight the ring belongs to yesterday
        assert clips.children[0].thumbnail == (
            f"/api/{DOMAIN}/picture/{entry.entry_id}/{SERIAL}/{day.strftime('%Y%m%d')}/m-ring"
        )


async def test_recordings_come_in_pages(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    day = dt_util.now().date()
    # 60 recordings, 50 per answer: the second request starts where the first ended
    client.records = [record(day, f"{h:02d}:{m:02d}:00", 20) for h in range(10) for m in range(0, 60, 10)]
    items = await entry.runtime_data.async_recordings_day(SERIAL, day)
    assert len(items) == 60 and len(client.record_calls) == 2


async def test_recordings_failure_gives_an_error(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    client.fail_records = True
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.async_recordings_day(SERIAL, dt_util.now().date() - timedelta(days=3))


async def test_clip_is_resolved_and_a_finished_clip_is_served(
    hass: HomeAssistant, client: FakeClient, hass_client
) -> None:
    from homeassistant.components.media_source import async_resolve_media

    entry = await setup(hass, client)
    await browse(hass)
    hub = entry.runtime_data
    day = dt_util.now().date().strftime("%Y%m%d")
    clip = f"{day}T154203Z_{day}T154252Z"
    played = await async_resolve_media(
        hass, f"media-source://{DOMAIN}/{entry.entry_id}/{SERIAL}/rec/{day}/{clip}", None
    )
    assert played.mime_type == "video/mp4"
    assert played.url == f"/api/{DOMAIN}/clip/{entry.entry_id}/{SERIAL}/{clip}"
    begin, end = parse_clip_id(clip)
    path = hub.clips.path_for(SERIAL, begin, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MP4DATA")
    http = await hass_client()
    response = await http.get(played.url)
    assert response.status == 200 and await response.read() == b"MP4DATA"
    assert response.headers["Content-Type"] == "video/mp4"
    # nonsense is refused
    assert (await http.get(f"/api/{DOMAIN}/clip/{entry.entry_id}/{SERIAL}/nonsense")).status == 404
    assert (await http.get(f"/api/{DOMAIN}/clip/{entry.entry_id}/XXX/{clip}")).status == 404


async def test_a_clip_is_handed_out_while_it_arrives(hass: HomeAssistant, client: FakeClient, hass_client) -> None:
    entry = await setup(hass, client)
    await browse(hass)  # loads the media source, which registers the web view
    hub = entry.runtime_data
    day = dt_util.now().date().strftime("%Y%m%d")
    clip = f"{day}T154203Z_{day}T154252Z"

    def fetch(job, serial, begin, end):
        import time as _time

        job.state = "streaming"
        job.part_path.parent.mkdir(parents=True, exist_ok=True)
        with job.part_path.open("wb") as out:
            for piece in (b"AAAA", b"BBBB", b"CCCC"):
                out.write(piece)
                out.flush()
                _time.sleep(0.3)
        job.state = "finalizing"
        job.part_path.replace(job.final_path)
        job.state = "done"

    http = await hass_client()
    with patch.object(hub.clips, "_fetch", fetch):
        response = await http.get(f"/api/{DOMAIN}/clip/{entry.entry_id}/{SERIAL}/{clip}")
        assert response.status == 200
        assert await response.read() == b"AAAABBBBCCCC"
        await hass.async_block_till_done(wait_background_tasks=True)
    begin, end = parse_clip_id(clip)
    assert hub.clips.ready_file(SERIAL, begin, end) is not None  # the next viewer gets the file


async def test_a_clip_that_cannot_be_fetched_gives_an_error(hass: HomeAssistant, client: FakeClient, hass_client) -> None:
    from custom_components.ezviz_instant_bell.clip_stream import ClipUnavailable

    entry = await setup(hass, client)
    await browse(hass)  # loads the media source, which registers the web view
    hub = entry.runtime_data
    day = dt_util.now().date().strftime("%Y%m%d")
    clip = f"{day}T154203Z_{day}T154252Z"

    def fetch(job, serial, begin, end):
        raise ClipUnavailable("No video arrived")

    http = await hass_client()
    with patch.object(hub.clips, "_fetch", fetch):
        response = await http.get(f"/api/{DOMAIN}/clip/{entry.entry_id}/{SERIAL}/{clip}")
    assert response.status == 502 and "No video" in await response.text()
    begin, end = parse_clip_id(clip)
    assert hub.clips.ready_file(SERIAL, begin, end) is None
    assert not list(hub.clips.root.glob("*/*.part.mp4"))


async def test_old_clips_are_removed_when_the_folder_is_too_big(hass: HomeAssistant, client: FakeClient) -> None:
    import os

    entry = await setup(hass, client)
    store = entry.runtime_data.clips
    folder = store.root / "S"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        path = folder / f"c{index}.mp4"
        path.write_bytes(b"x" * 100)
        os.utime(path, (1000 + index, 1000 + index))
    with patch("custom_components.ezviz_instant_bell.clips.CLIP_CACHE_MAX_BYTES", 150):
        store.prune()
    assert sorted(p.name for p in folder.glob("*.mp4")) == ["c2.mp4"]


async def test_clip_is_fetched_converted_and_kept(hass: HomeAssistant, client: FakeClient, tmp_path) -> None:
    """The whole way with a stand-in for ffmpeg: video in, growing file, final file."""
    entry = await setup(hass, client)
    hub = entry.runtime_data
    fake = tmp_path / "fake_ffmpeg"
    fake.write_text(
        "#!/bin/sh\n"
        'for last; do :; done\n'
        'case "$*" in *pipe:0*) cat > "$last";;\n'
        '*) while [ $# -gt 0 ]; do [ "$1" = -i ] && src="$2"; shift; done; cp "$src" "$last";; esac\n'
    )
    fake.chmod(0o755)

    class Playback:
        closed = False

        def close(self):
            Playback.closed = True

    begin, end = parse_clip_id("20261028T154203Z_20261028T154252Z")
    with patch.object(hub.clips, "_ffmpeg", return_value=str(fake)), patch(
        "custom_components.ezviz_instant_bell.clips.open_playback", return_value=Playback()
    ) as opened, patch(
        "custom_components.ezviz_instant_bell.clips.iter_media", return_value=iter([b"one-", b"two-", b"three"])
    ):
        job = hub.clips.job_for(SERIAL, begin, end)
        assert hub.clips.job_for(SERIAL, begin, end) is job  # a second viewer joins the same job
        await asyncio.wait_for(job.done.wait(), 10)
    assert job.state == "done" and job.bytes_in == 13 and Playback.closed
    assert opened.call_args.args[1:3] == (SERIAL, 1)
    ready = hub.clips.ready_file(SERIAL, begin, end)
    assert ready is not None and ready.read_bytes() == b"one-two-three"
    assert not list(hub.clips.root.glob("*/*.part.mp4")) and not list(hub.clips.root.glob("*/*.tmp.mp4"))


async def test_a_clip_without_video_is_not_kept(hass: HomeAssistant, client: FakeClient, tmp_path) -> None:
    entry = await setup(hass, client)
    hub = entry.runtime_data
    fake = tmp_path / "fake_ffmpeg"
    fake.write_text('#!/bin/sh\nfor last; do :; done\ncat > "$last"\n')
    fake.chmod(0o755)

    class Playback:
        def close(self):
            pass

    begin, end = parse_clip_id("20261028T154203Z_20261028T154252Z")
    with patch.object(hub.clips, "_ffmpeg", return_value=str(fake)), patch(
        "custom_components.ezviz_instant_bell.clips.open_playback", return_value=Playback()
    ), patch("custom_components.ezviz_instant_bell.clips.iter_media", return_value=iter([])):
        job = hub.clips.job_for(SERIAL, begin, end)
        await asyncio.wait_for(job.done.wait(), 10)
    assert job.state == "failed" and job.error
    assert hub.clips.ready_file(SERIAL, begin, end) is None
    assert hub.clips.job_for(SERIAL, begin, end) is not job  # a new try is possible


# --- the ring notification blueprint -------------------------------------------


BLUEPRINT = Path(__file__).parents[1] / "blueprints/automation/ezviz_instant_bell/ring_notification.yaml"


async def setup_blueprint_automation(hass: HomeAssistant, entry, phones, picture_wait=5):
    import shutil

    from homeassistant.setup import async_setup_component

    target = Path(hass.config.path("blueprints/automation/ezviz_instant_bell/ring_notification.yaml"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, target)
    config = {
        "automation": {
            "alias": "Ring",
            "use_blueprint": {
                "path": "ezviz_instant_bell/ring_notification.yaml",
                "input": {
                    "bell": entity_id(hass, "event", "bell"),
                    "picture": entity_id(hass, "image", "last_picture"),
                    "open_button": "button.open_door",
                    "notify_service": phones,
                    "picture_wait": picture_wait,
                    "tap_wait": 1,
                },
            },
        }
    }
    assert await async_setup_component(hass, "automation", config)
    await hass.async_block_till_done()
    return target


@pytest.mark.parametrize("phones", [["notify.mobile_app_a", "notify.mobile_app_b"], "notify.mobile_app_a"])
async def test_blueprint_sends_the_notification_on_the_first_ring(
    hass: HomeAssistant, client: FakeClient, phones
) -> None:
    from pytest_homeassistant_custom_component.common import async_mock_service

    entry = await setup(hass, client)
    notify_a = async_mock_service(hass, "notify", "mobile_app_a")
    notify_b = async_mock_service(hass, "notify", "mobile_app_b")
    target = await setup_blueprint_automation(hass, entry, phones)
    try:
        client.messages = [ring_message("first")]
        await poll_once(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        await tick(hass, 1)
        assert len(notify_a) == 1
        assert len(notify_b) == (1 if isinstance(phones, list) else 0)
        data = notify_a[0].data
        assert data["title"] == "Someone is at the door"
        assert data["data"]["actions"][0]["title"] == "Open door"
        assert data["data"]["image"].startswith("/api/image_proxy/")
    finally:
        target.unlink(missing_ok=True)


# --- the ring blueprint variants (issue #10) ----------------------------------


async def setup_blueprint_file(hass: HomeAssistant, blueprint: Path, filename: str, inputs: dict):
    import shutil

    from homeassistant.setup import async_setup_component

    target = Path(hass.config.path(f"blueprints/automation/ezviz_instant_bell/{filename}"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(blueprint, target)
    config = {
        "automation": {
            "alias": "Ring variant",
            "use_blueprint": {
                "path": f"ezviz_instant_bell/{filename}",
                "input": inputs,
            },
        }
    }
    assert await async_setup_component(hass, "automation", config)
    await hass.async_block_till_done()
    return target


PERSISTENT_BLUEPRINT = (
    Path(__file__).parents[1] / "blueprints/automation/ezviz_instant_bell/ring_persistent_notification.yaml"
)
SPEAKER_BLUEPRINT = (
    Path(__file__).parents[1] / "blueprints/automation/ezviz_instant_bell/ring_speaker_announcement.yaml"
)


async def test_persistent_notification_blueprint_notifies_on_ring(
    hass: HomeAssistant, client: FakeClient
) -> None:
    from pytest_homeassistant_custom_component.common import async_mock_service

    await setup(hass, client)
    create = async_mock_service(hass, "persistent_notification", "create")
    target = await setup_blueprint_file(
        hass,
        PERSISTENT_BLUEPRINT,
        "ring_persistent_notification.yaml",
        {
            "bell": entity_id(hass, "event", "bell"),
            "picture": entity_id(hass, "image", "last_picture"),
            "picture_wait": 5,
            "auto_dismiss": 0,
        },
    )
    try:
        client.messages = [ring_message("first")]
        await poll_once(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        await tick(hass, 1)
        assert len(create) == 1
        data = create[0].data
        assert data["notification_id"] == "ezviz_instant_bell_ring"
        assert data["title"] == "Someone is at the door"
        assert data["message"].startswith("The doorbell rang at ")
        assert "![Doorbell picture](/api/image_proxy/" in data["message"]
    finally:
        target.unlink(missing_ok=True)


async def test_speaker_announcement_blueprint_speaks_on_ring(
    hass: HomeAssistant, client: FakeClient
) -> None:
    from pytest_homeassistant_custom_component.common import async_mock_service

    await setup(hass, client)
    speak = async_mock_service(hass, "tts", "speak")
    volume = async_mock_service(hass, "media_player", "volume_set")
    target = await setup_blueprint_file(
        hass,
        SPEAKER_BLUEPRINT,
        "ring_speaker_announcement.yaml",
        {
            "bell": entity_id(hass, "event", "bell"),
            "speaker": ["media_player.kitchen"],
            "tts_entity": "tts.google_translate_en_com",
            "message": "Someone is at the door",
            "adjust_volume": False,
            "volume_level": 0.5,
        },
    )
    try:
        client.messages = [ring_message("first")]
        await poll_once(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        await tick(hass, 1)
        assert len(speak) == 1
        data = speak[0].data
        assert data["message"] == "Someone is at the door"
        assert data["media_player_entity_id"] == ["media_player.kitchen"]
        assert len(volume) == 0  # adjust_volume is off: the volume is left as it is
    finally:
        target.unlink(missing_ok=True)


# --- visitor log card ----------------------------------------------------------


async def test_visitor_log_lists_events_with_picture_and_clip(hass: HomeAssistant, client: FakeClient, hass_ws_client) -> None:
    entry = await setup(hass, client)
    day = dt_util.now().date()
    ring_time = dt_util.now() - timedelta(seconds=5)
    client.messages = [ring_message("m-ring", 5)]
    client.records = [record(day, ring_time.strftime("%H:%M:%S"))]
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/visitor_log", "days": 1})
    answer = (await ws.receive_json())["result"]
    assert answer["doors"] == [{"serial": SERIAL, "name": "Tor"}] and answer["partial"] is False
    ring = next(i for i in answer["items"] if i["id"] == "m-ring")
    assert ring["kind"] == "ring" and ring["serial"] == SERIAL
    assert ring["picture"].startswith(f"/api/{DOMAIN}/picture/{entry.entry_id}/{SERIAL}/") and "authSig=" in ring["picture"]
    if day == ring_time.date():  # near midnight the ring belongs to yesterday
        assert ring["clip"].startswith(f"/api/{DOMAIN}/clip/{entry.entry_id}/{SERIAL}/") and "authSig=" in ring["clip"]
        assert ring["clip_seconds"] == 49


async def test_visitor_log_works_without_the_sd_card_list(hass: HomeAssistant, client: FakeClient, hass_ws_client) -> None:
    await setup(hass, client)
    client.messages = [ring_message("m-ring", 5)]
    client.fail_records = True
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/visitor_log"})
    items = (await ws.receive_json())["result"]["items"]
    assert [i["id"] for i in items] == ["m-ring"] and items[0]["clip"] is None


async def test_visitor_log_refuses_an_unknown_door_and_bad_days(hass: HomeAssistant, client: FakeClient, hass_ws_client) -> None:
    await setup(hass, client)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/visitor_log", "serial": "NOPE"})
    assert (await ws.receive_json())["error"]["code"] == "not_found"
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/visitor_log", "days": 99})
    assert (await ws.receive_json())["success"] is False


async def test_the_card_script_is_served(hass: HomeAssistant, client: FakeClient, hass_client) -> None:
    await setup(hass, client)
    http = await hass_client()
    response = await http.get(f"/{DOMAIN}_static/ezviz-visitor-log-card.js")
    assert response.status == 200 and "ezviz-visitor-log-card" in await response.text()


async def test_the_card_is_added_to_the_frontend(hass: HomeAssistant, client: FakeClient) -> None:
    # The real frontend can't be set up in tests (hass_frontend isn't installed),
    # so capture the callback and drive it as the frontend would.
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    captured: dict = {}

    def fake_when_setup(_hass, component, cb):
        captured[component] = cb

    with patch("custom_components.ezviz_instant_bell.api.async_when_setup", fake_when_setup):
        entry = await setup(hass, client)
    assert "frontend" in captured  # the card waits for the frontend, in any start order
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    # Without dashboard resources kept by the UI the page loads the card.
    await captured["frontend"](hass, "frontend")
    card = f"/{DOMAIN}_static/ezviz-visitor-log-card.js?v="
    assert [url for url in hass.data[DATA_EXTRA_MODULE_URL] if url.startswith(card)]

    # With them, the card goes into the resources instead.
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    assert await async_setup_component(hass, "lovelace", {})
    await captured["frontend"](hass, "frontend")
    assert not hass.data[DATA_EXTRA_MODULE_URL]
    resources = hass.data["lovelace"].resources
    assert [item for item in resources.async_items() if item["url"].startswith(card)]

    # Removing the account takes it out again.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not [item for item in resources.async_items() if item["url"].startswith(card)]


# --- a device value the library does not know (issue #1: CP5 reports sound mode 5) -----


async def test_setup_survives_a_sound_mode_the_library_does_not_know(hass: HomeAssistant, client: FakeClient) -> None:
    client.infos[SERIAL]["STATUS"]["alarmSoundMode"] = 5
    entry = await setup(hass, client)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.last_update_success


# --- diagnostics ------------------------------------------------------------------


async def test_diagnostics_have_no_secrets(hass: HomeAssistant, client: FakeClient) -> None:
    from custom_components.ezviz_instant_bell.diagnostics import async_get_config_entry_diagnostics

    entry = await setup(hass, client, {"encryption_key": "SECRETKEY", "monitor_serials": MONITOR})
    text = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    for secret in ("SECRETKEY", entry.data["password"], entry.data["username"], SERIAL, MONITOR, "BE0000002"):
        assert secret not in text, secret
    assert '"door_1"' in text and "pyezvizapi" in text


# --- saving the clip of a ring --------------------------------------------------------


def fake_ffmpeg(tmp_path) -> str:
    fake = tmp_path / "fake_ffmpeg"
    fake.write_text(
        "#!/bin/sh\n"
        'for last; do :; done\n'
        'case "$*" in *pipe:0*) cat > "$last";;\n'
        '*) while [ $# -gt 0 ]; do [ "$1" = -i ] && src="$2"; shift; done; cp "$src" "$last";; esac\n'
    )
    fake.chmod(0o755)
    return str(fake)


class _Playback:
    def close(self):
        pass


async def test_a_ring_saves_its_clip_and_says_where_it_is(hass: HomeAssistant, client: FakeClient, tmp_path) -> None:
    day = dt_util.now().date()
    ring_time = dt_util.now() - timedelta(seconds=5)
    if ring_time.date() != day:  # the minute around midnight: the recording would be on the other day
        return
    client.records = [record(day, ring_time.strftime("%H:%M:%S"))]
    entry = await setup(hass, client, {"save_clips": True})
    hub = entry.runtime_data
    saved = []
    hass.bus.async_listen("ezviz_instant_bell_clip_saved", lambda event: saved.append(event.data))
    with patch("custom_components.ezviz_instant_bell.hub.CLIP_SAVE_FIRST_WAIT", 0), patch(
        "custom_components.ezviz_instant_bell.hub.CLIP_SAVE_POLL", 0.01
    ), patch.object(hub.clips, "_ffmpeg", return_value=fake_ffmpeg(tmp_path)), patch(
        "custom_components.ezviz_instant_bell.clips.open_playback", return_value=_Playback()
    ), patch(
        "custom_components.ezviz_instant_bell.clips.iter_media", side_effect=lambda *a, **k: iter([b"video"])
    ):
        client.messages = [ring_message("m-ring", 5)]
        await poll_once(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert len(saved) == 1
    info = saved[0]
    begin, end = parse_clip_id(info["clip_id"])
    assert hub.clips.ready_file(SERIAL, begin, end).read_bytes() == b"video"
    assert info["media_content_id"].startswith(f"media-source://{DOMAIN}/{entry.entry_id}/{SERIAL}/rec/")
    assert info["message_id"] == "m-ring" and info["seconds"] == 49 and info["bytes"] == 5
    state = hass.states.get(entity_id(hass, "sensor", "last_clip"))
    assert state.attributes["media_content_id"] == info["media_content_id"]
    assert state.state != "unknown"


async def test_no_clip_is_saved_unless_the_option_is_on(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    saved = []
    hass.bus.async_listen("ezviz_instant_bell_clip_saved", lambda event: saved.append(event))
    client.messages = [ring_message("m-ring", 5)]
    await poll_once(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert saved == [] and entry.runtime_data.last_clip == {}
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_last_clip") is None


async def test_a_ring_without_a_recording_gives_up_quietly(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client, {"save_clips": True})
    with patch("custom_components.ezviz_instant_bell.hub.CLIP_SAVE_FIRST_WAIT", 0), patch(
        "custom_components.ezviz_instant_bell.hub.CLIP_SAVE_POLL", 0.01
    ), patch("custom_components.ezviz_instant_bell.hub.CLIP_SAVE_GIVE_UP", 0.05):
        client.messages = [ring_message("m-ring", 5)]
        await poll_once(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.last_clip == {}


# --- pictures of motion and person events ---------------------------------------------


def motion_message(msg_id: str, age_seconds: int = 0, **changes) -> dict:
    return {**ring_message(msg_id, age_seconds), "subType": 2401, "title": "Motion Detection Alarm", **changes}


async def test_a_motion_picture_is_saved_in_the_media_folder(hass: HomeAssistant, client: FakeClient, tmp_path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = await setup(hass, client, {"save_detection_pictures": True})
    saved = []
    hass.bus.async_listen("ezviz_instant_bell_picture_saved", lambda event: saved.append(event.data))
    rings = []
    hass.bus.async_listen("state_changed", lambda e: rings.append(e) if e.data["entity_id"].startswith("event.") else None)
    client.messages = [
        motion_message("m-motion", 3),
        motion_message("m-name", 2, deviceSerial=None, **{"from": "Tor"}, title="Human detected"),
        motion_message("m-old", 600),  # older than two minutes: from before a restart
        motion_message("m-other", 1, **{"from": "OTHER1"}),  # another camera of the account
        motion_message("m-nopic", 1, pic=None),
    ]
    await poll_once(hass)
    files = sorted((tmp_path / "media" / "ezviz_instant_bell" / "tor").rglob("*.jpg"))
    assert [f.name.split("_", 1)[1] for f in files] == ["motion_detection_alarm.jpg", "human_detected.jpg"]
    assert all(f.read_bytes() == b"JPEGDATA" for f in files)
    assert sorted(e["message_id"] for e in saved) == ["m-motion", "m-name"]
    info = next(e for e in saved if e["message_id"] == "m-motion")
    assert info["serial"] == SERIAL and info["entry_id"] == entry.entry_id
    assert info["media_content_id"].startswith("media-source://media_source/local/ezviz_instant_bell/tor/")
    assert rings == []  # motion is not a ring


async def test_rings_and_openings_are_not_saved_as_motion(hass: HomeAssistant, client: FakeClient, tmp_path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    await setup(hass, client, {"save_detection_pictures": True})
    client.messages = [ring_message("m-ring", 2), motion_message("m-unlock", 1, subType=0, title="EZVIZ app unlock the lock")]
    await poll_once(hass)
    assert not (tmp_path / "media").exists()


async def test_no_motion_picture_is_saved_unless_the_option_is_on(hass: HomeAssistant, client: FakeClient, tmp_path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    await setup(hass, client)
    client.messages = [motion_message("m-motion", 2)]
    await poll_once(hass)
    assert not (tmp_path / "media").exists()


# --- motion and person as event types --------------------------------------------------


async def test_motion_and_person_fire_the_event_entity_when_switched_on(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client, {"motion_events": True, "person_events": True})
    bell = entity_id(hass, "event", "bell")
    image = hass.data["image"].get_entity(entity_id(hass, "image", "last_picture"))
    seen = []
    hass.bus.async_listen("state_changed", lambda e: seen.append(e.data["new_state"]) if e.data["entity_id"] == bell else None)
    client.messages = [  # newest first, as the cloud sends them
        motion_message("m-person", 1, deviceSerial=None, **{"from": "Tor"}, title="Human detected"),
        motion_message("m-unknown", 2, title="Bewegung erkannt"),  # not recognised: no event
        motion_message("m-motion", 3),
    ]
    await poll_once(hass)
    # Sorted: older Home Assistant runs a plain listener in the executor, in no fixed order.
    assert sorted((s.attributes["event_type"], s.attributes["message"]) for s in seen) == [
        ("motion", "Motion Detection Alarm"),
        ("person", "Human detected"),
    ]
    assert all(s.attributes["source"] == "poll" for s in seen)
    assert await image.async_image() is None  # the ring picture is not replaced by motion


async def test_motion_is_no_event_unless_switched_on(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client, {"person_events": True})
    bell = entity_id(hass, "event", "bell")
    client.messages = [motion_message("m-motion", 2)]
    await poll_once(hass)
    assert hass.states.get(bell).attributes.get("event_type") is None


def push_alert(client: FakeClient, title: str, code: int = 10001, msg_id: int = 7) -> None:
    client.mqtt.callback(
        {
            "id": msg_id,
            "alert": title,
            "ext": {"device_serial": SERIAL, "alert_type_code": code, "time": dt_util.now().strftime("%Y-%m-%d %H:%M:%S")},
        }
    )


async def test_a_pushed_motion_is_motion_only_when_switched_on(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    bell = entity_id(hass, "event", "bell")
    push_alert(client, "Motion Detection Alarm")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(bell).attributes["event_type"] == "other"  # as before

    hass.config_entries.async_update_entry(client_entry := hass.config_entries.async_entries(DOMAIN)[0], options={"motion_events": True})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert client_entry.options["motion_events"]
    bell = entity_id(hass, "event", "bell")
    push_alert(client, "Motion Detection Alarm", msg_id=8)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(bell).attributes["event_type"] == "motion"


async def test_diagnostics_show_the_codes_of_recent_messages(hass: HomeAssistant, client: FakeClient) -> None:
    from custom_components.ezviz_instant_bell.diagnostics import async_get_config_entry_diagnostics

    client.messages = [
        motion_message("m-card", 2, title="RFID card unlock Anna"),
        motion_message("m-motion", 3, ext={"alarmType": 2402}),
    ]
    entry = await setup(hass, client, {"motion_events": True})
    await poll_once(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["detection_events"] == ["motion"]
    assert diag["recent_messages"] == [
        {"subtype": 2401, "alarm_type": 2402, "kind": "motion", "title": "Motion Detection Alarm"},
        {"subtype": 2401, "alarm_type": None, "kind": None, "title": None},  # an unlock's title can carry a name
    ]


# --- reconfigure ----------------------------------------------------------------------


async def test_reconfigure_changes_region_and_keeps_the_password(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    old_password = entry.data["password"]
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        assert result["type"] == "form" and result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"region": "apiius.ezvizlife.com"})
        assert result["step_id"] == "devices"
        key = next(k for k in result["data_schema"].schema if k == "devices")
        assert key.default() == [SERIAL]  # what is set up now is preselected
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"devices": [SERIAL]})
        await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert entry.data["region"] == "apiius.ezvizlife.com" and entry.data["password"] == old_password
    assert entry.data["devices"] == [SERIAL] and entry.data["username"] == "user@example.com"


async def test_reconfigure_with_a_wrong_password_stays_on_the_form(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    with patch("custom_components.ezviz_instant_bell.ezviz.login", side_effect=ezviz.InvalidAuth("Incorrect Password.")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "bad", "region": "apiieu.ezvizlife.com"}
        )
    assert result["step_id"] == "reconfigure" and result["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] != "bad"


# --- who opened the door ----------------------------------------------------------------


async def test_opening_from_home_assistant_is_announced_with_the_user(hass: HomeAssistant, client: FakeClient) -> None:
    from homeassistant.core import Context

    await setup(hass, client)
    first = await hass.auth.async_create_user("First")
    second = await hass.auth.async_create_user("Second")
    seen = []
    hass.bus.async_listen("ezviz_instant_bell_door_opened", lambda event: seen.append(event))
    await hass.services.async_call(
        "button", "press", {"entity_id": legacy_entity(hass, "button", "unlock_door")},
        blocking=True, context=Context(user_id=first.id),
    )
    await hass.services.async_call(DOMAIN, "unlock_gate", {}, blocking=True, context=Context(user_id=second.id))
    await hass.async_block_till_done()
    assert [(e.data["lock"], e.data["user_id"]) for e in seen] == [("door", first.id), ("gate", second.id)]
    assert seen[0].context.user_id == first.id


async def test_a_failed_opening_is_not_announced(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    seen = []
    hass.bus.async_listen("ezviz_instant_bell_door_opened", lambda event: seen.append(event))
    client.fail_unlock = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "unlock_door", {}, blocking=True)
    await hass.async_block_till_done()
    assert seen == []


async def test_logbook_describes_the_opening(hass: HomeAssistant, client: FakeClient) -> None:
    from homeassistant.components.logbook import async_log_entry  # noqa: F401  (the logbook is installed)
    from homeassistant.core import Context, Event

    from custom_components.ezviz_instant_bell.logbook import async_describe_events

    described = {}
    async_describe_events(hass, lambda domain, name, fn: described.update({(domain, name): fn}))
    describe = described[(DOMAIN, "ezviz_instant_bell_door_opened")]
    with_user = describe(Event("x", {"lock": "gate"}, context=Context(user_id="u")))
    assert with_user["message"] == "opened the gate from Home Assistant"
    assert "automation" in describe(Event("x", {"lock": "door"}))["message"]


async def test_two_rings_of_one_visit_announce_the_recording_once(hass: HomeAssistant, client: FakeClient, tmp_path) -> None:
    day = dt_util.now().date()
    ring_time = dt_util.now() - timedelta(seconds=30)
    if ring_time.date() != day:
        return
    client.records = [record(day, ring_time.strftime("%H:%M:%S"), 120)]
    entry = await setup(hass, client, {"save_clips": True})
    hub = entry.runtime_data
    saved, rings = [], []
    hass.bus.async_listen("ezviz_instant_bell_clip_saved", lambda event: saved.append(event.data))
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    async_dispatcher_connect(hass, hub.signal_event, lambda event: rings.append(event))
    with patch("custom_components.ezviz_instant_bell.hub.CLIP_SAVE_FIRST_WAIT", 0), patch(
        "custom_components.ezviz_instant_bell.hub.CLIP_SAVE_POLL", 0.01
    ), patch.object(hub.clips, "_ffmpeg", return_value=fake_ffmpeg(tmp_path)), patch(
        "custom_components.ezviz_instant_bell.clips.open_playback", return_value=_Playback()
    ), patch(
        "custom_components.ezviz_instant_bell.clips.iter_media", side_effect=lambda *a, **k: iter([b"video"])
    ):
        client.messages = [ring_message("ring-1", 30)]
        await poll_once(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        client.messages = [ring_message("ring-2", 5), ring_message("ring-1", 30)]
        await poll_once(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert len(rings) == 2  # both rings reached the hub (25 s apart, outside the duplicate window)
    assert len(saved) == 1


# --- working without the EZVIZ cloud -------------------------------------------------------

LOCAL_STORE = f"{DOMAIN}.local.offline-entry"


def offline_entry(options: dict | None = None) -> MockConfigEntry:
    entry = make_entry(options)
    return MockConfigEntry(
        domain=DOMAIN, entry_id="offline-entry", unique_id=entry.unique_id, data=dict(entry.data),
        options=dict(entry.options),
    )


async def setup_without_cloud(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = offline_entry(options)
    entry.add_to_hass(hass)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", side_effect=ezviz.CannotConnect("no internet")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def stored_local_data() -> dict:
    return {
        "version": 1,
        "minor_version": 1,
        "key": LOCAL_STORE,
        "data": {
            "doors": {"BE0000001": {"lan_key": "0123456789abcdef", "ip": "192.168.1.50"}},
            "models": {SERIAL: "CPD5"},
            "monitors": {SERIAL: [MONITOR]},
        },
    }


async def test_without_cloud_and_nothing_stored_setup_is_retried(hass: HomeAssistant) -> None:
    entry = await setup_without_cloud(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_what_the_local_video_needs_is_stored(hass: HomeAssistant, client: FakeClient, hass_storage) -> None:
    entry = await setup(hass, client)
    support = entry.runtime_data.stream

    class FakeCas:
        def __init__(self, token):
            pass

        def cas_get_encryption(self, serial):
            return {"Response": {"Session": {"@Key": "0123456789abcdef"}}}

    class Endpoint:
        host = "192.168.1.50"

    with patch("pyezvizapi.cas.EzvizCAS", FakeCas), patch("requests.post"), patch(
        "pyezvizapi.local_stream._local_sdk_endpoint_from_client", return_value=Endpoint()
    ):
        await hass.async_add_executor_job(support.fetch_lan_aes_key, SERIAL)
        await hass.async_add_executor_job(support.get_local_ip, SERIAL)
    await hass.async_block_till_done()
    from pytest_homeassistant_custom_component.common import flush_store

    await flush_store(entry.runtime_data._local_store)  # the store writes a few seconds later; do it now
    stored = hass_storage[f"{DOMAIN}.local.{entry.entry_id}"]["data"]
    assert stored["doors"] == {"BE0000001": {"lan_key": "0123456789abcdef", "ip": "192.168.1.50"}}
    assert stored["models"] == {SERIAL: "CPD5"} and stored["monitors"] == {SERIAL: [MONITOR]}


async def test_starts_without_cloud_with_what_is_stored(hass: HomeAssistant, hass_storage) -> None:
    hass_storage[LOCAL_STORE] = stored_local_data()
    entry = await setup_without_cloud(hass)
    assert entry.state is ConfigEntryState.LOADED
    hub = entry.runtime_data
    assert hub.cloud_online is False and hub.client is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "cloud_offline_offline-entry") is not None
    # the local video has its key and address without asking the cloud
    assert await hass.async_add_executor_job(hub.stream.fetch_lan_aes_key, SERIAL) == b"0123456789abcdef"
    assert await hass.async_add_executor_job(hub.stream.get_local_ip, SERIAL) == "192.168.1.50"
    # the same entities as with the cloud: live camera, monitor controls, device named after the stored model
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("camera", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_live") is not None
    assert reg.async_get_entity_id("switch", DOMAIN, f"{LEGACY_DOMAIN}_{MONITOR}_chime_sound_monitor") is not None
    assert device_of(dr.async_get(hass), {(DOMAIN, SERIAL)}).model == "CPD5"
    # status entities wait for the cloud
    assert hass.states.get(legacy_entity(hass, "switch", "privacy")).state == "unavailable"


async def test_everything_starts_again_when_the_cloud_answers(hass: HomeAssistant, client: FakeClient, hass_storage) -> None:
    hass_storage[LOCAL_STORE] = stored_local_data()
    entry = await setup_without_cloud(hass)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        await tick(hass, 301)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.cloud_online is True
        assert hass.states.get(legacy_entity(hass, "switch", "privacy")).state == "off"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "cloud_offline_offline-entry") is None


async def test_a_refused_password_while_offline_asks_for_a_new_login(hass: HomeAssistant, hass_storage) -> None:
    hass_storage[LOCAL_STORE] = stored_local_data()
    entry = await setup_without_cloud(hass)
    with patch(
        "custom_components.ezviz_instant_bell.ezviz.login", side_effect=ezviz.InvalidAuth("Incorrect Password.")
    ) as login:
        await tick(hass, 301)
        await hass.async_block_till_done()
        assert login.call_count == 1
        assert any(
            flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        )
        # the retry timer is cancelled, so the wrong password is not tried again
        # (EZVIZ locks an account after repeated failed logins)
        await tick(hass, 301)
        await hass.async_block_till_done()
        assert login.call_count == 1
    assert entry.runtime_data.cloud_online is False


async def test_opening_while_offline_says_why_it_cannot(hass: HomeAssistant, hass_storage) -> None:
    hass_storage[LOCAL_STORE] = stored_local_data()
    await setup_without_cloud(hass)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "unlock_door", {}, blocking=True)
    assert err.value.translation_key == "cloud_unreachable"
