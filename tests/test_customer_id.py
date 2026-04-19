"""Tests for banking Customer ID recognizer."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from pii_shield.recognizers.customer_id import CustomerIdRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestCustomerIdRecognizerPatterns:
    """Verify the recognizer detects Customer IDs in isolation."""

    recognizer = CustomerIdRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["CUSTOMER_ID"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_standard_9_digit(self):
        results = self._match("Customer ID 103841234")
        assert len(results) == 1
        assert results[0].entity_type == "CUSTOMER_ID"
        assert results[0].score >= 0.9

    def test_with_custid_keyword(self):
        results = self._match("CustID 209876543 for net banking login")
        assert len(results) == 1
        assert results[0].score >= 0.9

    def test_with_cif_keyword(self):
        results = self._match("CIF 313456790 flagged for annual review")
        assert len(results) == 1
        assert results[0].score >= 0.9

    def test_with_net_banking_keyword(self):
        results = self._match("Log in to net banking using 412309876")
        assert len(results) == 1
        assert results[0].score >= 0.9

    def test_with_internet_banking_keyword(self):
        results = self._match("Internet banking login ID is 508734219")
        assert len(results) == 1
        assert results[0].score >= 0.9

    def test_with_mobile_banking_keyword(self):
        results = self._match("Your mobile banking customer ID is 617823490")
        assert len(results) == 1
        assert results[0].score >= 0.9

    def test_multiple_customer_ids(self):
        text = "Customer ID 103841234 and Customer ID 209876543."
        results = self._match(text)
        assert len(results) == 2

    def test_without_context_low_score(self):
        """A bare 9-digit number without context should have low score."""
        results = self._match("Reference 456789012 for your order")
        assert len(results) == 1
        assert results[0].score <= 0.15  # base score, no boost

    # ── Negative cases ────────────────────────────────────────────────────

    def test_too_few_digits_8(self):
        results = self._match("Customer ID 12345678")
        assert len(results) == 0

    def test_too_many_digits_10(self):
        results = self._match("Customer ID 1234567890")
        assert len(results) == 0

    def test_empty_text(self):
        results = self._match("")
        assert len(results) == 0

    def test_no_pii(self):
        results = self._match("Nothing sensitive here.")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Integration tests — Customer ID detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestCustomerIdAnonymize:
    def test_customer_id_with_login_context(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Please log in to net banking using your Customer ID 103841234 and the OTP sent to your registered mobile.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "103841234" not in body["anonymized_text"]

    def test_customer_id_with_cif_context(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "CIF 313456790 flagged for annual review — all accounts under this Customer ID require re-verification.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "313456790" not in body["anonymized_text"]

    def test_customer_id_welcome_kit(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Welcome kit for Customer ID 617823490: Your new savings account 917020048230123 is now active.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "617823490" not in body["anonymized_text"]

    def test_bare_9_digit_not_customer_id(self, client):
        """A bare 9-digit number without customer context should NOT be CUSTOMER_ID."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "The order reference number is 456789012 for your recent purchase.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" not in body["anonymized_text"]

    def test_round_trip_customer_id(self, client):
        original = "Customer ID 103841234 linked to net banking account."
        anon_resp = client.post(
            "/anonymize_unique",
            json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()
        assert "103841234" not in anon_body["anonymized_text"]

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        assert "103841234" in deanon_resp.json()["text"]


# ---------------------------------------------------------------------------
# Combination tests — Customer ID alongside other entity types
# ---------------------------------------------------------------------------


class TestCustomerIdCombinations:
    """Verify Customer ID is correctly detected alongside other entities."""

    def test_customer_id_with_account_number(self, client):
        """Customer ID and bank account number in same sentence."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Customer ID 435678912 is linked to savings account 917020048230123 at our bank.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "{{IN_BANK_ACCOUNT_" in body["anonymized_text"]
        assert "435678912" not in body["anonymized_text"]
        assert "917020048230123" not in body["anonymized_text"]

    def test_customer_id_with_aadhaar_and_pan(self, client):
        """KYC update scenario: Customer ID + Aadhaar + PAN."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "KYC update pending for Customer ID 435678912. Please visit your nearest branch with original Aadhaar 9876 5432 1098 and PAN ABCPS7234F.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "{{IN_AADHAAR_" in body["anonymized_text"]
        assert "{{IN_PAN_" in body["anonymized_text"]
        assert "435678912" not in body["anonymized_text"]

    def test_customer_id_with_phone_and_email(self, client):
        """Customer profile with ID + phone + email."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Customer ID 209876543 registered with mobile +91 9845012345 and email rajesh.sharma@gmail.com for net banking alerts.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "{{PHONE_NUMBER_" in body["anonymized_text"]
        assert "{{EMAIL_ADDRESS_" in body["anonymized_text"]
        assert "209876543" not in body["anonymized_text"]

    def test_customer_id_with_ifsc_and_account(self, client):
        """Fund transfer instruction with Customer ID + IFSC + account."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Customer ID 768901245 has requested a NEFT transfer, debiting account 917020043567890 with IFSC CONT0005678.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "{{IN_BANK_ACCOUNT_" in body["anonymized_text"]
        assert "{{IN_IFSC_" in body["anonymized_text"]
        assert "768901245" not in body["anonymized_text"]

    def test_customer_id_with_person_and_address(self, client):
        """Account statement with Customer ID + person + address."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Account statement for Mr. Rajesh Kumar, Customer ID 103841234, residential address: Flat 302, Prestige Towers, Indiranagar, Bangalore 560038.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "{{PERSON_" in body["anonymized_text"]
        assert "103841234" not in body["anonymized_text"]

    def test_customer_id_with_dl_and_pan(self, client):
        """KYC verification with Customer ID + driving license + PAN."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Verify KYC for Customer ID 508734219 — DL: MH 14 2019 0012345, PAN: BKRPD3456J. Update mobile banking profile.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "{{IN_DRIVING_LICENSE_" in body["anonymized_text"]
        assert "{{IN_PAN_" in body["anonymized_text"]
        assert "508734219" not in body["anonymized_text"]

    def test_two_customer_ids_with_account(self, client):
        """Joint account linking two Customer IDs to one account."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Joint savings account 917020048230123 linked to Customer ID 103841234 (primary) and Customer ID 209876543 (secondary).",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        anon = body["anonymized_text"]
        assert anon.count("{{CUSTOMER_ID_") == 2
        assert "{{IN_BANK_ACCOUNT_" in anon
        assert "103841234" not in anon
        assert "209876543" not in anon

    def test_customer_id_with_upi_and_phone(self, client):
        """UPI linking scenario with Customer ID + UPI + phone."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Link UPI ID rajesh@okaxis to Customer ID 617823490. Registered mobile: +91 9845012345 for internet banking.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "{{IN_UPI_ID_" in body["anonymized_text"]
        assert "{{PHONE_NUMBER_" in body["anonymized_text"]
        assert "617823490" not in body["anonymized_text"]

    def test_customer_id_with_credit_card(self, client):
        """Card dispatch with Customer ID + credit card number."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Credit card 4532 0151 2345 6789 dispatched to Customer ID 412309876. Activate via mobile banking app.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{CUSTOMER_ID_" in body["anonymized_text"]
        assert "{{CREDIT_CARD_" in body["anonymized_text"]
        assert "412309876" not in body["anonymized_text"]

    def test_combination_round_trip(self, client):
        """Round-trip: Customer ID + account + phone all survive deanonymize."""
        original = "Transfer initiated by Customer ID 435678912 from account 917020048230123, contact +91 9845012345 for net banking confirmation."
        anon_resp = client.post(
            "/anonymize_unique",
            json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()
        assert "{{CUSTOMER_ID_" in anon_body["anonymized_text"]
        assert "435678912" not in anon_body["anonymized_text"]
        assert "917020048230123" not in anon_body["anonymized_text"]

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        restored = deanon_resp.json()["text"]
        assert "435678912" in restored
        assert "917020048230123" in restored
        assert "9845012345" in restored
