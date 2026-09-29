"""Tests for pii_shield.conversation.ConversationAnonymizer (stateful multi-turn).

Uses ``FakeEngine`` (deterministic detection + the real placeholder logic) so
these run without loading an NLP model.
"""

import pytest

from pii_shield.conversation import ConversationAnonymizer
from pii_shield.policy import AnonymizationPolicy
from pii_shield.session_store import InMemorySessionStore
from tests._fakes import FakeEngine

DETECTIONS = [
    ("PERSON", "John Smith"),
    ("PERSON", "Mary Jane"),
    ("EMAIL_ADDRESS", "john@x.com"),
    ("LOCATION", "Paris"),
]


@pytest.fixture()
def conv():
    engine = FakeEngine(DETECTIONS)
    return ConversationAnonymizer(
        engine, "conv-1", store=InMemorySessionStore(), policy=AnonymizationPolicy()
    )


def test_basic_anonymize(conv):
    r = conv.anonymize("Contact John Smith at john@x.com")
    assert "John Smith" not in r.anonymized_text
    assert "john@x.com" not in r.anonymized_text
    assert "{{PERSON_1}}" in r.anonymized_text
    assert r.entity_mapping["{{PERSON_1}}"] == "John Smith"


def test_same_value_same_placeholder_across_turns(conv):
    t1 = conv.anonymize("Contact John Smith at john@x.com")
    t2 = conv.anonymize("John Smith emailed again")
    # John Smith must keep the SAME placeholder on turn 2.
    assert "{{PERSON_1}}" in t2.anonymized_text
    assert t2.entity_mapping["{{PERSON_1}}"] == "John Smith"
    # Turn 2 mapping is a superset of turn 1 (accumulated).
    assert set(t1.entity_mapping).issubset(set(t2.entity_mapping))


def test_new_value_continues_counter(conv):
    conv.anonymize("John Smith here")          # PERSON_1
    t2 = conv.anonymize("Mary Jane arrived")   # PERSON_2 (counter continues)
    assert "{{PERSON_2}}" in t2.anonymized_text
    assert t2.entity_mapping["{{PERSON_2}}"] == "Mary Jane"
    assert t2.entity_mapping["{{PERSON_1}}"] == "John Smith"


def test_roundtrip_deanonymize_uses_accumulated_mapping(conv):
    conv.anonymize("John Smith here")
    conv.anonymize("Mary Jane arrived")
    restored = conv.deanonymize("{{PERSON_1}} met {{PERSON_2}}")
    assert restored == "John Smith met Mary Jane"


def test_deanonymize_before_any_state_returns_text():
    engine = FakeEngine(DETECTIONS)
    c = ConversationAnonymizer(engine, "empty", store=InMemorySessionStore())
    assert c.deanonymize("{{PERSON_1}}") == "{{PERSON_1}}"


def test_mapping_property_and_clear(conv):
    conv.anonymize("John Smith here")
    assert conv.mapping.get("{{PERSON_1}}") == "John Smith"
    assert conv.clear() is True
    assert conv.mapping == {}


def test_stats_populated(conv):
    r = conv.anonymize("Contact John Smith at john@x.com")
    assert r.stats is not None
    assert r.stats.entity_count == 2
    assert r.stats.entity_counts.get("PERSON") == 1
    assert r.stats.total_ms >= 0.0


def test_on_event_callback(conv):
    events = []
    conv.anonymize("John Smith here", on_event=events.append)
    assert len(events) == 1
    assert events[0].name == "anonymize"
    assert events[0].conversation_id == "conv-1"
    assert events[0].entity_count == 1


def test_isolated_conversations_share_engine_and_store():
    engine = FakeEngine(DETECTIONS)
    store = InMemorySessionStore()
    a = ConversationAnonymizer(engine, "A", store=store)
    b = ConversationAnonymizer(engine, "B", store=store)
    a.anonymize("John Smith")
    b.anonymize("Mary Jane")
    # Each conversation numbers independently from its own state.
    assert a.mapping == {"{{PERSON_1}}": "John Smith"}
    assert b.mapping == {"{{PERSON_1}}": "Mary Jane"}


def test_policy_entity_type_allow_list_passthrough():
    engine = FakeEngine(DETECTIONS)
    policy = AnonymizationPolicy(entity_type_allow_list={"EMAIL_ADDRESS"})
    c = ConversationAnonymizer(engine, "c", store=InMemorySessionStore(), policy=policy)
    r = c.anonymize("Contact John Smith at john@x.com")
    # Email type is allow-listed -> left intact; person still masked.
    assert "john@x.com" in r.anonymized_text
    assert "John Smith" not in r.anonymized_text


def test_policy_entity_type_include_list_masks_only_listed():
    engine = FakeEngine(DETECTIONS)
    policy = AnonymizationPolicy(entity_type_include_list={"PERSON"})
    c = ConversationAnonymizer(engine, "c", store=InMemorySessionStore(), policy=policy)
    r = c.anonymize("Contact John Smith at john@x.com in Paris")
    # Only PERSON is anonymized; EMAIL_ADDRESS and LOCATION are left untouched.
    assert "John Smith" not in r.anonymized_text
    assert "{{PERSON_1}}" in r.anonymized_text
    assert "john@x.com" in r.anonymized_text
    assert "Paris" in r.anonymized_text


def test_fake_strategy_consistent_across_turns():
    engine = FakeEngine(DETECTIONS)
    policy = AnonymizationPolicy(strategies={"PERSON": "fake"})
    c = ConversationAnonymizer(engine, "c", store=InMemorySessionStore(), policy=policy)
    t1 = c.anonymize("John Smith here")
    t2 = c.anonymize("John Smith again")
    # The fake substitution for the same value is stable across turns.
    fake_names_1 = set(t1.entity_mapping.values())
    assert "John Smith" in fake_names_1
    # t2 anonymized text should contain the same fake value used in t1.
    fake_placeholder = [k for k, v in t1.entity_mapping.items() if v == "John Smith"][0]
    assert fake_placeholder in t2.anonymized_text
