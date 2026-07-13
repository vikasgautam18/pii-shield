"""Tests for merge_adjacent_person_tokens — PERSON span recall for names."""

from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import merge_adjacent_person_tokens


def _person(text: str, value: str, recognizer: str = "SpacyRecognizer") -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(entity_type="PERSON", start=start, end=start + len(value), score=0.85)
    r.recognition_metadata = {"recognizer_name": recognizer}
    return r


def _span(text, results):
    r = results[0]
    return text[r.start:r.end]


def test_extends_to_missed_first_name_odd_caps():
    text = "My name is VIkas Gautam"
    out = merge_adjacent_person_tokens([_person(text, "Gautam")], text)
    assert _span(text, out) == "VIkas Gautam"


def test_extends_three_word_name():
    text = "I met Rajesh Kumar Sharma there"
    out = merge_adjacent_person_tokens([_person(text, "Sharma")], text)
    assert _span(text, out) == "Rajesh Kumar Sharma"


def test_does_not_absorb_sentence_initial_word():
    text = "Hello Vikas"
    out = merge_adjacent_person_tokens([_person(text, "Vikas")], text)
    assert _span(text, out) == "Vikas"


def test_does_not_absorb_pronoun_or_title():
    text = "I spoke to Dr Gautam"
    out = merge_adjacent_person_tokens([_person(text, "Gautam")], text)
    # "Dr" is a title in the stop set -> not absorbed.
    assert _span(text, out) == "Gautam"


def test_stops_at_lowercase_token():
    text = "My name is Gautam"
    out = merge_adjacent_person_tokens([_person(text, "Gautam")], text)
    assert _span(text, out) == "Gautam"  # "is" is lowercase -> stop


def test_does_not_merge_across_comma():
    text = "Regards, Gautam"
    out = merge_adjacent_person_tokens([_person(text, "Gautam")], text)
    assert _span(text, out) == "Gautam"


def test_caps_at_two_added_tokens():
    text = "Alpha Bravo Charlie Delta"
    out = merge_adjacent_person_tokens([_person(text, "Delta")], text)
    # At most two tokens added -> "Bravo Charlie Delta", not "Alpha ...".
    assert _span(text, out) == "Bravo Charlie Delta"


def test_ignores_non_ner_person():
    text = "My name is VIkas Gautam"
    r = _person(text, "Gautam", recognizer="CustomRecognizer")
    out = merge_adjacent_person_tokens([r], text)
    assert _span(text, out) == "Gautam"  # only NER-sourced PERSON is extended


def test_whole_name_unchanged():
    text = "Contact Vikas Gautam today"
    out = merge_adjacent_person_tokens([_person(text, "Vikas Gautam")], text)
    assert _span(text, out) == "Vikas Gautam"
