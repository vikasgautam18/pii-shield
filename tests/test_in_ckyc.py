"""Tests for Indian CKYC (Central KYC Identifier) recognizer."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from pii_shield.recognizers.in_ckyc import InCkycRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestCkycRecognizerPatterns:
    """Verify the recognizer detects CKYC numbers in isolation."""

    recognizer = InCkycRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["IN_CKYC"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_standard_14_digit(self):
        results = self._match("CKYC number 10002345678901 verified")
        assert len(results) == 1
        assert results[0].entity_type == "IN_CKYC"
        assert results[0].score >= 0.3

    def test_prefix_l_simplified(self):
        results = self._match("CKYC L10002345678901 for simplified account")
        assert len(results) == 1
        assert results[0].score >= 0.5

    def test_prefix_s_small_account(self):
        results = self._match("CKYC S98765432109876 small account")
        assert len(results) == 1
        assert results[0].score >= 0.5

    def test_prefix_o_otp_ekyc(self):
        results = self._match("CKYC O12345678901234 OTP eKYC")
        assert len(results) == 1
        assert results[0].score >= 0.5

    def test_multiple_ckyc_numbers(self):
        text = "First: 10002345678901 second: 99887766554433."
        results = self._match(text)
        assert len(results) == 2

    # ── Negative cases ────────────────────────────────────────────────────

    def test_too_few_digits_13(self):
        results = self._match("CKYC 1000234567890")
        assert len(results) == 0

    def test_too_many_digits_15(self):
        results = self._match("CKYC 100023456789012")
        assert len(results) == 0

    def test_invalid_prefix(self):
        """Prefixes other than L/S/O should not match the prefixed pattern."""
        results = self._match("CKYC X10002345678901")
        # Should NOT match the prefixed pattern (X is not L/S/O)
        # but the 14-digit portion after X may match the standard pattern
        for r in results:
            assert r.score < 0.5  # not the prefixed pattern score

    def test_empty_text(self):
        results = self._match("")
        assert len(results) == 0

    def test_no_pii(self):
        results = self._match("Nothing sensitive here.")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Integration tests — CKYC detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestCkycAnonymize:
    def test_ckyc_standard_detected(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Customer CKYC number 10002345678901 has been verified by CERSAI.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_CKYC_" in body["anonymized_text"]
        assert "10002345678901" not in body["anonymized_text"]

    def test_ckyc_prefixed_detected(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Simplified CKYC account L10002345678901 registered with central KYC registry.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_CKYC_" in body["anonymized_text"]
        assert "L10002345678901" not in body["anonymized_text"]

    def test_round_trip_ckyc(self, client):
        original = "Customer CKYC number is 10002345678901 as per CERSAI records."
        anon_resp = client.post(
            "/anonymize_unique",
            json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()
        assert "10002345678901" not in anon_body["anonymized_text"]

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        assert "10002345678901" in deanon_resp.json()["text"]
