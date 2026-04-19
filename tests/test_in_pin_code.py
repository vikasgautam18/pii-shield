import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.recognizers.in_pin_code import InPinCodeRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestPinCodeRecognizerPatterns:
    """Verify the recognizer detects Indian PIN codes in isolation."""

    recognizer = InPinCodeRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["IN_PIN_CODE"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_standard_six_digit(self):
        results = self._match("PIN code 560038")
        assert len(results) == 1
        assert results[0].entity_type == "IN_PIN_CODE"

    def test_space_separated(self):
        results = self._match("pincode 560 038")
        assert len(results) == 1

    def test_various_regions(self):
        """First digit 1-8 covers all postal regions."""
        pin_codes = ["110001", "201301", "380015", "400001", "560038", "600001", "700001", "800001"]
        for pin in pin_codes:
            results = self._match(f"PIN code {pin}")
            assert len(results) == 1, f"Failed for PIN code {pin}"

    def test_with_context_keyword_pin(self):
        results = self._match("PIN 560038")
        assert len(results) == 1

    def test_with_context_keyword_postal(self):
        results = self._match("Postal code: 560038")
        assert len(results) == 1

    def test_with_context_keyword_zip(self):
        results = self._match("Zip code 560038")
        assert len(results) == 1

    def test_with_context_keyword_pincode(self):
        results = self._match("Pincode: 560038")
        assert len(results) == 1

    def test_multiple_pin_codes(self):
        text = "Origin pincode 560038 and destination pincode 400001."
        results = self._match(text)
        assert len(results) == 2

    # ── Negative cases ────────────────────────────────────────────────────

    def test_starts_with_zero(self):
        results = self._match("PIN 060038")
        assert len(results) == 0

    def test_starts_with_nine(self):
        results = self._match("PIN 960038")
        assert len(results) == 0

    def test_five_digits_too_short(self):
        results = self._match("PIN 56003")
        assert len(results) == 0

    def test_seven_digits_too_long(self):
        results = self._match("PIN 5600380")
        assert len(results) == 0

    def test_empty_text(self):
        results = self._match("")
        assert len(results) == 0

    def test_no_pii(self):
        results = self._match("Nothing sensitive here.")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Integration tests — PIN code detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestPinCodeAnonymize:
    def test_pin_code_detected_in_plain_text(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Residential address: Flat 302, Prestige Towers, Bangalore pincode 560038.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Pin code is detected — it may appear standalone or merged into ADDRESS
        assert "560038" not in body["anonymized_text"]
        anon = body["anonymized_text"]
        assert "{{IN_PIN_CODE_" in anon or "{{ADDRESS_" in anon

    def test_pin_code_detected_in_json_text(self, client):
        json_text = '{"name": "Rajesh", "address": {"city": "Bangalore", "pin": "560038"}}'
        resp = client.post(
            "/anonymize_unique",
            json={"text": json_text, "language": "en"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "560038" not in body["anonymized_text"]

    def test_round_trip_pin_code(self, client):
        original = "Delivery pincode is 400001."
        anon_resp = client.post(
            "/anonymize_unique",
            json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()
        assert "400001" not in anon_body["anonymized_text"]

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        assert "400001" in deanon_resp.json()["text"]

    def test_pin_code_space_separated_detected(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Office pincode is 560 038.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "560 038" not in body["anonymized_text"]


# ---------------------------------------------------------------------------
# Integration tests — PIN code beats DATE_TIME via overlap removal
# ---------------------------------------------------------------------------


class TestPinCodeVsDateTime:
    """Verify PIN code wins over SpaCy DATE_TIME when address context is present."""

    def test_pin_in_address_context(self):
        from app.main import analyzer, _remove_overlapping

        text = "Residence: B-45, Safdarjung Enclave, New Delhi 110029."
        results = analyzer.analyze(text=text, language="en")
        filtered = _remove_overlapping(results)
        pin = [r for r in filtered if r.entity_type == "IN_PIN_CODE"]
        dt = [r for r in filtered if r.entity_type == "DATE_TIME" and text[r.start:r.end] == "110029"]
        assert len(pin) >= 1
        assert len(dt) == 0
        assert "110029" in [text[r.start:r.end] for r in pin]

    def test_pin_in_full_complaint_text(self):
        from app.main import analyzer, _remove_overlapping

        text = (
            "Customer visited the Contoso Bank Saket branch, New Delhi on "
            "10 March 2025. Residence: B-45, Safdarjung Enclave, New Delhi 110029."
        )
        results = analyzer.analyze(text=text, language="en")
        filtered = _remove_overlapping(results)
        pin = [r for r in filtered if r.entity_type == "IN_PIN_CODE"]
        assert len(pin) >= 1
        assert "110029" in [text[r.start:r.end] for r in pin]
