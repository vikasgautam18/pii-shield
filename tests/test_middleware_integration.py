"""End-to-end integration test with the real PiiShieldEngine (loads NLP model).

Validates the stateful multi-turn + streaming round-trip on genuine detection.
The engine is session-scoped so the model loads only once.
"""

import pytest

from pii_shield.middleware import PiiMiddleware
from pii_shield.engine import PiiShieldEngine
from pii_shield.policy import AnonymizationPolicy
from pii_shield.session_store import InMemorySessionStore


@pytest.fixture(scope="session")
def engine():
    return PiiShieldEngine()


@pytest.fixture()
def middleware(engine):
    return PiiMiddleware(
        engine=engine,
        store=InMemorySessionStore(),
        policy=AnonymizationPolicy(),
    )


def test_multiturn_consistency_and_roundtrip(middleware):
    t1 = middleware.anonymize("c1", "Call John Smith at john.smith@example.com.")
    # PII removed from turn 1.
    assert "John Smith" not in t1.anonymized_text
    assert "john.smith@example.com" not in t1.anonymized_text
    assert "{{PERSON_1}}" in t1.anonymized_text

    # Turn 2: the same person must reuse the same placeholder.
    t2 = middleware.anonymize("c1", "John Smith called back today.")
    assert "{{PERSON_1}}" in t2.anonymized_text
    assert t2.entity_mapping["{{PERSON_1}}"] == "John Smith"

    # Round-trip a simulated LLM reply that references the placeholder.
    restored = middleware.deanonymize("c1", "I emailed {{PERSON_1}} back.")
    assert restored == "I emailed John Smith back."


def test_streaming_roundtrip_no_leak(middleware):
    r = middleware.anonymize("c2", "Contact Jane Doe at jane.doe@example.com now.")
    reply = r.anonymized_text  # placeholders the LLM would echo
    sd = middleware.streaming_deanonymizer("c2")
    # Stream char-by-char (worst case for split placeholders).
    out = "".join(sd.feed(ch) for ch in reply) + sd.flush()
    assert out == "Contact Jane Doe at jane.doe@example.com now."
    assert "{{" not in out


def test_engine_backward_compatible_single_call(engine):
    # The refactored engine still behaves as before for a one-shot call.
    r = engine.anonymize("Call John Smith at john.smith@example.com.")
    assert "John Smith" not in r.anonymized_text
    assert r.stats is not None and r.stats.entity_count >= 2
