import os
import types

import feature_code as fc


def test_generates_stores_and_reuses(tmp_path):
    first = fc.resolve_feature_code(tmp_path, environ={})
    assert fc.valid_code(first) == first
    assert (tmp_path / fc.FEATURE_CODE_FILENAME).read_text() == first
    assert fc.resolve_feature_code(tmp_path, environ={}) == first


def test_two_installs_get_different_codes(tmp_path):
    a = fc.resolve_feature_code(tmp_path / "a", environ={})
    b = fc.resolve_feature_code(tmp_path / "b", environ={})
    assert a != b


def test_environment_variable_wins(tmp_path):
    forced = "c" * 32
    assert fc.resolve_feature_code(tmp_path, environ={fc.ENV_VAR: forced.upper()}) == forced
    assert not (tmp_path / fc.FEATURE_CODE_FILENAME).exists()


def test_invalid_environment_value_is_ignored(tmp_path):
    code = fc.resolve_feature_code(tmp_path, environ={fc.ENV_VAR: "not-a-code"})
    assert fc.valid_code(code) == code


def test_corrupt_file_is_replaced(tmp_path):
    (tmp_path / fc.FEATURE_CODE_FILENAME).write_text("garbage")
    code = fc.resolve_feature_code(tmp_path, environ={})
    assert fc.valid_code(code) == code
    assert (tmp_path / fc.FEATURE_CODE_FILENAME).read_text() == code


def test_unwritable_folder_still_returns_a_code(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")  # a file where a folder is needed
    code = fc.resolve_feature_code(blocker / "sub", environ={})
    assert fc.valid_code(code) == code


def test_apply_to_library_patches_modules_and_header():
    header = {"featureCode": "old", "other": 1}
    mods = {
        "pyezvizapi.constants": types.SimpleNamespace(FEATURE_CODE="old", REQUEST_HEADER=header),
        "pyezvizapi.client": types.SimpleNamespace(FEATURE_CODE="old"),
        "pyezvizapi.mqtt": types.SimpleNamespace(),  # no FEATURE_CODE: untouched
    }
    changed = fc.apply_to_library("d" * 32, mods)
    assert changed == ["pyezvizapi.constants", "pyezvizapi.client"]
    assert mods["pyezvizapi.client"].FEATURE_CODE == "d" * 32
    assert header["featureCode"] == "d" * 32 and header["other"] == 1
