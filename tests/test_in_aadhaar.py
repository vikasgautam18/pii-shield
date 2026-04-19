import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.recognizers.in_aadhaar import InAadhaarImprovedRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestAadhaarRecognizerPatterns:
    """Verify the recognizer detects Aadhaar numbers in isolation."""

    recognizer = InAadhaarImprovedRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["IN_AADHAAR"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_space_separated(self):
        results = self._match("Aadhaar 9876 5432 1098")
        assert len(results) == 1
        assert results[0].entity_type == "IN_AADHAAR"
        assert results[0].score >= 0.85

    def test_hyphen_separated(self):
        results = self._match("Aadhaar 9876-5432-1098")
        assert len(results) == 1
        assert results[0].score >= 0.85

    def test_no_separator(self):
        results = self._match("UID 987654321098")
        assert len(results) == 1
        assert results[0].score >= 0.3

    def test_various_starting_digits(self):
        """First digit must be 2-9."""
        for d in range(2, 10):
            results = self._match(f"Aadhaar {d}876 5432 1098")
            assert len(results) == 1, f"Failed for starting digit {d}"

    def test_with_context_keyword_aadhaar(self):
        results = self._match("Aadhaar number is 9876 5432 1098")
        assert len(results) == 1
        assert results[0].score >= 0.85

    def test_with_context_keyword_uidai(self):
        results = self._match("UIDAI: 9876 5432 1098")
        assert len(results) == 1

    def test_multiple_aadhaar_numbers(self):
        text = "First: 9876 5432 1098, second: 2345 6789 0123."
        results = self._match(text)
        assert len(results) == 2

    # ── Negative cases ────────────────────────────────────────────────────

    def test_starts_with_zero(self):
        results = self._match("Aadhaar 0876 5432 1098")
        assert len(results) == 0

    def test_starts_with_one(self):
        results = self._match("Aadhaar 1876 5432 1098")
        assert len(results) == 0

    def test_too_few_digits(self):
        results = self._match("Aadhaar 9876 5432 109")
        assert len(results) == 0

    def test_too_many_digits(self):
        results = self._match("Aadhaar 9876 5432 10987")
        assert len(results) == 0

    def test_empty_text(self):
        results = self._match("")
        assert len(results) == 0

    def test_no_pii(self):
        results = self._match("Nothing sensitive here.")
        assert len(results) == 0

    def test_no_match_inside_credit_card_hyphens(self):
        """16-digit credit card (4-4-4-4) must not trigger Aadhaar."""
        results = self._match("The full card id is 1232-9876-5656-5678")
        assert len(results) == 0

    def test_no_match_inside_credit_card_spaces(self):
        """16-digit credit card (4-4-4-4) must not trigger Aadhaar."""
        results = self._match("The full card id is 1232 9876 5656 5678")
        assert len(results) == 0

    def test_no_match_inside_credit_card_spaces_mastercard(self):
        """Mastercard-style 16-digit number must not trigger Aadhaar."""
        results = self._match("My card 5214 6390 7842 5678 is blocked.")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Integration tests — Aadhaar detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestAadhaarAnonymize:
    def test_aadhaar_space_separated_detected(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Aadhaar 9876 5432 1098 belongs to the customer.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_AADHAAR_" in body["anonymized_text"]
        assert "9876 5432 1098" not in body["anonymized_text"]

    def test_aadhaar_hyphen_separated_detected(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Customer Aadhaar number is 9876-5432-1098 as per records.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_AADHAAR_" in body["anonymized_text"]
        assert "9876-5432-1098" not in body["anonymized_text"]

    def test_round_trip_aadhaar(self, client):
        original = "Customer Aadhaar is 9876 5432 1098."
        anon_resp = client.post(
            "/anonymize_unique",
            json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()
        assert "9876 5432 1098" not in anon_body["anonymized_text"]

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        assert "9876 5432 1098" in deanon_resp.json()["text"]
