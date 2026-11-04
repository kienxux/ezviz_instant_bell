"""Every translation has the same keys and placeholders as strings.json (no Home Assistant needed)."""

import json
import re
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_instant_bell"


def flat(tree, path=""):
    for key, value in tree.items():
        if isinstance(value, dict):
            yield from flat(value, f"{path}{key}.")
        else:
            yield f"{path}{key}", value


def load(name):
    return dict(flat(json.loads((DIR / name).read_text(encoding="utf-8"))))


def test_english_translation_is_the_strings_file():
    assert load("translations/en.json") == load("strings.json")


@pytest.mark.parametrize("language", sorted(p.stem for p in (DIR / "translations").glob("*.json") if p.stem != "en"))
def test_other_languages_have_every_key_and_placeholder(language):
    english, other = load("strings.json"), load(f"translations/{language}.json")
    assert set(other) == set(english)
    for key, text in english.items():
        assert sorted(re.findall(r"\{\w+\}", text)) == sorted(re.findall(r"\{\w+\}", other[key])), key
