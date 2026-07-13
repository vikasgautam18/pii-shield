"""Tests for pii_shield.middleware.PiiMiddleware (facade + async helpers)."""

import asyncio

from pii_shield.middleware import PiiMiddleware
from pii_shield.policy import AnonymizationPolicy
from pii_shield.session_store import InMemorySessionStore
from pii_shield.streaming import StreamingDeanonymizer
from tests._fakes import FakeEngine

DETECTIONS = [
    ("PERSON", "John Smith"),
    ("PERSON", "Mary Jane"),
    ("EMAIL_ADDRESS", "john@x.com"),
]


def _mw():
    return PiiMiddleware(
        engine=FakeEngine(DETECTIONS),
        store=InMemorySessionStore(),
        policy=AnonymizationPolicy(),
    )


def test_anonymize_deanonymize_roundtrip():
    mw = _mw()
    r = mw.anonymize("c1", "Contact John Smith at john@x.com")
    assert "John Smith" not in r.anonymized_text
    restored = mw.deanonymize("c1", r.anonymized_text)
    assert restored == "Contact John Smith at john@x.com"


def test_conversations_are_isolated():
    mw = _mw()
    r1 = mw.anonymize("c1", "John Smith")
    r2 = mw.anonymize("c2", "Mary Jane")
    # Independent numbering per conversation.
    assert r1.entity_mapping == {"{{PERSON_1}}": "John Smith"}
    assert r2.entity_mapping == {"{{PERSON_1}}": "Mary Jane"}
    # Cross-conversation deanonymize does not bleed.
    assert mw.deanonymize("c2", "{{PERSON_1}}") == "Mary Jane"


def test_streaming_deanonymizer_from_middleware():
    mw = _mw()
    r = mw.anonymize("c1", "Hi John Smith")
    sd = mw.streaming_deanonymizer("c1")
    assert isinstance(sd, StreamingDeanonymizer)
    text = r.anonymized_text
    # Feed one char at a time; nothing should leak a partial placeholder.
    out = "".join(sd.feed(ch) for ch in text) + sd.flush()
    assert out == "Hi John Smith"
    assert "{{" not in out


def test_clear():
    mw = _mw()
    mw.anonymize("c1", "John Smith")
    assert mw.clear("c1") is True
    assert mw.deanonymize("c1", "{{PERSON_1}}") == "{{PERSON_1}}"


def test_async_helpers():
    mw = _mw()

    async def run():
        r = await mw.anonymize_async("c1", "John Smith here")
        restored = await mw.deanonymize_async("c1", r.anonymized_text)
        return r, restored

    r, restored = asyncio.run(run())
    assert "{{PERSON_1}}" in r.anonymized_text
    assert restored == "John Smith here"


def test_conversation_accessor_reuses_shared_state():
    mw = _mw()
    mw.conversation("c1").anonymize("John Smith")
    # A fresh conversation handle sees the persisted state.
    assert mw.conversation("c1").mapping.get("{{PERSON_1}}") == "John Smith"
