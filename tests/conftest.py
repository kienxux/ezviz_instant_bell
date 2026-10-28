"""Test setup.

``parsing`` and ``feature_code`` have no Home Assistant imports, so they are put
on the path directly and tested without it. The integration tests use the
fixtures of pytest-homeassistant-custom-component and are skipped if it is not
installed.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "ezviz_instant_bell"))
sys.path.insert(0, str(ROOT))

try:
    import pytest_homeassistant_custom_component  # noqa: F401

    pytest_plugins = ["pytest_homeassistant_custom_component"]
    HAVE_HA = True
except ImportError:
    HAVE_HA = False


def pytest_ignore_collect(collection_path, config):
    if not HAVE_HA and collection_path.name.startswith("test_ha_"):
        return True
    return None
