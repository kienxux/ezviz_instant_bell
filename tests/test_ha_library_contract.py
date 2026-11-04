"""The parts of pyezvizapi that this integration reaches into: a library update that removes one fails here."""

import importlib

import pytest

from custom_components.ezviz_instant_bell.compat import tolerate_unknown_enum_values


def test_private_client_methods_still_exist():
    from pyezvizapi.client import EzvizClient

    for name in ("_request_json", "_ensure_ok", "get_cam_key", "set_dev_config_kv", "export_token",
                 "remote_unlock", "search_records", "get_terminals", "get_device_messages_list",
                 "download_alarm_image", "get_mqtt_client", "get_dev_config", "switch_status",
                 "set_camera_defence", "do_not_disturb", "set_iot_feature", "set_video_enc"):
        assert hasattr(EzvizClient, name), name
    assert "_token" in EzvizClient.__init__.__code__.co_names or hasattr(EzvizClient("a", "b"), "_token")


@pytest.mark.parametrize(
    "module, name",
    [
        ("pyezvizapi.api_endpoints", "API_ENDPOINT_DEVCONFIG_BY_KEY"),
        ("pyezvizapi.cloud_stream", "get_cloud_stream_info"),
        ("pyezvizapi.local_stream", "_local_sdk_endpoint_from_client"),
        ("pyezvizapi.cas", "EzvizCAS"),
        ("pyezvizapi.stream", "VtmStreamClient"),
        ("pyezvizapi.stream", "build_stream_info_request"),
        ("pyezvizapi.stream", "parse_stream_info_response"),
        ("pyezvizapi.stream", "VtmMessageCode"),
        ("pyezvizapi.camera", "EzvizCamera"),
    ],
)
def test_library_names_still_exist(module, name):
    assert hasattr(importlib.import_module(module), name)


def test_unknown_enum_numbers_become_unknown_and_known_ones_stay():
    from pyezvizapi.constants import SoundMode

    tolerate_unknown_enum_values()
    assert SoundMode(5) is SoundMode.UNKNOWN
    assert SoundMode(2) is SoundMode.SILENT
    assert tolerate_unknown_enum_values() == []  # a second call changes nothing
