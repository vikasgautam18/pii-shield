"""Tests for Indian PRAN (Permanent Retirement Account Number) recognizer."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from pii_shield.recognizers.in_pran import InPranRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestPranRecognizerPatterns:
    """Verify the recognizer detects PRAN numbers in isolation."""

    recognizer = InPranRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["IN_PRAN"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_standard_12_digit(self):
        results = self._match("PRAN 110034567890")
        assert len(results) == 1
        assert results[0].entity_type == "IN_PRAN"
        assert results[0].score >= 0.1

    def test_starting_with_zero(self):
        """PRAN can start with 0 (unlike Aadhaar which requires 2-9)."""
        results = self._match("NPS PRAN 012345678901")
        assert len(results) == 1

    def test_starting_with_one(self):
        """PRAN can start with 1 (unlike Aadhaar which requires 2-9)."""
        results = self._match("Pension PRAN 198765432109")
        assert len(results) == 1

    def test_multiple_pran_numbers(self):
        text = "PRAN 110034567890, another PRAN 220045678901."
        results = self._match(text)
        assert len(results) == 2

    # ── Negative cases ────────────────────────────────────────────────────

    def test_too_few_digits_11(self):
        results = self._match("PRAN 11003456789")
        assert len(results) == 0

    def test_too_many_digits_13(self):
        results = self._match("PRAN 1100345678901")
        assert len(results) == 0

    def test_empty_text(self):
        results = self._match("")
        assert len(results) == 0

    def test_no_pii(self):
        results = self._match("Nothing sensitive here.")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Integration tests — PRAN detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestPranAnonymize:
    def test_pran_with_pension_context(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "NPS subscriber with PRAN 110034567890 has a Tier I pension account managed by PFRDA.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_PRAN_" in body["anonymized_text"]
        assert "110034567890" not in body["anonymized_text"]

    def test_pran_starting_with_zero(self, client):
        """PRAN starting with 0 cannot be Aadhaar — should detect as IN_PRAN."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "The employee's NPS PRAN is 012345678901 for the retirement pension fund.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_PRAN_" in body["anonymized_text"]
        assert "012345678901" not in body["anonymized_text"]

    def test_pran_vs_aadhaar_disambiguation(self, client):
        """With Aadhaar context, a 12-digit number should be IN_AADHAAR, not IN_PRAN."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Customer Aadhaar number is 987654321098 as per UIDAI records.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_AADHAAR_" in body["anonymized_text"]
        assert "{{IN_PRAN_" not in body["anonymized_text"]

    def test_round_trip_pran(self, client):
        original = "NPS subscriber PRAN 110034567890 linked to pension fund."
        anon_resp = client.post(
            "/anonymize_unique",
            json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()
        assert "110034567890" not in anon_body["anonymized_text"]

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        assert "110034567890" in deanon_resp.json()["text"]
