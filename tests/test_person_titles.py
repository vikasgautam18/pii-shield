"""Tests for honorific / professional title handling.

Cased NER handles the familiar Western titles but not Indian professional
ones.  "CA Abhay Sharma" and "CS Priya" come back as ORGANIZATION — which
still redacts under the default strategy but leaks the name outright for any
app that allow-lists ORGANIZATION, and applies the wrong per-entity strategy.
"Er. Ram" and "Col. Rana" fail the other way: the abbreviation itself is
tagged PERSON, leaving a stray "." span behind.
"""

from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import normalize_person_titles


def _ent(text: str, value: str, entity_type: str) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(
        entity_type=entity_type, start=start, end=start + len(value), score=0.9
    )
    r.recognition_metadata = {"recognizer_name": "TransformersRecognizer"}
    return r


def _out(text: str, results: list[RecognizerResult]) -> list[tuple[str, str]]:
    return [
        (r.entity_type, text[r.start : r.end])
        for r in normalize_person_titles(results, text)
    ]


# ---------------------------------------------------------------------------
# title inside the span — trimmed, remainder forced to PERSON
# ---------------------------------------------------------------------------


def test_ca_organization_becomes_person():
    text = "CA Abhay Sharma has approved the loan"
    assert _out(text, [_ent(text, "CA Abhay Sharma", "ORGANIZATION")]) == [
        ("PERSON", "Abhay Sharma")
    ]


def test_cs_organization_becomes_person():
    text = "CS Priya has approved the loan"
    assert _out(text, [_ent(text, "CS Priya", "ORGANIZATION")]) == [
        ("PERSON", "Priya")
    ]


def test_title_trimmed_from_person_span():
    text = "Shri Ramesh has approved the loan"
    assert _out(text, [_ent(text, "Shri Ramesh", "PERSON")]) == [("PERSON", "Ramesh")]


def test_dotted_title_trimmed():
    text = "Smt. Kavita Deshmukh visited the branch"
    assert _out(text, [_ent(text, "Smt. Kavita Deshmukh", "PERSON")]) == [
        ("PERSON", "Kavita Deshmukh")
    ]


def test_stacked_titles_trimmed():
    text = "Lt. Col. Vikram Rana called"
    assert _out(text, [_ent(text, "Lt. Col. Vikram Rana", "PERSON")]) == [
        ("PERSON", "Vikram Rana")
    ]


# ---------------------------------------------------------------------------
# title outside the span — entity relabelled
# ---------------------------------------------------------------------------


def test_organization_after_title_becomes_person():
    text = "Smt. Kavita has approved the loan"
    assert _out(text, [_ent(text, "Kavita", "ORGANIZATION")]) == [("PERSON", "Kavita")]


def test_nrp_after_title_becomes_person():
    text = "Er. Ram has approved the loan"
    assert _out(text, [_ent(text, "Ram", "NRP")]) == [("PERSON", "Ram")]


def test_organization_without_title_is_untouched():
    text = "Contoso Manufacturing has approved the loan"
    assert _out(text, [_ent(text, "Contoso Manufacturing", "ORGANIZATION")]) == [
        ("ORGANIZATION", "Contoso Manufacturing")
    ]


# ---------------------------------------------------------------------------
# degenerate spans dropped
# ---------------------------------------------------------------------------


def test_title_only_span_dropped():
    text = "Er. Ram Kumar filed the report"
    assert _out(text, [_ent(text, "Er", "PERSON")]) == []


def test_dotted_title_only_span_dropped():
    text = "Col. Rana has approved the loan"
    assert _out(text, [_ent(text, "Col.", "PERSON")]) == []


def test_punctuation_only_span_dropped():
    # "Er. Ram Kumar" produced a PERSON whose value was a literal period.
    text = "Er. Ram Kumar filed the report"
    assert _out(text, [_ent(text, ".", "PERSON")]) == []


def test_real_name_not_dropped():
    text = "Ram Kumar filed the report"
    assert _out(text, [_ent(text, "Ram Kumar", "PERSON")]) == [("PERSON", "Ram Kumar")]


def test_non_ner_entity_never_dropped():
    # A validated recognizer's span is authoritative even if it looks title-ish.
    text = "Reference CS is 9876543210"
    assert _out(text, [_ent(text, "9876543210", "IN_BANK_ACCOUNT")]) == [
        ("IN_BANK_ACCOUNT", "9876543210")
    ]
