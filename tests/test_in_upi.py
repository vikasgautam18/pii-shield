"""Tests for Indian UPI ID recognizer."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.recognizers.in_upi import InUpiIdRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestUpiIdRecognizerPatterns:
    """Verify the recognizer detects UPI IDs in isolation."""

    recognizer = InUpiIdRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["IN_UPI_ID"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_standard_ybl(self):
        results = self._match("Pay to amit@ybl via UPI")
        assert len(results) == 1
        assert results[0].entity_type == "IN_UPI_ID"
        assert results[0].score >= 0.7

    def test_google_pay_oksbi(self):
        results = self._match("UPI ID: rajesh.kumar@oksbi")
        assert len(results) == 1

    def test_google_pay_okicici(self):
        results = self._match("Transfer to 9876543210@okicici")
        assert len(results) == 1

    def test_paytm_handle(self):
        results = self._match("Paytm UPI: merchant@paytm")
        assert len(results) == 1

    def test_sbi_handle(self):
        results = self._match("Pay to account@sbi")
        assert len(results) == 1

    def test_numeric_username(self):
        results = self._match("Send to 9876543210@upi")
        assert len(results) == 1

    def test_dotted_username(self):
        results = self._match("Pay ajay.sharma@hdfcbank")
        assert len(results) == 1

    def test_multiple_upi_ids(self):
        results = self._match("Pay amit@ybl or rajesh@oksbi")
        assert len(results) == 2

    def test_various_handles(self):
        """Test a spread of different handle types."""
        handles = ["ybl", "axl", "ibl", "paytm", "icici", "kotak", "airtel", "freecharge"]
        for handle in handles:
            results = self._match(f"user@{handle}")
            assert len(results) == 1, f"Failed for handle: @{handle}"

    def test_context_boosts_score(self):
        # Without context
        no_ctx = self._match("user123@ybl")
        # With context
        with_ctx = self._match("UPI payment to user123@ybl")
        assert len(no_ctx) == 1
        assert len(with_ctx) == 1
        assert with_ctx[0].score >= no_ctx[0].score

    # ── Negative cases ────────────────────────────────────────────────────

    def test_email_not_matched(self):
        """Regular email addresses should NOT be detected as UPI IDs."""
        results = self._match("Contact user@gmail.com for info")
        assert len(results) == 0

    def test_email_with_bank_domain_not_matched(self):
        """Email with bank-like domain (has TLD) should NOT match."""
        results = self._match("Email: support@sbi.co.in")
        assert len(results) == 0

    def test_email_hdfcbank_domain(self):
        results = self._match("Write to help@hdfcbank.com")
        assert len(results) == 0

    def test_unknown_handle_not_matched(self):
        """Handles not in the NPCI list should not match."""
        results = self._match("user@randombank")
        assert len(results) == 0

    def test_empty_text(self):
        assert self._match("") == []

    def test_no_pii(self):
        assert self._match("Nothing sensitive here.") == []

    def test_single_char_username_not_matched(self):
        """Username must be at least 1 char (regex requires [a-zA-Z0-9] start)."""
        results = self._match("@ybl alone")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Integration tests — UPI ID detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestUpiIdAnonymize:
    def test_upi_detected_and_anonymized(self, client):
        resp = client.post("/anonymize_unique", json={
            "text": "Pay to amit@ybl via UPI",
            "language": "en",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "amit@ybl" not in body["anonymized_text"]
        assert "IN_UPI_ID" in body["anonymized_text"]

    def test_upi_google_pay_anonymized(self, client):
        resp = client.post("/anonymize_unique", json={
            "text": "Send payment to rajesh.kumar@oksbi please",
            "language": "en",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "rajesh.kumar@oksbi" not in body["anonymized_text"]

    def test_round_trip(self, client):
        original = "UPI payment to rajesh@oksbi please."
        anon = client.post("/anonymize_unique", json={"text": original}).json()
        assert "rajesh@oksbi" not in anon["anonymized_text"]

        deanon = client.post("/deanonymize", json={
            "id": anon["id"], "text": anon["anonymized_text"],
        }).json()
        assert "rajesh@oksbi" in deanon["text"]

    def test_upi_in_entity_type_allow_list(self, client):
        """IN_UPI_ID in entity-type allow-list should be excluded."""
        resp = client.post("/anonymize_unique", json={
            "text": "Pay to amit@ybl via UPI",
            "entity_type_allow_list": ["IN_UPI_ID"],
        })
        assert resp.status_code == 200
        assert "amit@ybl" in resp.json()["anonymized_text"]

    def test_email_not_confused_with_upi(self, client):
        """Emails should still be detected as EMAIL_ADDRESS, not IN_UPI_ID."""
        resp = client.post("/anonymize_unique", json={
            "text": "Contact user@gmail.com for details",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "user@gmail.com" not in body["anonymized_text"]
        # Should be anonymized as EMAIL_ADDRESS, not IN_UPI_ID
        assert "IN_UPI_ID" not in body["anonymized_text"]
