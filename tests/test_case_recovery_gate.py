"""Tests for the case-recovery gate: when the second NER pass is worth running.

``normalize_case`` opens for any ALL-CAPS word or run of lowercase words, and an e-mail address, a URL or a UPI
id is one by itself.  Documents whose only case oddity is such an entity used to pay for a second NER pass that
could not find anything.  The "smart" gate ignores words inside validated entities and ordinary vocabulary, and
keeps the pass for ALL-CAPS words and for unknown lowercase words, which may be uncased names.
"""

import pytest

from pii_shield.pipeline import has_recoverable_case_change, normalize_case


def _gate(text: str, validated_values: list[str], common_words: list[str] = ()) -> bool:
    normalized = normalize_case(text, force=True)
    assert normalized is not None
    validated = [(text.index(v), text.index(v) + len(v)) for v in validated_values]
    common = frozenset(text.index(w) for w in common_words)
    return has_recoverable_case_change(text, normalized, validated, common)


class TestHasRecoverableCaseChange:
    def test_email_only_has_nothing_to_recover(self):
        text = "Contact Rajesh Kumar at rajesh.kumar@gmail.com"
        assert _gate(text, ["rajesh.kumar@gmail.com"]) is False

    def test_the_same_words_outside_an_entity_count(self):
        text = "Contact Rajesh Kumar at rajesh kumar gmail"
        assert _gate(text, []) is True

    def test_all_caps_word_always_counts(self):
        text = "Dear RAJESH, mail rajesh@gmail.com"
        assert _gate(text, ["rajesh@gmail.com"]) is True

    def test_all_caps_word_counts_even_when_it_is_vocabulary(self):
        text = "STATEMENT sent to rajesh@gmail.com"
        assert _gate(text, ["rajesh@gmail.com"], common_words=["STATEMENT", "sent"]) is True

    def test_unknown_lowercase_word_may_be_a_name(self):
        text = "hi this is priya, my email is priya@gmail.com"
        assert _gate(text, ["priya@gmail.com"], common_words=["hi", "email"]) is True

    def test_ordinary_lowercase_words_do_not_count(self):
        text = "Rajesh visited the branch, mail rajesh@gmail.com"
        assert _gate(text, ["rajesh@gmail.com"], common_words=["visited", "mail"]) is False

    def test_a_word_only_partly_inside_an_entity_counts(self):
        text = "see rajeshkumar@gmail.com"
        start = text.index("rajeshkumar@gmail.com") + len("rajesh")
        normalized = normalize_case(text, force=True)
        assert has_recoverable_case_change(text, normalized, [(start, len(text))], frozenset()) is True

    def test_nested_entities_still_cover_the_words_of_the_outer_one(self):
        text = "Mail rajesh.kumar@gmail.com now"
        email = "rajesh.kumar@gmail.com"
        start = text.index(email)
        spans = [(start, start + len(email)), (start + 1, start + 3)]
        normalized = normalize_case(text, force=True)
        common = frozenset({text.index("Mail") + 0, text.index("now")})
        assert has_recoverable_case_change(text, normalized, spans, common) is False


@pytest.fixture()
def engine():
    from app.main import engine

    return engine


@pytest.fixture()
def passes(engine, monkeypatch):
    """How many times detection sent *text* through the NLP engine (one per NER pass)."""
    nlp_engine = engine._analyzer.nlp_engine
    original = nlp_engine.process_text
    calls: list[str] = []

    def counting(text, language):
        calls.append(text)
        return original(text, language)

    monkeypatch.setattr(nlp_engine, "process_text", counting)

    def count(text: str, gate: str = "smart") -> int:
        monkeypatch.setattr(engine, "_case_recovery_gate", gate)
        calls.clear()
        engine.detect(text)
        return len(calls)

    return count


class TestSecondPass:
    def test_an_email_alone_no_longer_costs_a_second_pass(self, passes):
        text = "Please send the signed form to rajesh.kumar@gmail.com tomorrow"
        assert passes(text, "smart") == 1
        assert passes(text, "legacy") == 2

    def test_an_all_caps_name_still_gets_its_second_pass(self, passes):
        assert passes("Dear RAJESH KUMAR, your form went to rajesh.kumar@gmail.com", "smart") == 2

    def test_an_unknown_lowercase_word_still_gets_its_second_pass(self, passes):
        assert passes("hi this is priya, my email is priya@gmail.com", "smart") == 2

    def test_skipping_the_pass_changes_no_entity(self, engine, monkeypatch):
        text = "Please send the signed form to rajesh.kumar@gmail.com tomorrow"
        found = {}
        for gate in ("smart", "legacy"):
            monkeypatch.setattr(engine, "_case_recovery_gate", gate)
            found[gate] = [(e.start, e.end, e.entity_type) for e in engine.detect(text)]
        assert found["smart"] == found["legacy"]

    def test_unknown_gate_is_rejected(self):
        from pii_shield.engine import PiiShieldEngine

        with pytest.raises(ValueError, match="case_recovery_gate"):
            PiiShieldEngine(case_recovery_gate="sometimes")
