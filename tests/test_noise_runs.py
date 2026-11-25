"""Noise runs: the NER model reads every character of a hash, a base64 payload or a dotted leader as a token.

``shorten_noise_for_ner`` blanks the inside of such runs in the copy of the text the NER model sees, at the same
length, so a document with an attachment or a table of dotted leaders no longer costs seconds of model time.
Text without a long run must come back unchanged: that is what keeps every other result the same.
"""

import re

import pytest

from pii_shield.pipeline import shorten_noise_for_ner

ORDINARY = [
    "Rajesh Kumar, PAN ABCPS1234K, Aadhaar 9876 5432 1098, mobile +91 98765 43210.",
    "Mail rajesh.kumar@example.com or see https://example.com/forms/abcdefghijklmnopqrstuvwxyz/page?id=1",
    '{"customer": {"name": "Priya Menon", "account": "123456789012345", "ifsc": "HDFC0001234"}}',
    "Card ****-****-****-1234 expires 12/27; ref 550e8400-e29b-41d4-a716-446655440000.",
    "Flat 402, Sai Heights, Andheri (West), Mumbai 400058 ....... see note 3",
    "NEFT/CR/701815908301/VIKRAM JOSHI/HDFC Bal 199505.50 ----- total",
    "Seven dots....... and thirty-one hex " + "a3f5c9d1" * 3 + "abcdef0",
    "x" * 31,
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_is_returned_unchanged(text):
    assert shorten_noise_for_ner(text) == text


def test_a_dotted_leader_keeps_a_token_at_each_end():
    text = "Applicant name " + "." * 2000 + " Rajesh Kumar"
    out = shorten_noise_for_ner(text)
    assert len(out) == len(text) and out.startswith("Applicant name .") and out.endswith(". Rajesh Kumar")
    assert re.fullmatch(r"\. {1998}\.", out[len("Applicant name "):len("Applicant name ") + 2000])


@pytest.mark.parametrize(
    "run",
    [
        "a3f5c9d1e7b2" * 6,  # a hash
        "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo+/" * 4 + "==",  # base64
        "_" * 40,  # a form blank
        "*" * 10,  # a masked number
        "=" * 8,
    ],
)
def test_noise_runs_are_blanked_inside_and_never_change_length(run):
    text = f"Name: {run} Rajesh Kumar signed the form"
    out = shorten_noise_for_ner(text)
    assert len(out) == len(text)
    assert out != text and out.startswith("Name: " + run[0]) and out.endswith(run[-1] + " Rajesh Kumar signed the form")
    middle = out[len("Name: ") + 1 : len("Name: ") + len(run) - 1]
    assert middle.strip() == ""


def test_the_thresholds():
    assert shorten_noise_for_ner("ab " + "." * 7 + " cd " * 10) == "ab " + "." * 7 + " cd " * 10
    assert shorten_noise_for_ner("ab " + "." * 8 + " cd " * 10) != "ab " + "." * 8 + " cd " * 10
    assert shorten_noise_for_ner("id " + "a" * 31 + " " + "b" * 31) == "id " + "a" * 31 + " " + "b" * 31
    assert shorten_noise_for_ner("id " + "a" * 32 + " tail text here") != "id " + "a" * 32 + " tail text here"


def test_a_ten_thousand_character_attachment_is_one_run():
    text = "Report attached: " + "QUJD" * 2500 + " sent to rajesh@example.com"
    out = shorten_noise_for_ner(text)
    assert len(out) == len(text) and out.count(" ") > 9900 and out.endswith("sent to rajesh@example.com")


@pytest.fixture()
def engine():
    from app.main import engine

    return engine


def test_the_model_sees_the_shortened_text_and_the_recognizers_the_whole_text(engine, monkeypatch):
    seen = []
    nlp_engine = engine._analyzer.nlp_engine
    original = nlp_engine.process_text

    def spy(text, language):
        seen.append(text)
        return original(text, language)

    monkeypatch.setattr(nlp_engine, "process_text", spy)
    text = "Name " + "." * 500 + " Rajesh Kumar, PAN ABCPS1234K, mail rajesh.kumar@example.com " + "0123456789abcdef" * 8
    found = {(e.entity_type, e.text) for e in engine.detect(text)}
    assert ("IN_PAN", "ABCPS1234K") in found and ("EMAIL_ADDRESS", "rajesh.kumar@example.com") in found
    assert seen and all(len(t) == len(text) and "........" not in t for t in seen)
