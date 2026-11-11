"""The visitor log card in the dashboard resources."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ezviz_instant_bell.api import CARD_URL, _add_resource, async_remove_card

URL = f"{CARD_URL}?v=1.0"


@pytest.fixture
async def resources(hass: HomeAssistant):
    assert await async_setup_component(hass, "lovelace", {})
    return hass.data["lovelace"].resources


def urls(resources) -> list[str]:
    return [item["url"] for item in resources.async_items()]


async def test_card_is_added_once(hass: HomeAssistant, resources) -> None:
    assert await _add_resource(hass, URL)
    assert await _add_resource(hass, URL)
    assert urls(resources) == [URL]
    assert resources.async_items()[0]["type"] == "module"


async def test_new_version_replaces_the_old_one(hass: HomeAssistant, resources) -> None:
    await resources.async_create_item({"res_type": "module", "url": f"{CARD_URL}?v=0.9"})
    await resources.async_create_item({"res_type": "module", "url": CARD_URL})
    await resources.async_create_item({"res_type": "module", "url": "/local/other-card.js"})
    assert await _add_resource(hass, URL)
    assert sorted(urls(resources)) == sorted([URL, "/local/other-card.js"])


async def test_removal_leaves_other_resources(hass: HomeAssistant, resources) -> None:
    await resources.async_create_item({"res_type": "module", "url": "/local/other-card.js"})
    await _add_resource(hass, URL)
    await async_remove_card(hass)
    assert urls(resources) == ["/local/other-card.js"]


async def test_yaml_resources_are_left_alone(hass: HomeAssistant, resources) -> None:
    # The YAML option is named differently across Home Assistant versions; set the mode directly.
    hass.data["lovelace"].resource_mode = "yaml"
    assert not await _add_resource(hass, URL)
    await async_remove_card(hass)
    assert urls(resources) == []


async def test_without_lovelace_falls_back(hass: HomeAssistant) -> None:
    assert not await _add_resource(hass, URL)
