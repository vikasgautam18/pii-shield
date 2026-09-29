"""Tests for pii_shield.policy.AnonymizationPolicy."""

import pytest

from pii_shield.models import EntityConfig
from pii_shield.policy import AnonymizationPolicy


def test_defaults_are_empty():
    p = AnonymizationPolicy()
    assert p.strategies == {}
    assert p.allow_list == []
    assert p.entity_type_allow_list == set()
    assert p.entity_keyword_allow_list == {}
    assert p.score_threshold is None
    assert p.language == "en"


def test_from_dict_roundtrip():
    data = {
        "strategies": {"IN_AADHAAR": "hash", "PERSON": "replace"},
        "allow_list": ["Contoso"],
        "entity_type_allow_list": ["EMAIL_ADDRESS"],
        "entity_keyword_allow_list": {"LOCATION": ["India"]},
        "score_threshold": 0.6,
        "language": "en",
    }
    p = AnonymizationPolicy.from_dict(data)
    assert p.strategies["IN_AADHAAR"] == "hash"
    assert p.allow_list == ["Contoso"]
    assert p.entity_type_allow_list == {"EMAIL_ADDRESS"}
    assert p.entity_keyword_allow_list == {"LOCATION": ["India"]}
    assert p.score_threshold == 0.6

    out = p.to_dict()
    assert out["strategies"] == data["strategies"]
    assert out["entity_type_allow_list"] == ["EMAIL_ADDRESS"]
    assert out["entity_keyword_allow_list"] == {"LOCATION": ["India"]}


def test_include_list_roundtrip():
    p = AnonymizationPolicy.from_dict({"entity_type_include_list": ["PERSON", "EMAIL_ADDRESS"]})
    assert p.entity_type_include_list == {"PERSON", "EMAIL_ADDRESS"}
    assert p.to_dict()["entity_type_include_list"] == ["EMAIL_ADDRESS", "PERSON"]


def test_from_dict_handles_none_and_missing():
    p = AnonymizationPolicy.from_dict({})
    assert p.strategies == {}
    p2 = AnonymizationPolicy.from_dict(None)
    assert p2.language == "en"


def test_to_entity_config():
    p = AnonymizationPolicy(strategies={"PERSON": "fake"})
    cfg = p.to_entity_config()
    assert isinstance(cfg, EntityConfig)
    assert cfg.strategies == {"PERSON": "fake"}


def test_from_yaml(tmp_path):
    yml = tmp_path / "policy.yml"
    yml.write_text(
        "strategies:\n"
        "  PERSON: replace\n"
        "  IN_AADHAAR: encrypt\n"
        "allow_list:\n"
        "  - Woodgrove Bank\n"
        "entity_type_allow_list:\n"
        "  - URL\n",
        encoding="utf-8",
    )
    p = AnonymizationPolicy.from_yaml(str(yml))
    assert p.strategies == {"PERSON": "replace", "IN_AADHAAR": "encrypt"}
    assert p.allow_list == ["Woodgrove Bank"]
    assert p.entity_type_allow_list == {"URL"}


def test_from_yaml_rejects_non_mapping(tmp_path):
    yml = tmp_path / "bad.yml"
    yml.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        AnonymizationPolicy.from_yaml(str(yml))
