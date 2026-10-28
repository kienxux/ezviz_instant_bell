"""Integration tests with a fake EZVIZ client (no network)."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ezviz_instant_bell import ezviz
from custom_components.ezviz_instant_bell.const import DOMAIN, EVENT_UNLOCK, EVENT_UNLOCK_LEGACY, LEGACY_DOMAIN


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _local_sockets(socket_enabled):
    """The stream relay listens on a port of this computer."""
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
    def get_device_messages_list(self, **kwargs):
        return {"message": list(self.messages)}

    def download_alarm_image(self, url, serial=None):
        return b"JPEGDATA"

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
                          "alarm_unlock_rfid", "alarm_unlock_face", "alarm_unlock_palm", "alarm_unlock_code",
                          "alarm_unlock_app", "alarm_smart_detection_alarm", "alarm_intelligent_detection_alarm",
                          "binary_mute_on", "binary_loitering_on", "binary_stranger_detection_on"],
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


async def test_sensor_values_and_names(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    assert hass.states.get(legacy_entity(hass, "sensor", "sensor_status")).state == "online"
    assert hass.states.get(legacy_entity(hass, "sensor", "sensor_name")).state == "Tor"
    assert hass.states.get(legacy_entity(hass, "sensor", "sensor_mic_volume")).state == "90"
    assert hass.states.get(legacy_entity(hass, "switch", "defence")).state == "on"
    assert hass.states.get(legacy_entity(hass, "switch", "privacy")).state == "off"
    # device named the way the earlier integration named it
    reg = dr.async_get(hass)
    device = reg.async_get_device(identifiers={(DOMAIN, SERIAL)})
    assert device.name == "EZVIZ CPD5 (BE0000001-BE0000002)"
    assert reg.async_get_device(identifiers={(DOMAIN, MONITOR)}).name == "EZVIZ CPD5 Monitor (BE0000001)"


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
    assert [d for d in seen if d["category"] == "unlock_lock"][0]["keys"][0]["keyName"] == "Alice"


async def test_push_for_other_device_is_ignored(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    push_unlock(client, serial="ZZ9999999-ZZ1", msg_id=2)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "event", "bell")).attributes.get("event_type") is None


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


# --- taking over the entities of the earlier ezviz_hp7 integration -----------


def add_legacy_setup(hass: HomeAssistant, *, state: ConfigEntryState = ConfigEntryState.NOT_LOADED, disabled=False):
    """A registry as the ezviz_hp7 integration left it, with its own entity IDs."""
    legacy = MockConfigEntry(domain=LEGACY_DOMAIN, data={"serial": SERIAL, "username": "user@example.com"})
    legacy.add_to_hass(hass)
    if state != ConfigEntryState.NOT_LOADED:
        legacy.mock_state(hass, state)
    devices = dr.async_get(hass)
    door = devices.async_get_or_create(
        config_entry_id=legacy.entry_id, identifiers={(LEGACY_DOMAIN, SERIAL)}, name="EZVIZ HP7 (BE0000001-BE0000002)"
    )
    devices.async_update_device(door.id, area_id=None, name_by_user="Front door")
    mon = devices.async_get_or_create(
        config_entry_id=legacy.entry_id, identifiers={(LEGACY_DOMAIN, MONITOR)}, name="EZVIZ HP7 Monitor (BE0000001)"
    )
    reg = er.async_get(hass)
    made = {}
    for domain, owner, suffix, dev, eid in (
        ("button", SERIAL, "unlock_door", door, "button.front_door_unlock_door"),
        ("switch", SERIAL, "dnd", door, "switch.front_door_dnd"),
        ("number", MONITOR, "chime_volume_monitor", mon, "number.hall_monitor_volume"),
        ("camera", SERIAL, "live", door, "camera.front_door_live"),
    ):
        entry = reg.async_get_or_create(
            domain,
            LEGACY_DOMAIN,
            f"{LEGACY_DOMAIN}_{owner}_{suffix}",
            config_entry=legacy,
            device_id=dev.id,
            suggested_object_id=eid.split(".", 1)[1],
            disabled_by=er.RegistryEntryDisabler.CONFIG_ENTRY if disabled else None,
        )
        made[suffix] = entry.entity_id
        assert entry.entity_id == eid
    return legacy, made, door


async def test_old_entities_keep_their_entity_ids(hass: HomeAssistant, client: FakeClient) -> None:
    legacy, made, door = add_legacy_setup(hass)
    entry = await setup(hass, client)
    reg = er.async_get(hass)

    moved = reg.async_get(made["unlock_door"])
    assert moved.platform == DOMAIN and moved.config_entry_id == entry.entry_id
    assert moved.entity_id == "button.front_door_unlock_door"
    assert reg.async_get(made["dnd"]).platform == DOMAIN
    assert reg.async_get(made["chime_volume_monitor"]).platform == DOMAIN
    # no second copy was created under a different entity ID
    assert not [e for e in reg.entities if e.endswith("_2")]
    # the live video moves too, and keeps its entity ID
    live = reg.async_get(made["live"])
    assert live.platform == DOMAIN and live.entity_id == "camera.front_door_live"
    assert hass.states.get("camera.front_door_live") is not None
    # the device keeps its ID and the name the user gave it
    devices = dr.async_get(hass)
    device = devices.async_get_device(identifiers={(DOMAIN, SERIAL)})
    assert device.id == door.id and device.name_by_user == "Front door"
    assert entry.entry_id in device.config_entries
    # and the moved button works
    await hass.services.async_call("button", "press", {"entity_id": "button.front_door_unlock_door"}, blocking=True)
    assert client.unlocks == [(SERIAL, "internal-user", 2)]


async def test_old_entities_of_a_switched_off_integration_are_switched_on(hass: HomeAssistant, client: FakeClient) -> None:
    _, made, _ = add_legacy_setup(hass, disabled=True)
    await setup(hass, client)
    assert er.async_get(hass).async_get(made["dnd"]).disabled_by is None
    assert hass.states.get(made["dnd"]).state == "off"


async def test_nothing_is_moved_while_the_old_integration_runs(hass: HomeAssistant, client: FakeClient) -> None:
    _, made, _ = add_legacy_setup(hass, state=ConfigEntryState.LOADED)
    await setup(hass, client)
    assert er.async_get(hass).async_get(made["dnd"]).platform == LEGACY_DOMAIN
    assert ir.async_get(hass).async_get_issue(DOMAIN, "legacy_running") is not None


async def test_copies_made_while_both_ran_are_replaced(hass: HomeAssistant, client: FakeClient) -> None:
    legacy, made, _ = add_legacy_setup(hass, state=ConfigEntryState.LOADED)
    entry = await setup(hass, client)  # both run: the new one gets its own entity IDs
    reg = er.async_get(hass)
    copy_id = reg.async_get_entity_id("switch", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_dnd")
    assert copy_id and copy_id != made["dnd"]
    legacy.mock_state(hass, ConfigEntryState.NOT_LOADED)  # the old one is switched off
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert reg.async_get_entity_id("switch", DOMAIN, f"{LEGACY_DOMAIN}_{SERIAL}_dnd") == made["dnd"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, "legacy_running") is None


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


async def test_config_flow_offers_the_old_setup(hass: HomeAssistant, client: FakeClient) -> None:
    MockConfigEntry(
        domain=LEGACY_DOMAIN,
        data={"username": "old@example.com", "password": "oldpw", "region": "eu", "serial": SERIAL},
        options={"monitor_serial": "BE0000001"},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "menu" and set(result["menu_options"]) == {"legacy", "manual"}
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client) as login, patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "legacy"})
        assert result["step_id"] == "devices"
        assert login.call_args.args[1:4] == ("old@example.com", "oldpw", "apiieu.ezvizlife.com")
        key = next(k for k in result["data_schema"].schema if k == "devices")
        assert key.default() == [SERIAL]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"devices": [SERIAL]})
    assert result["type"] == "create_entry"
    assert result["data"]["username"] == "old@example.com"
    assert result["options"] == {"monitor_serials": "BE0000001"}


async def test_config_flow_old_setup_can_be_skipped(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=LEGACY_DOMAIN, data={"username": "a", "password": "b", "serial": SERIAL}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    assert result["type"] == "form" and result["step_id"] == "user"


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
    result = await flow.async_step_confirm({"code": "1234"})
    assert result["type"] == "create_entry"
    assert entry.options["encryption_key"] == "KEY123"
    # a wrong code is refused
    client.cam_key_answers = [RuntimeError("bad"), RuntimeError("bad")]
    result = await flow.async_step_confirm({"code": "0000"})
    assert result["type"] == "form" and result["errors"] == {"code": "invalid_code"}


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


async def test_old_stream_settings_are_taken_over(hass: HomeAssistant, client: FakeClient) -> None:
    MockConfigEntry(
        domain=LEGACY_DOMAIN,
        data={"username": "old@example.com", "password": "oldpw", "region": "eu", "serial": SERIAL},
        options={"monitor_serial": "BE0000001", "stream_source": "auto", "relay_port": 8554, "encryption_key": "K1"},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client), patch(
        "custom_components.ezviz_instant_bell.ezviz.list_devices", return_value=DEVICES
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "legacy"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"devices": [SERIAL]})
    assert result["options"] == {
        "stream_source": "auto",
        "relay_port": 8554,
        "encryption_key": "K1",
        "monitor_serials": "BE0000001",
    }
