"""Tests for context words written as the key of a key=value pair.

spaCy keeps a URL, and a ``key=value`` pair written without spaces, as one
token, so Presidio never saw "aadhaar" in "https://api.com?aadhaar=987654321098"
or "aadhaar:987654321098" as a context word.  An Aadhaar number without
separators scores 0.30, below the 0.35 threshold, so it leaked.  The key
directly before a value now counts as context.
"""

import pytest
import spacy
from fastapi.testclient import TestClient
from presidio_analyzer import RecognizerResult
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_analyzer.nlp_engine import NlpArtifacts

from pii_shield.context_enhancer import KeyValueContextEnhancer, key_words_before
from pii_shield.pipeline import merge_recovered_results, remove_allowed_overlaps
from pii_shield.recognizers import InAadhaarImprovedRecognizer

_NLP = spacy.blank("en")


class _KeywordEngine:
    """Stands in for the NLP engine when ``NlpArtifacts`` picks keywords."""

    def is_stopword(self, word: str, language: str) -> bool:
        return word.lower() in {"my", "is", "the", "a"}

    def is_punct(self, word: str, language: str) -> bool:
        return not any(c.isalnum() for c in word)


def _artifacts(text: str) -> NlpArtifacts:
    doc = _NLP(text)
    return NlpArtifacts(
        entities=[],
        tokens=doc,
        tokens_indices=[t.idx for t in doc],
        lemmas=[t.text for t in doc],
        nlp_engine=_KeywordEngine(),
        language="en",
    )


def _aadhaar_score(enhancer, text: str) -> float:
    recognizer = InAadhaarImprovedRecognizer()
    recognizer.context = ["aadhaar", "uid"]
    results = recognizer.analyze(text, ["IN_AADHAAR"])
    assert [text[r.start : r.end] for r in results] == ["987654321098"]
    enhanced = enhancer.enhance_using_context(text, results, _artifacts(text), [recognizer])
    return enhanced[0].score


def _enhancer(cls):
    return cls(
        context_similarity_factor=0.45,
        context_suffix_count=5,
        context_matching_mode="whole_word",
    )


# ---------------------------------------------------------------------------
# key_words_before
# ---------------------------------------------------------------------------


class TestKeyWordsBefore:
    @pytest.mark.parametrize(
        ("token", "value", "words"),
        [
            ("https://api.com?aadhaar=987654321098", "987654321098", ["aadhaar"]),
            ("aadhaar:987654321098", "987654321098", ["aadhaar"]),
            ("?aadhaar_no=987654321098", "987654321098", ["aadhaar", "no"]),
            ("aadhaarNumber=987654321098", "987654321098", ["aadhaar", "number"]),
            ("&PAN_NO=ABCPK1234L", "ABCPK1234L", ["pan", "no"]),
            ("customer.uid=987654321098", "987654321098", ["customer", "uid"]),
        ],
    )
    def test_key_before_the_value(self, token, value, words):
        assert key_words_before(token, token.rindex(value)) == words

    @pytest.mark.parametrize(
        ("token", "value"),
        [
            ("aadhaar987654321098", "987654321098"),  # no separator
            ("aadhaar=IN987654321098", "987654321098"),  # value not right after it
            ("987654321098", "987654321098"),  # nothing before it
            ("k" * 41 + "=987654321098", "987654321098"),  # implausibly long key
        ],
    )
    def test_no_key(self, token, value):
        assert key_words_before(token, token.index(value)) == []


# ---------------------------------------------------------------------------
# KeyValueContextEnhancer
# ---------------------------------------------------------------------------


class TestKeyValueContextEnhancer:
    @pytest.mark.parametrize(
        "text",
        [
            "my website is https://api.com?aadhaar=987654321098&pan=ABCPK1234L.",
            "aadhaar=987654321098",
            "log: uid=987654321098 status=ok",
        ],
    )
    def test_key_in_the_same_token_boosts_the_score(self, text):
        assert _aadhaar_score(_enhancer(LemmaContextAwareEnhancer), text) == pytest.approx(0.30)
        assert _aadhaar_score(_enhancer(KeyValueContextEnhancer), text) == pytest.approx(0.75)

    def test_key_of_another_pair_does_not_count(self):
        # "aadhaar" is the key of the first pair, not of the number.
        text = "https://api.com?aadhaar=yes&ref=987654321098"
        assert _aadhaar_score(_enhancer(KeyValueContextEnhancer), text) == pytest.approx(0.30)

    def test_separate_words_still_count_as_before(self):
        text = "My aadhaar is 987654321098"
        assert _aadhaar_score(_enhancer(KeyValueContextEnhancer), text) == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# merge_recovered_results
# ---------------------------------------------------------------------------


def _result(entity_type: str, text: str, value: str, score: float = 0.85) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(entity_type=entity_type, start=start, end=start + len(value), score=score)
    r.recognition_metadata = {"recognizer_name": "TransformersRecognizer"}
    return r


def _spans(text: str, results: list[RecognizerResult]) -> list[tuple[str, str]]:
    return sorted((text[r.start : r.end], r.entity_type) for r in results)


class TestRecoveredSpanOverKeyValue:
    def test_name_next_to_a_key_value_pair_is_kept(self):
        # NER expands its tag over the whole token "uid=987654321098".
        text = "log: user=rahul uid=987654321098 status=ok"
        primary = [_result("IN_AADHAAR", text, "987654321098", 0.75)]
        recovered = [_result("PERSON", text, "rahul uid=987654321098")]
        assert _spans(text, merge_recovered_results(primary, recovered, text)) == [
            ("987654321098", "IN_AADHAAR"), ("rahul", "PERSON"),
        ]

    def test_nothing_left_after_the_pair(self):
        text = "log: uid=987654321098 status=ok"
        primary = [_result("IN_AADHAAR", text, "987654321098", 0.75)]
        recovered = [_result("PERSON", text, "uid=987654321098")]
        assert merge_recovered_results(primary, recovered, text) is primary

    def test_other_validated_overlap_still_drops_the_span(self):
        # A label in front of an ID is not a name.
        text = "dl gj 01 2012 0034567"
        primary = [_result("IN_DRIVING_LICENSE", text, "gj 01 2012 0034567")]
        recovered = [_result("PERSON", text, "dl gj")]
        assert merge_recovered_results(primary, recovered, text) is primary

    @pytest.mark.parametrize(
        ("text", "span", "name"),
        [
            ("name=rahul&aadhaar=987654321098", "rahul&aadhaar=987654321098", "rahul"),
            ("rahul sharma|aadhaar:987654321098", "rahul sharma|aadhaar:987654321098",
             "rahul sharma"),
            ("uid=987654321098;rahul sharma", "uid=987654321098;rahul sharma", "rahul sharma"),
        ],
    )
    def test_name_joined_to_the_pair_by_a_delimiter_is_kept(self, text, span, name):
        # Only the key, separator and value are cut, not the whole word.
        primary = [_result("IN_AADHAAR", text, "987654321098", 0.75)]
        recovered = [_result("PERSON", text, span)]
        assert _spans(text, merge_recovered_results(primary, recovered, text)) == [
            ("987654321098", "IN_AADHAAR"), (name, "PERSON"),
        ]

    def test_mixed_case_span_is_judged_before_the_cut(self):
        # "Priya" alone is not a span the recovery accepts; the span it came in
        # is, as it was while the Aadhaar number went undetected.
        text = "DEBUG customer Priya;aadhaar=987654321098"
        primary = [_result("IN_AADHAAR", text, "987654321098", 0.75)]
        recovered = [_result("PERSON", text, "Priya;aadhaar=987654321098")]
        out = _spans(text, merge_recovered_results(primary, recovered, text, frozenset()))
        assert ("Priya", "PERSON") in out
        assert ("987654321098", "IN_AADHAAR") in out
        assert not any("aadhaar" in span for span, _ in out)

    def test_part_is_widened_over_the_first_pass_span_it_replaces(self):
        # "RAHUL SHARMA" alone would evict the wider first-pass span and
        # expose "RAHUL"'s neighbours; widened, it masks everything it did.
        text = "INFO customer PAN=ABCPK1234L;RAHUL SHARMA"
        primary = [
            _result("IN_PAN", text, "ABCPK1234L"),
            _result("ORGANIZATION", text, "ABCPK1234L;RAHUL SHARMA"),
        ]
        recovered = [_result("PERSON", text, "PAN=ABCPK1234L;RAHUL SHARMA")]
        assert _spans(text, merge_recovered_results(primary, recovered, text)) == [
            ("ABCPK1234L", "IN_PAN"), ("ABCPK1234L;RAHUL SHARMA", "PERSON"),
        ]


# ---------------------------------------------------------------------------
# remove_allowed_overlaps
# ---------------------------------------------------------------------------


class TestRemoveAllowedOverlaps:
    def _allowed(self, text: str, value: str) -> list[tuple[int, int]]:
        start = text.index(value)
        return [(start, start + len(value))]

    def test_ner_span_keeps_the_name_outside_the_allowed_value(self):
        text = "name=Rahul Sharma&account=12345678901"
        results = [_result("PERSON", text, "Rahul Sharma&account=12345678901")]
        kept = remove_allowed_overlaps(results, text, self._allowed(text, "12345678901"))
        assert _spans(text, kept) == [("Rahul Sharma", "PERSON")]

    def test_pattern_entity_inside_an_allowed_one_is_dropped(self):
        text = "Mail john@example.com today"
        url = _result("URL", text, "example.com")
        assert remove_allowed_overlaps([url], text, self._allowed(text, "john@example.com")) == []

    def test_ner_span_inside_the_allowed_value_is_dropped(self):
        text = "Visit Contoso Bank today"
        org = _result("ORGANIZATION", text, "Contoso")
        assert remove_allowed_overlaps([org], text, self._allowed(text, "Contoso Bank")) == []

    def test_entities_elsewhere_are_kept(self):
        text = "Rahul Sharma, account=12345678901"
        person = _result("PERSON", text, "Rahul Sharma")
        assert remove_allowed_overlaps([person], text, self._allowed(text, "12345678901")) == [
            person
        ]


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
    labels = {v: k.strip("{}").rsplit("_", 1)[0] for k, v in data["entity_mapping"].items()}
    return data["anonymized_text"], labels, restored


class TestKeyValueEndToEnd:
    def test_reported_url(self, client):
        text = "my website is https://api.com?aadhaar=987654321098&pan=ABCPK1234L."
        anonymized, _, restored = _anonymize(client, text)
        assert anonymized == "my website is {{URL_1}}?aadhaar={{IN_AADHAAR_1}}&pan={{IN_PAN_1}}."
        assert restored == text

    @pytest.mark.parametrize(
        "text",
        [
            "aadhaar=987654321098",
            "aadhaar:987654321098",
            "aadhaarNumber=987654321098",
            "Aadhaar:987654321098, PAN:ABCPK1234L",
            "pan=ABCPK1234L&aadhaar=987654321098",
            "https://api.com?uid=987654321098",
            "GET /kyc?aadhaar_no=987654321098&mobile=9876543210 HTTP/1.1",
        ],
    )
    def test_aadhaar_value_is_masked(self, client, text):
        anonymized, labels, restored = _anonymize(client, text)
        assert labels.get("987654321098") == "IN_AADHAAR"
        assert "987654321098" not in anonymized
        assert restored == text

    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("log: user=rahul uid=987654321098 status=ok", "rahul"),
            ("LOG: USER=RAHUL UID=987654321098 STATUS=OK", "RAHUL"),
        ],
    )
    def test_name_next_to_the_pair_stays_masked(self, client, text, name):
        anonymized, labels, restored = _anonymize(client, text)
        assert labels.get(name) == "PERSON"
        assert labels.get("987654321098") == "IN_AADHAAR"
        assert name not in anonymized and "987654321098" not in anonymized
        assert restored == text

    @pytest.mark.parametrize(
        ("text", "secrets"),
        [
            ("POST /kyc?name=rahul&aadhaar=987654321098 HTTP/1.1", ["rahul", "987654321098"]),
            ("name=rahul sharma&aadhaar=987654321098", ["rahul", "sharma", "987654321098"]),
            ("rahul sharma|aadhaar:987654321098|active", ["rahul", "sharma", "987654321098"]),
            ("INFO customer PAN=ABCPK1234L;RAHUL SHARMA", ["RAHUL", "SHARMA", "ABCPK1234L"]),
            ("DEBUG customer Priya;aadhaar=987654321098", ["Priya", "987654321098"]),
            ("LOG: user=aadhaar=987654321098&Kavitha", ["Kavitha", "987654321098"]),
        ],
    )
    def test_name_joined_to_the_pair_stays_masked(self, client, text, secrets):
        anonymized, _, restored = _anonymize(client, text)
        assert [s for s in secrets if s in anonymized] == []
        assert restored == text

    @pytest.mark.parametrize(
        ("text", "allowed", "value"),
        [
            ("name=Rahul Sharma&account=12345678901", "IN_BANK_ACCOUNT", "12345678901"),
            ("Customer Rahul Sharma&uid=987654321098 updated", "IN_AADHAAR", "987654321098"),
        ],
    )
    def test_allow_listed_value_does_not_expose_the_name(self, client, text, allowed, value):
        resp = client.post(
            "/anonymize_unique", json={"text": text, "entity_type_allow_list": [allowed]}
        )
        anonymized = resp.json()["anonymized_text"]
        assert "Rahul" not in anonymized and "Sharma" not in anonymized
        assert value in anonymized

    @pytest.mark.parametrize(
        ("text", "allowed", "secrets"),
        [
            ("INFO name=Rahul&aadhaar=987654321098", "LOCATION", ["Rahul", "987654321098"]),
            ("INFO name=Priya&aadhaar=987654321098", "ORGANIZATION", ["Priya", "987654321098"]),
        ],
    )
    def test_allow_listed_ner_type_does_not_expose_the_pair(self, client, text, allowed, secrets):
        resp = client.post(
            "/anonymize_unique", json={"text": text, "entity_type_allow_list": [allowed]}
        )
        anonymized = resp.json()["anonymized_text"]
        assert [s for s in secrets if s in anonymized] == []

    @pytest.mark.parametrize(
        "text",
        ["txn_id=987654321098&amount=500", "Order 987654321098 dispatched today"],
    )
    def test_keys_that_are_not_context_words_change_nothing(self, client, text):
        anonymized, _, _ = _anonymize(client, text)
        assert anonymized == text
