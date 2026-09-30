"""Tests for extending PERSON spans across dotted initials.

Cased NER models end the entity at dotted initials: "Mr. R.K. Sharma" yields
only "R.K." (score 1.00) and the surname is never detected, leaking it. The
same name without dots ("R K Sharma") returns the whole span, so the periods
are the trigger.
"""

from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import extend_person_over_initials


def _person(text: str, value: str) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(
        entity_type="PERSON", start=start, end=start + len(value), score=1.0
    )
    r.recognition_metadata = {"recognizer_name": "TransformersRecognizer"}
    return r


def _span(text: str, results: list[RecognizerResult]) -> str:
    out = extend_person_over_initials(results, text)
    return text[out[0].start : out[0].end]


# ---------------------------------------------------------------------------
# surname absorbed
# ---------------------------------------------------------------------------


def test_two_initials_plus_surname():
    text = "Mr. R.K. Sharma has written a complaint"
    assert _span(text, [_person(text, "R.K.")]) == "R.K. Sharma"


def test_single_initial_plus_surname():
    text = "Ms. S. Iyer called the branch"
    assert _span(text, [_person(text, "S.")]) == "S. Iyer"


def test_three_initials_plus_two_names():
    text = "Dr. A.P.J. Abdul Kalam visited the branch"
    assert _span(text, [_person(text, "A.P.J.")]) == "A.P.J. Abdul Kalam"


def test_initials_with_internal_spaces():
    text = "Cheque signed by R. K. Sharma today"
    assert _span(text, [_person(text, "R. K.")]) == "R. K. Sharma"


def test_stops_at_lowercase_word():
    text = "Complaint filed by R.K. Sharma yesterday"
    assert _span(text, [_person(text, "R.K.")]) == "R.K. Sharma"


def test_absorbs_at_most_two_tokens():
    text = "R.K. Sharma Verma Gupta filed it"
    assert _span(text, [_person(text, "R.K.")]) == "R.K. Sharma Verma"


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def test_common_word_after_initials_not_absorbed():
    # "Next" starts a new clause, not a surname.
    text = "Send it to A.B. Next week we will follow up"
    assert _span(text, [_person(text, "A.B.")]) == "A.B."


def test_title_after_initials_not_absorbed():
    text = "Report to J.P. Mr Sharma tomorrow"
    assert _span(text, [_person(text, "J.P.")]) == "J.P."


def test_ordinary_name_span_untouched():
    # Not initials-shaped, so the right-extension never runs.
    text = "Mr. Rajesh Sharma has written a complaint"
    assert _span(text, [_person(text, "Rajesh")]) == "Rajesh"


def test_undotted_initials_untouched():
    # NER already returns the whole span when there are no periods.
    text = "The cheque was signed by R K Sharma"
    assert _span(text, [_person(text, "R K Sharma")]) == "R K Sharma"


def test_initials_at_end_of_text():
    text = "The signatory is R.K."
    assert _span(text, [_person(text, "R.K.")]) == "R.K."


def test_non_person_entity_untouched():
    text = "Ref A.B. Contoso Bank"
    r = RecognizerResult(entity_type="ORGANIZATION", start=4, end=8, score=0.9)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    assert _span(text, [r]) == "A.B."


# ---------------------------------------------------------------------------
# spaced initials, where NER returns only the first one
# ---------------------------------------------------------------------------


def test_absorbs_further_spaced_initials():
    # ONNX returns only "R." here; "K." and the surname must both be pulled in.
    text = "Cheque signed by R. K. Sharma today"
    assert _span(text, [_person(text, "R.")]) == "R. K. Sharma"


def test_absorbs_three_spaced_initials():
    text = "Report by A. P. J. Kalam filed today"
    assert _span(text, [_person(text, "A.")]) == "A. P. J. Kalam"


def test_trailing_initial_without_surname():
    text = "Signed R. K. yesterday"
    assert _span(text, [_person(text, "R.")]) == "R. K."
