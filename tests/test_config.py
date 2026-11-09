from pathlib import Path

import pytest

from fedreg_digest.config import ConfigError, load_profile

ROOT = Path(__file__).parent.parent


@pytest.mark.parametrize("path", sorted((ROOT / "profiles").glob("*.yaml")))
def test_example_profiles_load(path):
    profile = load_profile(path)
    assert profile.name and profile.topic and profile.keywords
    assert profile.watches_cfr


def write(tmp_path, text):
    path = tmp_path / "p.yaml"
    path.write_text(text)
    return path


MINIMAL = """
name: Test
topic: Testing
keywords: [x]
federal_register:
  agencies: [food-and-drug-administration]
"""


def test_minimal_profile_gets_defaults(tmp_path):
    profile = load_profile(write(tmp_path, MINIMAL))
    assert profile.federal_register.document_types == ["RULE", "PRORULE", "NOTICE"]
    assert profile.ai.model == "claude-haiku-5-5"
    assert not profile.watches_cfr


@pytest.mark.parametrize(
    "extra, message",
    [
        ("typo_key: 1\n", "unknown key"),
        ("scoring:\n  hgih: 3\n", "Unknown key"),
        ("ecfr:\n  parts: ['101']\n", "ecfr.title"),
        ("scoring:\n  high: 5\n  medium: 10\n", "thresholds"),
        ("ai:\n  effort: turbo\n", "effort"),
    ],
)
def test_invalid_profiles_explain_themselves(tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        load_profile(write(tmp_path, MINIMAL + extra))


def test_profile_needs_agency_or_search(tmp_path):
    with pytest.raises(ConfigError, match="agency"):
        load_profile(write(tmp_path, "name: T\ntopic: T\nkeywords: [x]\n"))


def test_bad_document_type(tmp_path):
    text = MINIMAL.replace(
        "agencies: [food-and-drug-administration]", "agencies: [x]\n  document_types: [RULES]"
    )
    with pytest.raises(ConfigError, match="document_types"):
        load_profile(write(tmp_path, text))
