"""Integration tests with a fake EZVIZ client (no network)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ezviz_instant_bell import ezviz
from custom_components.ezviz_instant_bell.const import DOMAIN

@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    yield


SERIAL = "BE0000001-BE0000002"
DEVICES = {
    "BE0000001": {"name": "Tor", "category": "DBScreen"},
    SERIAL: {"name": "Tor", "category": "BDoorBell"},
}


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
        self.mqtt = None

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


BUTTONS_ON = {"create_buttons": True}


async def setup(hass: HomeAssistant, client: FakeClient, options: dict | None = None) -> MockConfigEntry:
    entry = make_entry(options)
    entry.add_to_hass(hass)
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")
    assert eid, f"no {platform} entity for {key}"
    return eid


async def poll_once(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_entities_are_created(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    for platform, key in (("event", "bell"), ("image", "last_picture")):
        entity_id(hass, platform, key)
    assert client.mqtt.connected


async def test_buttons_are_off_by_default(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_open_door") is None
    assert reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_open_gate") is None


async def test_turning_buttons_off_removes_them(hass: HomeAssistant, client: FakeClient) -> None:
    entry = await setup(hass, client, BUTTONS_ON)
    entity_id(hass, "button", "open_door")
    with patch("custom_components.ezviz_instant_bell.ezviz.login", return_value=client):
        hass.config_entries.async_update_entry(entry, options={"create_buttons": False})
        await hass.async_block_till_done()
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_open_door") is None


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


async def test_same_ring_is_announced_once(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    bell = entity_id(hass, "event", "bell")
    client.messages = [ring_message("a")]
    await poll_once(hass)
    first = hass.states.get(bell).state
    await poll_once(hass)
    assert hass.states.get(bell).state == first


async def test_push_unlock_event(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    now = dt_util.now()
    client.mqtt.callback(
        {
            "id": 1,
            "alert": "EZVIZ app unlock the lock",
            "ext": {
                "device_serial": SERIAL,
                "alert_type_code": 10242,
                "time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "image": "https://example.invalid/p",
            },
        }
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(entity_id(hass, "event", "bell"))
    assert state.attributes["event_type"] == "door_unlock"
    assert state.attributes["source"] == "push"


async def test_push_for_other_device_is_ignored(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client)
    client.mqtt.callback(
        {"id": 2, "alert": "x", "ext": {"device_serial": "ZZ9999999-ZZ1", "alert_type_code": 10242,
                                        "time": dt_util.now().strftime("%Y-%m-%d %H:%M:%S")}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "event", "bell")).attributes.get("event_type") is None


async def test_buttons_open_the_right_lock_only(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client, BUTTONS_ON)
    await hass.services.async_call("button", "press", {"entity_id": entity_id(hass, "button", "open_door")}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": entity_id(hass, "button", "open_gate")}, blocking=True)
    assert client.unlocks == [(SERIAL, "internal-user", 2), (SERIAL, "internal-user", 1)]


async def test_double_press_is_ignored(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client, BUTTONS_ON)
    door = entity_id(hass, "button", "open_door")
    for _ in range(2):
        await hass.services.async_call("button", "press", {"entity_id": door}, blocking=True)
    assert len(client.unlocks) == 1


async def test_failed_unlock_raises_and_never_tries_the_other_lock(hass: HomeAssistant, client: FakeClient) -> None:
    await setup(hass, client, BUTTONS_ON)
    client.fail_unlock = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id(hass, "button", "open_door")}, blocking=True
        )
    assert client.unlocks == []


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
