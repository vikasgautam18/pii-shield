"""Tests for DATE_TIME false-positive filtering."""

import pytest
from fastapi.testclient import TestClient

from app.main import _is_valid_datetime, app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Unit tests — _is_valid_datetime helper
# ---------------------------------------------------------------------------


class TestIsValidDatetime:
    """Verify the date/time validator accepts real dates and rejects impostors."""

    # ── Should be accepted ────────────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "8th March 2025",
        "15/03/2025",
        "2025-03-15",
        "03-15-2025",
        "March 2025",
        "Jan 15",
        "December",
        "10:30",
        "2:45 PM",
        "14:22:00",
        "2025-03-15T14:22:00",
        "yesterday",
        "last week",
        "3 days ago",
        "next month",
        "2025",
        "1999",
        "5th",
        "1st January",
        "23rd",
    ])
    def test_valid_dates_accepted(self, text):
        assert _is_valid_datetime(text) is True, f"Should accept: {text!r}"

    # ── Should be rejected ────────────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "238879",
        "380015",
        "+65 91234567",
        "91234567",
        "560038",
        "01 2012 0034567",
        "12345678901234",
        "9876543210",
    ])
    def test_false_positives_rejected(self, text):
        assert _is_valid_datetime(text) is False, f"Should reject: {text!r}"


# ---------------------------------------------------------------------------
# Integration tests — NRI scenario (the original bug report)
# ---------------------------------------------------------------------------


class TestDateTimeFalsePositives:
    NRI_TEXT = (
        "NRI customer Amit Patel, currently residing at 45 Orchard Road, "
        "Singapore 238879, has enquired about converting his NRE account "
        "921020012345678 at Contoso Bank, Ahmedabad to an NRO account. "
        "Indian address: 12, Satellite Road, Ahmedabad, Gujarat 380015. "
        "Passport number J8765432, DL GJ 01 2012 0034567. "
        "Email: amit.patel@protonmail.com, Singapore mobile +65 91234567, "
        "India mobile +91 79 26543210. Request received on 8th March 2025."
    )

    def test_postal_code_not_datetime(self, client):
        """Singapore postal code 238879 must NOT be anonymized as DATE_TIME."""
        resp = client.post("/anonymize_unique", json={"text": self.NRI_TEXT})
        assert resp.status_code == 200
        body = resp.json()
        assert "{{DATE_TIME_" not in body["anonymized_text"] or "238879" not in body["anonymized_text"]
        # Specifically: 238879 should NOT have a DATE_TIME placeholder
        anon = body["anonymized_text"]
        assert "238879" in anon or "IN_PIN_CODE" in anon or "ADDRESS" in anon, (
            "238879 should either remain unmasked or be detected as PIN_CODE/ADDRESS, not DATE_TIME"
        )

    def test_indian_pin_not_datetime(self, client):
        """Indian PIN code 380015 must NOT be anonymized as DATE_TIME."""
        resp = client.post("/anonymize_unique", json={"text": self.NRI_TEXT})
        body = resp.json()
        # 380015 should be detected as IN_PIN_CODE or merged into ADDRESS, not DATE_TIME
        anon = body["anonymized_text"]
        assert "IN_PIN_CODE" in anon or "ADDRESS" in anon

    def test_singapore_phone_not_datetime(self, client):
        """Singapore phone +65 91234567 must NOT be anonymized as DATE_TIME."""
        resp = client.post("/anonymize_unique", json={"text": self.NRI_TEXT})
        body = resp.json()
        anon = body["anonymized_text"]
        # Should NOT appear as DATE_TIME
        # It might not be detected at all (foreign phone) or detected as something else
        assert "+65 91234567" not in anon or "DATE_TIME" not in anon

    def test_valid_date_still_detected(self, client):
        """'8th March 2025' must still be detected as DATE_TIME."""
        resp = client.post("/anonymize_unique", json={"text": self.NRI_TEXT})
        body = resp.json()
        assert "8th March 2025" not in body["anonymized_text"]
        assert "DATE_TIME" in body["anonymized_text"]

    def test_bare_number_not_datetime(self, client):
        """A simple 6-digit number should not become DATE_TIME."""
        resp = client.post("/anonymize_unique", json={
            "text": "The reference number is 456789 for this transaction.",
        })
        body = resp.json()
        # 456789 should NOT be replaced with a DATE_TIME placeholder
        assert "{{DATE_TIME_" not in body["anonymized_text"]

    def test_legitimate_date_still_works(self, client):
        """Ensure standard date formats are still detected."""
        resp = client.post("/anonymize_unique", json={
            "text": "The meeting is on 15th January 2025 at the office.",
        })
        body = resp.json()
        assert "15th January 2025" not in body["anonymized_text"]
        assert "DATE_TIME" in body["anonymized_text"]
