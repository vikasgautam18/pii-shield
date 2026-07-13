"""Tests for the custom Indian PAN recognizer (InPanImprovedRecognizer)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from pii_shield.recognizers.in_pan import InPanImprovedRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestPanRecognizerPatterns:
    recognizer = InPanImprovedRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["IN_PAN"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_valid_pan_individual(self):
        results = self._match("PAN: ABCPS7234F")
        assert len(results) == 1
        assert results[0].entity_type == "IN_PAN"
        assert results[0].score >= 0.85

    def test_all_holder_types(self):
        # 4th char = holder type; all valid codes should score high.
        for ht in "ABCFGHJLPT":
            pan = f"ABC{ht}S1234Z"
            results = self._match(f"PAN {pan}")
            assert len(results) == 1, f"holder type {ht} not matched"
            assert results[0].score >= 0.85

    def test_generic_lowercase_lower_score(self):
        # Lowercase / non-validated still matches the generic pattern.
        results = self._match("pan is abcps7234f")
        assert len(results) == 1
        assert results[0].score < 0.85

    def test_multiple_pans(self):
        results = self._match("First ABCPS7234F then XYZAB4321C")
        assert len(results) == 2

    # ── Negative cases ────────────────────────────────────────────────────

    def test_too_few_letters(self):
        assert self._match("ABCD7234F") == []

    def test_too_many_digits(self):
        assert self._match("ABCPS72345F") == []

    def test_missing_trailing_letter(self):
        assert self._match("ABCPS7234") == []

    def test_no_pii(self):
        assert self._match("Nothing sensitive here.") == []

    def test_empty(self):
        assert self._match("") == []


# ---------------------------------------------------------------------------
# Integration tests — PAN detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestPanAnonymize:
    def test_pan_detected_and_masked(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={"text": "My PAN is ABCPS7234F for income tax.", "language": "en"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_PAN_" in body["anonymized_text"]
        assert "ABCPS7234F" not in body["anonymized_text"]

    def test_round_trip_pan(self, client):
        original = "Please note PAN ABCPS7234F on the tax form."
        anon = client.post("/anonymize_unique", json={"text": original}).json()
        assert "ABCPS7234F" not in anon["anonymized_text"]
        deanon = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"]},
        ).json()
        assert "ABCPS7234F" in deanon["text"]

    def test_builtin_pan_recognizer_disabled(self):
        """The predefined InPanRecognizer must be replaced, not duplicated."""
        from app.main import engine

        names = {type(r).__name__ for r in engine._analyzer.registry.recognizers}
        assert "InPanImprovedRecognizer" in names
        assert "InPanRecognizer" not in names
