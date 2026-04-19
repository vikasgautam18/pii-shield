"""Tests for Indian APAAR ID (One Nation, One Student ID) recognizer."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from pii_shield.recognizers.in_apaar import InApaarRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestApaarRecognizerPatterns:
    """Verify the recognizer detects APAAR IDs in isolation."""

    recognizer = InApaarRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["IN_APAAR"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_standard_12_digit(self):
        results = self._match("APAAR ID 234567890123")
        assert len(results) == 1
        assert results[0].entity_type == "IN_APAAR"
        assert results[0].score >= 0.1

    def test_starting_with_zero(self):
        results = self._match("Student APAAR 012345678901")
        assert len(results) == 1

    def test_multiple_apaar_ids(self):
        text = "APAAR 234567890123 and APAAR 345678901234."
        results = self._match(text)
        assert len(results) == 2

    # ── Negative cases ────────────────────────────────────────────────────

    def test_too_few_digits_11(self):
        results = self._match("APAAR 23456789012")
        assert len(results) == 0

    def test_too_many_digits_13(self):
        results = self._match("APAAR 2345678901234")
        assert len(results) == 0

    def test_empty_text(self):
        results = self._match("")
        assert len(results) == 0

    def test_no_pii(self):
        results = self._match("Nothing sensitive here.")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Integration tests — APAAR detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestApaarAnonymize:
    def test_apaar_with_student_context(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Student APAAR ID 234567890123 registered in Academic Bank of Credits via DigiLocker.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_APAAR_" in body["anonymized_text"]
        assert "234567890123" not in body["anonymized_text"]

    def test_apaar_with_education_context(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "The student's APAAR enrollment number 345678901234 has been linked to the university education portal.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_APAAR_" in body["anonymized_text"]
        assert "345678901234" not in body["anonymized_text"]

    def test_apaar_vs_aadhaar_disambiguation(self, client):
        """With Aadhaar context, a 12-digit number should be IN_AADHAAR, not IN_APAAR."""
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
        assert "{{IN_APAAR_" not in body["anonymized_text"]

    def test_round_trip_apaar(self, client):
        original = "Student APAAR ID 234567890123 registered in Academic Bank of Credits."
        anon_resp = client.post(
            "/anonymize_unique",
            json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()
        assert "234567890123" not in anon_body["anonymized_text"]

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        assert "234567890123" in deanon_resp.json()["text"]
