"""Tests for mixed-case names — "Venkata narasimha raju".

A cased NER model stops at the first uncased word of a name, so the lowercase
middle and last names leaked.  The case-recovery pass (NER over a re-cased
copy) finds the whole name, but its result used to be rejected for mixed-case
text.  It is now accepted as PERSON, cut at any lowercase word that spaCy tags
as a verb, preposition, etc. or that is ordinary vocabulary, so "Ramesh paid"
never becomes a name.
"""

import pytest
import spacy
from fastapi.testclient import TestClient
from presidio_analyzer import RecognizerResult
from spacy.tokens import Doc

from pii_shield.pipeline import (
    common_word_starts,
    merge_recovered_results,
    needs_name_recovery,
    normalize_case,
    ordinary_word_starts,
)


def _result(entity_type: str, text: str, value: str, score: float = 0.85) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(
        entity_type=entity_type, start=start, end=start + len(value), score=score
    )
    r.recognition_metadata = {"recognizer_name": "TransformersRecognizer"}
    return r


def _ordinary(text: str, *words: str) -> frozenset[int]:
    """Offsets of *words* in *text*, standing in for spaCy's verb/ADP/... tags."""
    return frozenset(text.index(w) for w in words)


def _spans(text: str, results: list[RecognizerResult]) -> list[tuple[str, str]]:
    return sorted((text[r.start : r.end], r.entity_type) for r in results)


# ---------------------------------------------------------------------------
# ordinary_word_starts
# ---------------------------------------------------------------------------


class TestOrdinaryWordStarts:
    vocab = spacy.blank("en").vocab
    words = ["Ramesh", "paid", "the", "bill"]

    def test_offsets_of_non_name_parts_of_speech(self):
        doc = Doc(self.vocab, words=self.words, pos=["PROPN", "VERB", "DET", "NOUN"])
        assert ordinary_word_starts(doc) == frozenset({7, 12})

    def test_none_without_part_of_speech_tags(self):
        assert ordinary_word_starts(Doc(self.vocab, words=self.words)) is None

    def test_none_without_a_document(self):
        assert ordinary_word_starts(None) is None


class TestCommonWordStarts:
    vocab = spacy.blank("en").vocab
    words = ["Kavitha", "mother", "khan", "sharma"]

    def _doc(self, pos):
        return Doc(self.vocab, words=self.words, pos=pos)

    def test_common_words_not_tagged_as_proper_nouns(self):
        doc = self._doc(["PROPN", "NOUN", "NOUN", "NOUN"])
        assert common_word_starts(doc, frozenset({"mother", "khan"})) == frozenset({8, 15})

    def test_proper_noun_is_never_common(self):
        doc = self._doc(["PROPN", "NOUN", "PROPN", "NOUN"])
        assert common_word_starts(doc, frozenset({"mother", "khan"})) == frozenset({8})

    def test_without_a_vocabulary_every_non_proper_noun_is_common(self):
        doc = self._doc(["PROPN", "NOUN", "PROPN", "NOUN"])
        assert common_word_starts(doc, None) == frozenset({8, 20})

    def test_empty_without_part_of_speech_tags(self):
        assert common_word_starts(Doc(self.vocab, words=self.words), frozenset()) == frozenset()


# ---------------------------------------------------------------------------
# needs_name_recovery — a lone uncased word next to a detected name
# ---------------------------------------------------------------------------


class TestNeedsNameRecovery:
    def test_uncased_surname_after_name(self):
        text = "Rajesh Kumar sharma submitted his documents"
        assert needs_name_recovery([_result("PERSON", text, "Rajesh Kumar")], text, frozenset())

    def test_uncased_first_name_before_name(self):
        text = "rajesh Kumar Sharma submitted his documents"
        assert needs_name_recovery([_result("PERSON", text, "Kumar Sharma")], text, frozenset())

    def test_verb_after_name(self):
        text = "Suresh transferred money through netbanking"
        ordinary = _ordinary(text, "transferred")
        assert not needs_name_recovery([_result("PERSON", text, "Suresh")], text, ordinary)

    def test_vocabulary_word_after_entity(self):
        text = "Visit the Mumbai branch today"
        assert not needs_name_recovery([_result("LOCATION", text, "Mumbai")], text, frozenset())

    def test_word_on_next_line(self):
        text = "Rajesh Kumar\nsharma"
        assert not needs_name_recovery([_result("PERSON", text, "Rajesh Kumar")], text, frozenset())

    def test_word_touching_digits(self):
        text = "Rajesh Kumar sharma42 logged in"
        assert not needs_name_recovery([_result("PERSON", text, "Rajesh Kumar")], text, frozenset())

    def test_pattern_entity_neighbour_is_ignored(self):
        text = "Call 9876543210 sharma"
        phone = _result("PHONE_NUMBER", text, "9876543210")
        assert not needs_name_recovery([phone], text, frozenset())

    def test_without_part_of_speech_tags(self):
        text = "Rajesh Kumar sharma submitted his documents"
        assert not needs_name_recovery([_result("PERSON", text, "Rajesh Kumar")], text, None)

    def test_common_english_word_after_name(self):
        text = "Kavitha mother is the joint holder"
        person = [_result("PERSON", text, "Kavitha")]
        assert not needs_name_recovery(person, text, frozenset(), _ordinary(text, "mother"))

    def test_relation_word_after_name(self):
        text = "Anil bro sent the money"
        assert not needs_name_recovery([_result("PERSON", text, "Anil")], text, frozenset())

    def test_title_after_name(self):
        text = "Rahul sir will call you back"
        assert not needs_name_recovery([_result("PERSON", text, "Rahul")], text, frozenset())


def test_forced_normalization_recases_a_single_uncased_word():
    text = "Rajesh Kumar sharma submitted his documents"
    assert normalize_case(text) is None
    assert normalize_case(text, force=True) == "Rajesh Kumar Sharma submitted his documents"


# ---------------------------------------------------------------------------
# merge_recovered_results — mixed-case PERSON spans
# ---------------------------------------------------------------------------


class TestMixedCaseRecovery:
    def test_reported_example(self):
        text = "My name is Venkata narasimha raju."
        primary = [_result("PERSON", text, "Venkata")]
        recovered = [_result("PERSON", text, "Venkata narasimha raju")]
        out = merge_recovered_results(primary, recovered, text, frozenset())
        assert _spans(text, out) == [("Venkata narasimha raju", "PERSON")]

    def test_mislabelled_first_name_becomes_person(self):
        text = "Name: Venkata narasimha raju"
        primary = [_result("LOCATION", text, "Venkata")]
        recovered = [_result("PERSON", text, "Venkata narasimha raju")]
        out = merge_recovered_results(primary, recovered, text, frozenset())
        assert _spans(text, out) == [("Venkata narasimha raju", "PERSON")]

    def test_cut_at_verb(self):
        text = "Ramesh paid electricity bill"
        primary = [_result("PERSON", text, "Ramesh")]
        recovered = [_result("PERSON", text, "Ramesh paid")]
        out = merge_recovered_results(primary, recovered, text, _ordinary(text, "paid"))
        assert _spans(text, out) == [("Ramesh", "PERSON")]

    def test_cut_at_vocabulary_word(self):
        text = "Rajesh kumar sharma has applied"
        recovered = [_result("PERSON", text, "Rajesh kumar sharma has")]
        out = merge_recovered_results([], recovered, text, frozenset())
        assert _spans(text, out) == [("Rajesh kumar sharma", "PERSON")]

    def test_split_at_conjunction_into_two_names(self):
        text = "Rajesh kumar and Sunita devi signed"
        recovered = [_result("PERSON", text, "Rajesh kumar and Sunita devi")]
        out = merge_recovered_results([], recovered, text, _ordinary(text, "and"))
        assert _spans(text, out) == [("Rajesh kumar", "PERSON"), ("Sunita devi", "PERSON")]

    def test_dotted_initials_stay_in_the_name(self):
        text = "Signed by R.K. sharma today"
        recovered = [_result("PERSON", text, "R.K. sharma")]
        out = merge_recovered_results([], recovered, text, frozenset())
        assert _spans(text, out) == [("R.K. sharma", "PERSON")]

    def test_correctly_cased_name_adds_nothing(self):
        text = "Rahul bought new shoes"
        primary = [_result("PERSON", text, "Rahul")]
        recovered = [_result("PERSON", text, "Rahul")]
        assert merge_recovered_results(primary, recovered, text, frozenset()) == primary

    def test_fused_prose_is_rejected(self):
        text = "Kavitha visited Contoso Bank"
        recovered = [_result("PERSON", text, text)]
        assert merge_recovered_results([], recovered, text, _ordinary(text, "visited")) == []

    def test_mixed_case_rejected_without_part_of_speech_tags(self):
        text = "My name is Venkata narasimha raju."
        primary = [_result("PERSON", text, "Venkata")]
        recovered = [_result("PERSON", text, "Venkata narasimha raju")]
        assert merge_recovered_results(primary, recovered, text, None) == primary

    def test_mixed_case_non_person_still_rejected(self):
        text = "NOTE: Kavitha visited Contoso Bank yesterday"
        primary = [_result("PERSON", text, "Kavitha")]
        recovered = [_result("ORGANIZATION", text, "Kavitha")]
        assert merge_recovered_results(primary, recovered, text, frozenset()) == primary

    def test_validated_entity_still_wins(self):
        text = "Pay Anil kumar at anil@okaxis"
        upi = _result("IN_UPI_ID", text, "anil@okaxis")
        recovered = [_result("PERSON", text, "Anil kumar at anil")]
        out = merge_recovered_results([upi], recovered, text, _ordinary(text, "at"))
        assert _spans(text, out) == [("anil@okaxis", "IN_UPI_ID")]

    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("Anil d'souza has a pending dispute", "Anil d'souza"),
            ("Sofía martínez opened an account", "Sofía martínez"),
            ("Customer Venkata r raju called", "Venkata r raju"),
            ("Sunita kumari signed the form", "Sunita kumari"),
            ("Padma lakshmi, Andheri branch", "Padma lakshmi"),
        ],
    )
    def test_whole_name_kept(self, text, name):
        out = merge_recovered_results([], [_result("PERSON", text, name)], text, frozenset())
        assert _spans(text, out) == [(name, "PERSON")]

    @pytest.mark.parametrize(
        ("text", "recovered"),
        [
            ("Kavitha mother is the joint holder", "Kavitha mother"),
            ("Rahul laptop was stolen", "Rahul laptop"),
            ("Rahul sir will call you back", "Rahul sir"),
            ("Anil bro sent the money", "Anil bro"),
        ],
    )
    def test_relation_words_and_titles_are_not_name_parts(self, text, recovered):
        first_name = recovered.split()[0]
        primary = [_result("PERSON", text, first_name)]
        out = merge_recovered_results(primary, [_result("PERSON", text, recovered)], text, frozenset())
        assert _spans(text, out) == [(first_name, "PERSON")]

    def test_piece_never_masks_less_than_the_primary_pass(self):
        # Recovery keeps only "d'souza" of this span, but the primary pass
        # already had the whole name, so the result must still cover it.
        text = "Anil d'souza has a pending CIBIL dispute"
        primary = [_result("PERSON", text, "Anil d'souza")]
        recovered = [_result("PERSON", text, "d'souza has a pending CIBIL")]
        out = merge_recovered_results(primary, recovered, text, frozenset())
        assert _spans(text, out) == [("Anil d'souza", "PERSON")]


class TestSplitOffSurname:
    def test_lowercase_entity_after_name_is_its_surname(self):
        text = "Transfer INR 5,000 to Suresh babu naidu today"
        primary = [_result("LOCATION", text, "Suresh babu")]
        recovered = [_result("PERSON", text, "Suresh babu"), _result("LOCATION", text, "naidu")]
        out = merge_recovered_results(primary, recovered, text, _ordinary(text, "to"))
        assert _spans(text, out) == [("Suresh babu naidu", "PERSON")]

    @pytest.mark.parametrize(
        ("text", "leftover"),
        [
            ("Suresh babu, naidu", "naidu"),
            ("Suresh babu\nnaidu", "naidu"),
            ("Suresh babu Naidu Road", "Naidu Road"),
        ],
    )
    def test_not_absorbed_across_punctuation_or_cased_words(self, text, leftover):
        recovered = [_result("PERSON", text, "Suresh babu"), _result("LOCATION", text, leftover)]
        out = merge_recovered_results([], recovered, text, frozenset())
        assert ("Suresh babu", "PERSON") in _spans(text, out)
        assert all(r.end <= text.index(leftover) for r in out if r.entity_type == "PERSON")

    def test_ordinary_word_is_not_a_surname(self):
        text = "Suresh babu paid today"
        recovered = [_result("PERSON", text, "Suresh babu"), _result("LOCATION", text, "paid")]
        out = merge_recovered_results([], recovered, text, _ordinary(text, "paid"))
        assert _spans(text, out) == [("Suresh babu", "PERSON")]


# ---------------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


def _anonymize(client, text: str) -> tuple[str, dict[str, str], str]:
    resp = client.post("/anonymize_unique", json={"text": text})
    assert resp.status_code == 200
    data = resp.json()
    restored = client.post(
        "/deanonymize", json={"id": data["id"], "text": data["anonymized_text"]}
    ).json()["text"]
    return data["anonymized_text"], {v: k for k, v in data["entity_mapping"].items()}, restored


class TestMixedCaseEndToEnd:
    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("My name is Venkata narasimha raju.", "Venkata narasimha raju"),
            ("Rajesh kumar sharma has applied for a home loan", "Rajesh kumar sharma"),
            ("Customer Priya ramesh iyer called the branch yesterday", "Priya ramesh iyer"),
            ("Transfer INR 5,000 to Suresh babu naidu today", "Suresh babu naidu"),
            ("Account holder: Sunita devi agarwal", "Sunita devi agarwal"),
            ("The cheque was signed by Mohammed faisal khan", "Mohammed faisal khan"),
            ("Kavitha subramaniam opened a savings account", "Kavitha subramaniam"),
            ("Rajesh Kumar sharma submitted his documents", "Rajesh Kumar sharma"),
            ("Name: Venkata narasimha raju\nMobile: 9876543210", "Venkata narasimha raju"),
            ("Pamidighantam venkata subba rao is the nominee", "Pamidighantam venkata subba rao"),
            ("My son Arjun kumar needs a minor savings account.", "Arjun kumar"),
            ("Sita ram sharma is the nominee.", "Sita ram sharma"),
            ("Anil d'souza has a pending CIBIL dispute.", "Anil d'souza"),
            ("Sofía martínez opened an NRE account last week.", "Sofía martínez"),
        ],
    )
    def test_whole_name_is_masked(self, client, text, name):
        anonymized, reverse, restored = _anonymize(client, text)
        assert reverse.get(name, "").startswith("{{PERSON_")
        assert not any(word in anonymized for word in name.split())
        assert restored == text

    @pytest.mark.parametrize(
        ("text", "kept"),
        [
            ("Ramesh paid electricity bill using UPI", "paid electricity bill using UPI"),
            ("Meera booked movie tickets for Sunday", "booked movie tickets for"),
            ("Rahul bought new shoes from the mall", "bought new shoes from the mall"),
            ("Suresh transferred money through netbanking", "transferred money through netbanking"),
            ("NOTE: Kavitha visited Contoso Bank yesterday", "visited"),
            ("Kavitha mother is the joint holder.", "mother is the joint holder"),
            ("Kiran boss approved the salary advance.", "boss approved"),
            ("Rahul laptop was stolen along with his debit card.", "laptop was stolen"),
            ("Rahul sir will call you back.", "sir will call you back"),
            ("Anil bro sent the money yesterday.", "bro sent the money"),
        ],
    )
    def test_ordinary_words_after_a_name_stay(self, client, text, kept):
        anonymized, _, restored = _anonymize(client, text)
        assert kept in anonymized
        assert restored == text
