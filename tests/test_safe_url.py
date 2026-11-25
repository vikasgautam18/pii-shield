"""The URL recognizer must not stall on long runs of dots, and must find the same URLs as before.

Presidio 2.2.362's URL patterns backtrack quadratically on dotted leader lines and OCR noise (about 2 s
for 1,000 dots, 9 s for 2,000).  SafeUrlRecognizer scans a same-length copy of the text in which those
dots are spaces.
"""

import time

import pytest
from presidio_analyzer.predefined_recognizers import UrlRecognizer

from pii_shield.recognizers import SafeUrlRecognizer
from pii_shield.recognizers.safe_url import neutralise_dot_runs

BUILT_IN = UrlRecognizer()
SAFE = SafeUrlRecognizer()


def _spans(recognizer, text):
    return sorted((r.start, r.end, r.score) for r in recognizer.analyze(text, entities=["URL"]))


class TestNeutralise:
    def test_keeps_every_offset(self):
        text = "Name " + "." * 300 + " www.example.com " + "a." * 200
        assert len(neutralise_dot_runs(text)) == len(text)

    def test_dot_runs_of_four_or_more_become_underscores(self):
        assert neutralise_dot_runs("Name ...... Rajesh") == "Name ______ Rajesh"
        assert neutralise_dot_runs("Wait... what") == "Wait... what"

    def test_dots_of_a_host_longer_than_any_host_name_become_underscores(self):
        chain = "a." * 200
        assert "." not in neutralise_dot_runs(chain)
        assert neutralise_dot_runs("www.example.com") == "www.example.com"

    def test_other_text_is_untouched(self):
        text = "Visit https://bank.example.co.in/login?next=/a.b or mail a.b@c.com. Thanks..."
        assert neutralise_dot_runs(text) == text


# One unbroken run of host-name characters (no "_" and no "-"), the shape of a long dotted token.
_SEGMENT = "abcdefghijklmnopqrstuvwxyz0123456789" * 4
_TOKEN = ".".join([_SEGMENT, _SEGMENT + _SEGMENT[:60], _SEGMENT])


class TestSameResultsAsPresidio:
    @pytest.mark.parametrize("text", [
        "Visit https://www.example.com/path?x=1 today",
        "see www.hdfcbank.com or bank.example.co.in.",
        "'https://secure.contoso.com/login' and \"api.fabrikam.net\"",
        "Mail rajesh.sharma@example.com, site example.org...",
        "No link here, only 12.5% and 3.14 and U.S.A.",
        "Terms ........ 12\nPrivacy ........ 14\nVisit www.example.com",
        # A long dotted token in the query or path stays inside the URL: nothing after it is left unmasked.
        "Open https://api.example.com/verify?token=" + _TOKEN + " now",
        "Open api.example.com/verify?token=" + _TOKEN + " now",
        "Doc: https://files.example.com/" + ".".join([_SEGMENT] * 4) + ".pdf end",
    ])
    def test_urls_found_are_identical(self, text):
        assert _spans(SAFE, text) == _spans(BUILT_IN, text)

    def test_offsets_point_into_the_original_text_after_a_leader_line(self):
        text = "Applicant ............................ www.example.com signed"
        (start, end, _), = _spans(SAFE, text)
        assert text[start:end] == "www.example.com"

    def test_a_url_with_a_dotted_token_is_masked_to_its_last_character(self):
        text = "Open https://api.example.com/verify?token=" + _TOKEN + " now"
        (start, end, _), = _spans(SAFE, text)
        assert text[start:end] == text[len("Open "):-len(" now")]


class TestNoStall:
    @pytest.mark.parametrize("text", [
        "Name " + "." * 20_000,
        "Page " + "." * 5_000 + " 12",
        "a." * 5_000,
        ("Signature ............... " + "." * 700 + "\n") * 20,
    ])
    def test_pathological_runs_are_scanned_quickly(self, text):
        start = time.perf_counter()
        SAFE.analyze(text, entities=["URL"])
        assert time.perf_counter() - start < 2.0


def test_the_engine_uses_the_safe_recognizer_under_the_built_in_name():
    from app.main import analyzer

    urls = [r for r in analyzer.registry.recognizers if "URL" in r.supported_entities]
    assert len(urls) == 1
    assert type(urls[0]) is SafeUrlRecognizer and urls[0].name == "UrlRecognizer"
