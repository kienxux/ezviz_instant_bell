"""One key per door station (no Home Assistant needed)."""

from keys import CONF_KEY, CONF_KEYS, key_for, with_keys


def test_each_door_station_gets_its_own_key():
    options = with_keys({}, {"AAA-CAM1": "k1"}, door_count=2)
    options = with_keys(options, {"BBB-CAM2": "k2"}, door_count=2)
    assert key_for(options, "AAA-CAM1") == "k1" and key_for(options, "BBB-CAM2") == "k2"
    assert CONF_KEY not in options  # no single key is written while there are two stations


def test_single_station_still_writes_the_old_option_and_the_old_key_is_the_fallback():
    options = with_keys({"poll_interval": 5}, {"AAA-CAM1": "k1"}, door_count=1)
    assert options[CONF_KEY] == "k1" and options["poll_interval"] == 5
    assert key_for({CONF_KEY: " old "}, "ZZZ-CAM") == "old"
    assert key_for({}, "ZZZ-CAM") == ""


def test_same_device_by_bare_serial_and_case():
    options = with_keys({}, {"aaa-cam1": "k1"}, door_count=2)
    assert key_for(options, "AAA") == "k1" and options[CONF_KEYS] == {"AAA": "k1"}
