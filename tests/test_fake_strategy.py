"""Tests for the 'fake' anonymization strategy.

Covers:
  - FakeDataOperator unit tests (format validation per entity type)
  - Integration tests via /anonymize_unique and /deanonymize
  - Config validation (API accepts 'fake', rejects invalid)
  - Per-app config via Redis
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.operator_config import operator_config_store


@pytest.fixture(autouse=True)
def _reset_config():
    """Reset operator config to defaults before each test."""
    operator_config_store.set_strategy("IN_DRIVING_LICENSE", "replace")
    operator_config_store.set_strategy("IN_AADHAAR", "replace")
    operator_config_store.set_strategy("IN_PAN", "replace")
    operator_config_store.set_strategy("PHONE_NUMBER", "replace")
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def _register(client, name="TestApp"):
    resp = client.post("/apps", json={"app_name": name})
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# Unit tests — FakeDataOperator generators
# ---------------------------------------------------------------------------


class TestFakeDataOperator:
    def test_fake_aadhaar_spaces(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("9876 5432 1098", {"entity_type": "IN_AADHAAR"})
        assert re.match(r"^[2-9]\d{3} \d{4} \d{4}$", result)

    def test_fake_aadhaar_hyphens(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("9876-5432-1098", {"entity_type": "IN_AADHAAR"})
        assert re.match(r"^[2-9]\d{3}-\d{4}-\d{4}$", result)

    def test_fake_aadhaar_no_separator(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("987654321098", {"entity_type": "IN_AADHAAR"})
        assert re.match(r"^[2-9]\d{11}$", result)

    def test_fake_pan_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("ABCDE1234F", {"entity_type": "IN_PAN"})
        assert re.match(r"^[A-Z]{5}\d{4}[A-Z]$", result)

    def test_fake_driving_license_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        # Space-separated input → space-separated output
        result = op.operate("MH 14 2019 0012345", {"entity_type": "IN_DRIVING_LICENSE"})
        assert re.match(r"^[A-Z]{2} \d{2} \d{4} \d{7}$", result)

    def test_fake_driving_license_hyphen_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        # Hyphen-separated input → hyphen-separated output
        result = op.operate("KA-09-2023-5554321", {"entity_type": "IN_DRIVING_LICENSE"})
        assert re.match(r"^[A-Z]{2}-\d{2}-\d{4}-\d{7}$", result)

    def test_fake_driving_license_compact_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        # No separator → no separator
        result = op.operate("MH1420190012345", {"entity_type": "IN_DRIVING_LICENSE"})
        assert re.match(r"^[A-Z]{2}\d{2}\d{4}\d{7}$", result)

    def test_fake_phone_plus91(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("+91 9876543210", {"entity_type": "PHONE_NUMBER"})
        assert result.startswith("+91 ")
        assert re.match(r"^\+91 [7-9]\d{9}$", result)

    def test_fake_phone_zero_prefix(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("022-12345678", {"entity_type": "PHONE_NUMBER"})
        assert result.startswith("0")
        assert len(result) >= 10

    def test_fake_upi_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("john.doe@ybl", {"entity_type": "IN_UPI_ID"})
        assert "@" in result
        parts = result.split("@")
        assert len(parts) == 2
        assert len(parts[0]) >= 5

    def test_fake_pin_code_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("110001", {"entity_type": "IN_PIN_CODE"})
        assert re.match(r"^[1-9]\d{5}$", result)

    def test_fake_credit_card_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("4111111111111111", {"entity_type": "CREDIT_CARD"})
        assert re.match(r"^\d{16}$", result)

    def test_fake_email_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("john@example.com", {"entity_type": "EMAIL_ADDRESS"})
        assert "@" in result
        assert "." in result.split("@")[1]

    def test_fake_person_produces_name(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("John Smith", {"entity_type": "PERSON"})
        assert " " in result
        assert len(result) > 3

    def test_fake_location_produces_city(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("Mumbai", {"entity_type": "LOCATION"})
        assert len(result) > 2

    def test_generic_fallback_preserves_format(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        # Unknown entity type triggers generic fallback
        result = op.operate("AB-123", {"entity_type": "UNKNOWN_TYPE"})
        assert len(result) == 6
        assert result[0].isupper()
        assert result[1].isupper()
        assert result[2] == "-"
        assert result[3].isdigit()
        assert result[4].isdigit()
        assert result[5].isdigit()

    def test_generic_fallback_no_entity_type(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        result = op.operate("abc 123", {})
        assert len(result) == 7
        assert result[3] == " "

    def test_different_calls_produce_different_values(self):
        from app.operators.fake_data import FakeDataOperator

        op = FakeDataOperator()
        results = {
            op.operate("9876 5432 1098", {"entity_type": "IN_AADHAAR"})
            for _ in range(20)
        }
        assert len(results) > 1, "Expected different fake values across calls"


# ---------------------------------------------------------------------------
# Integration tests — fake strategy via /anonymize_unique
# ---------------------------------------------------------------------------


class TestFakeAnonymize:
    def test_anonymize_fakes_dl_when_configured(self, client):
        operator_config_store.set_strategy("IN_DRIVING_LICENSE", "fake")
        dl_number = "MH 14 2019 0012345"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"My driving license is {dl_number}.", "language": "en"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Original should not appear in anonymized text
        assert dl_number not in body["anonymized_text"]
        # entity_mapping should contain the fake→original mapping
        assert len(body["entity_mapping"]) > 0
        original_values = list(body["entity_mapping"].values())
        assert dl_number in original_values
        # hash_mapping and encrypt_mapping should be empty for this entity
        for hv in body.get("hash_mapping", {}).values():
            assert hv != dl_number
        for ev in body.get("encrypt_mapping", {}).values():
            assert ev != dl_number

    def test_anonymize_fake_via_app_config(self, client):
        created = _register(client)
        app_id = created["app_id"]

        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "fake"},
        )

        dl = "KA-09-2023-5554321"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"DL: {dl}", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert dl not in body["anonymized_text"]
        assert len(body["entity_mapping"]) > 0

    def test_same_value_gets_same_fake_within_request(self, client):
        operator_config_store.set_strategy("IN_DRIVING_LICENSE", "fake")
        dl = "MH 14 2019 0012345"
        text = f"First: {dl}. Second: {dl}."
        resp = client.post(
            "/anonymize_unique",
            json={"text": text, "language": "en"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # The same DL should be replaced by the same fake value
        # entity_mapping should have exactly one entry for DL
        dl_entries = [
            (fake, orig)
            for fake, orig in body["entity_mapping"].items()
            if orig == dl
        ]
        assert len(dl_entries) == 1
        fake_val = dl_entries[0][0]
        # Both occurrences should use the same fake
        assert body["anonymized_text"].count(fake_val) == 2


# ---------------------------------------------------------------------------
# Integration tests — deanonymize with fake
# ---------------------------------------------------------------------------


class TestFakeDeanonymize:
    def _anonymize_with_fake(self, client, text):
        operator_config_store.set_strategy("IN_DRIVING_LICENSE", "fake")
        resp = client.post(
            "/anonymize_unique",
            json={"text": text, "language": "en"},
        )
        assert resp.status_code == 200
        return resp.json()

    def test_deanonymize_restores_fake_values(self, client):
        dl = "TN 01 2018 9990001"
        anon = self._anonymize_with_fake(client, f"License: {dl}. Contact John Smith.")

        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"]},
        )
        assert resp.status_code == 200
        restored = resp.json()["text"]
        # Fake values should be restored to originals
        assert dl in restored

    def test_round_trip_with_fake(self, client):
        dl = "MH 14 2019 0012345"
        original = f"My DL is {dl}."
        anon = self._anonymize_with_fake(client, original)

        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"]},
        )
        assert resp.status_code == 200
        assert dl in resp.json()["text"]


# ---------------------------------------------------------------------------
# Config validation — accepts fake
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_update_strategy_to_fake(self, client):
        created = _register(client)
        app_id = created["app_id"]

        resp = client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "fake"},
        )
        assert resp.status_code == 200
        assert resp.json()["config"]["IN_DRIVING_LICENSE"] == "fake"

    def test_invalid_strategy_still_rejected(self, client):
        created = _register(client)
        resp = client.put(
            f"/apps/{created['app_id']}/config",
            json={"entity_type": "PERSON", "strategy": "bad"},
        )
        assert resp.status_code == 400
