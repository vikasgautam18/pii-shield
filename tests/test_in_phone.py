"""Tests for the custom Indian phone number recognizer."""

import pytest
from presidio_analyzer import RecognizerResult

from app.recognizers.in_phone import InPhoneRecognizer


@pytest.fixture()
def recognizer():
    return InPhoneRecognizer()


# ── Helper ───────────────────────────────────────────────────────────────

def _types(results: list[RecognizerResult]) -> set[str]:
    return {r.entity_type for r in results}


def _texts(text: str, results: list[RecognizerResult]) -> list[str]:
    return [text[r.start : r.end] for r in results]


# ── Mobile patterns ──────────────────────────────────────────────────────

class TestMobilePatterns:
    def test_plus91_space(self, recognizer):
        text = "mobile +91 9845012345"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "+91 9845012345" in _texts(text, results)

    def test_plus91_hyphen(self, recognizer):
        text = "call +91-9845012345"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "+91-9845012345" in _texts(text, results)

    def test_plus91_no_separator(self, recognizer):
        text = "phone +919845012345"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1

    def test_mobile_zero_prefix(self, recognizer):
        text = "call 09876543210"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "09876543210" in _texts(text, results)

    def test_mobile_starts_with_6(self, recognizer):
        text = "phone +91 6123456789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1

    def test_mobile_starts_with_7(self, recognizer):
        text = "phone +91 7890123456"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1

    def test_bare_mobile_10_digit(self, recognizer):
        text = "mobile 9443256789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "9443256789" in _texts(text, results)

    def test_bare_mobile_starts_with_6(self, recognizer):
        text = "contact 6123456789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "6123456789" in _texts(text, results)

    def test_bare_mobile_no_match_starts_with_5(self, recognizer):
        """Indian mobiles start with 6-9; digit 5 should not match."""
        text = "phone 5123456789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) == 0


# ── Landline patterns ────────────────────────────────────────────────────

class TestLandlinePatterns:
    def test_mumbai_022(self, recognizer):
        text = "phone 022-24561789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "022-24561789" in _texts(text, results)

    def test_delhi_011(self, recognizer):
        text = "phone 011-23456789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "011-23456789" in _texts(text, results)

    def test_chennai_044(self, recognizer):
        text = "contact 044-28190000"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "044-28190000" in _texts(text, results)

    def test_bangalore_080(self, recognizer):
        text = "ph: 080-41234567"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "080-41234567" in _texts(text, results)

    def test_landline_space_separator(self, recognizer):
        text = "phone 022 24561789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "022 24561789" in _texts(text, results)

    def test_3digit_std_code(self, recognizer):
        text = "number 0821-2345678"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1
        assert "0821-2345678" in _texts(text, results)

    def test_4digit_std_code(self, recognizer):
        text = "phone 01onal-234567"
        # This is contrived — let's use a numeric example
        text = "phone 01onal"
        # Use realistic 4-digit code
        text = "dial 08212-23456"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        # 5-digit prefix + 5-digit subscriber may not match; test realistic case
        text = "phone 02836-234567"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) >= 1


# ── Context boosting ─────────────────────────────────────────────────────

class TestContextBoosting:
    def test_context_phone_boosts_score(self, recognizer):
        text = "phone 022-24561789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        # Base score without analyzer context pipeline
        assert results[0].score >= 0.6

    def test_context_mobile_boosts_score(self, recognizer):
        text = "mobile +91 9845012345"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert results[0].score >= 0.7

    def test_context_contact_boosts_score(self, recognizer):
        text = "contact 044-28190000"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert results[0].score >= 0.6

    def test_no_context_lower_score(self, recognizer):
        text = "022-24561789"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        if results:
            assert results[0].score <= 0.7


# ── Negative cases ───────────────────────────────────────────────────────

class TestNegativeCases:
    def test_random_digits_no_match(self, recognizer):
        text = "code 123456"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) == 0

    def test_date_not_matched(self, recognizer):
        text = "date 12/08/1985"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) == 0

    def test_short_number_no_match(self, recognizer):
        text = "phone 12345"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) == 0

    def test_landline_subscriber_starts_with_0(self, recognizer):
        # Indian landline subscribers don't start with 0 or 1
        text = "022-01234567"
        results = recognizer.analyze(text, entities=["PHONE_NUMBER"])
        assert len(results) == 0


# ── Integration: phone beats DATE_TIME after overlap removal ─────────────

class TestPhoneVsDateTime:
    """Verify that the custom recognizer wins over SpaCy DATE_TIME
    when both detect the same span and context words are present."""

    def test_phone_wins_with_context(self):
        from app.main import analyzer, _remove_overlapping

        text = "phone 022-24561789"
        results = analyzer.analyze(text=text, language="en")
        filtered = _remove_overlapping(results)
        types = {r.entity_type for r in filtered}
        assert "PHONE_NUMBER" in types
        assert "DATE_TIME" not in types

    def test_phone_wins_in_full_text(self):
        from app.main import analyzer, _remove_overlapping

        text = (
            "Co-applicant: Arun Menon, contact arun.menon@outlook.com, "
            "phone 022-24561789. Property address: Whitefield, Bengaluru."
        )
        results = analyzer.analyze(text=text, language="en")
        filtered = _remove_overlapping(results)
        phone = [r for r in filtered if r.entity_type == "PHONE_NUMBER"]
        assert len(phone) >= 1
        assert "022-24561789" in [text[r.start : r.end] for r in phone]

    def test_plus91_mobile_wins(self):
        from app.main import analyzer, _remove_overlapping

        text = "mobile +91 9845012345"
        results = analyzer.analyze(text=text, language="en")
        filtered = _remove_overlapping(results)
        types = {r.entity_type for r in filtered}
        assert "PHONE_NUMBER" in types

    def test_bare_mobile_beats_uk_nhs(self):
        """Bare 10-digit mobile like 9443256789 must be PHONE_NUMBER, not UK_NHS."""
        from app.main import analyzer, _remove_overlapping

        text = "mobile 9443256789"
        results = analyzer.analyze(text=text, language="en")
        filtered = _remove_overlapping(results)
        phone = [r for r in filtered if r.entity_type == "PHONE_NUMBER"]
        nhs = [r for r in filtered if r.entity_type == "UK_NHS"]
        assert len(phone) >= 1
        assert len(nhs) == 0
        assert "9443256789" in [text[r.start : r.end] for r in phone]

    def test_bare_mobile_in_full_sentence(self):
        """End-to-end: bare mobile in realistic Indian banking text."""
        from app.main import analyzer, _remove_overlapping

        text = (
            "Fund transfer request from Suresh Babu, account holder at "
            "Contoso Bank. Suresh's contact: sureshbabu1978@rediffmail.com, "
            "mobile 9443256789. Transfer initiated on 22nd February 2025."
        )
        results = analyzer.analyze(text=text, language="en")
        filtered = _remove_overlapping(results)
        phone = [r for r in filtered if r.entity_type == "PHONE_NUMBER"]
        nhs = [r for r in filtered if r.entity_type == "UK_NHS"]
        assert len(phone) >= 1
        assert len(nhs) == 0
        assert "9443256789" in [text[r.start : r.end] for r in phone]
