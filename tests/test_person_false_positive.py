"""Tests for filter_person_false_positives — sentence-initial non-name PERSON drop."""

from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import filter_person_false_positives


def _person(text: str, value: str, recognizer: str = "SpacyRecognizer") -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(entity_type="PERSON", start=start, end=start + len(value), score=0.85)
    r.recognition_metadata = {"recognizer_name": recognizer}
    return r


def test_drops_sentence_initial_email():
    text = "Email me at vikas@example.com"
    out = filter_person_false_positives([_person(text, "Email")], text)
    assert out == []


def test_keeps_real_name_after_common_word():
    text = "Email Vikas today"
    # "Vikas" is a real name, not sentence-initial, not in the list -> kept.
    out = filter_person_false_positives([_person(text, "Vikas")], text)
    assert len(out) == 1


def test_keeps_common_word_mid_sentence():
    # Not sentence-initial -> not dropped (avoids removing legit spans).
    text = "Please Text is here"
    out = filter_person_false_positives([_person(text, "Text")], text)
    assert len(out) == 1


def test_keeps_multi_token_person():
    text = "Email Address is a person somehow"
    out = filter_person_false_positives([_person(text, "Email Address")], text)
    assert len(out) == 1  # multi-token -> never dropped


def test_drops_after_sentence_boundary():
    text = "Thanks. Email me back"
    out = filter_person_false_positives([_person(text, "Email")], text)
    assert out == []  # sentence-initial after '.'


def test_keeps_plausible_name_not_in_list():
    text = "Grace helped me today"
    out = filter_person_false_positives([_person(text, "Grace")], text)
    assert len(out) == 1  # "Grace" is a name, not in the denylist


def test_ignores_non_ner_person():
    text = "Email me now"
    r = _person(text, "Email", recognizer="CustomRecognizer")
    out = filter_person_false_positives([r], text)
    assert len(out) == 1  # only NER-sourced PERSON is filtered
