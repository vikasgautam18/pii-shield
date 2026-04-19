import hashlib

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.operator_config import operator_config_store
from app.recognizers.in_driving_license import InDrivingLicenseRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_config():
    """Reset operator config to defaults before each test."""
    operator_config_store.set_strategy("IN_DRIVING_LICENSE", "replace")
    yield


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestRecognizerPatterns:
    """Verify the recognizer detects DL numbers in isolation."""

    recognizer = InDrivingLicenseRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["IN_DRIVING_LICENSE"])

    # ── Positive cases ────────────────────────────────────────────────────

    def test_space_separated(self):
        results = self._match("DL number is MH 01 2020 1234567 here")
        assert len(results) == 1
        assert results[0].entity_type == "IN_DRIVING_LICENSE"
        assert results[0].score >= 0.85

    def test_hyphen_separated(self):
        results = self._match("DL: DL-05-2019-9876543")
        assert len(results) == 1
        assert results[0].score >= 0.85

    def test_no_separator(self):
        results = self._match("License TN0120181234567 issued")
        assert len(results) == 1
        assert results[0].score >= 0.5

    def test_various_state_codes(self):
        for code in ("KA", "UP", "GJ", "WB", "RJ"):
            results = self._match(f"License {code} 10 2022 0000001")
            assert len(results) == 1, f"Failed for state code {code}"

    def test_multiple_dl_numbers(self):
        text = "First: MH 01 2020 1234567, second: DL 02 2021 7654321."
        results = self._match(text)
        assert len(results) == 2

    # ── Negative cases ────────────────────────────────────────────────────

    def test_invalid_state_code(self):
        results = self._match("XX 01 2020 1234567")
        assert len(results) == 0

    def test_wrong_digit_count_serial(self):
        results = self._match("MH 01 2020 12345")
        assert len(results) == 0

    def test_wrong_digit_count_rto(self):
        results = self._match("MH 1 2020 1234567")
        assert len(results) == 0

    def test_wrong_digit_count_year(self):
        results = self._match("MH 01 20 1234567")
        assert len(results) == 0

    def test_empty_text(self):
        results = self._match("")
        assert len(results) == 0

    def test_no_pii(self):
        results = self._match("Nothing sensitive here.")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Unit tests — SHA3 operator
# ---------------------------------------------------------------------------


class TestSha3HashOperator:
    def test_produces_correct_hash(self):
        from app.operators.sha3_hash import Sha3HashOperator

        op = Sha3HashOperator()
        result = op.operate("MH 14 2019 0012345")
        expected = hashlib.sha3_256("MH 14 2019 0012345".encode()).hexdigest()
        assert result == expected

    def test_deterministic(self):
        from app.operators.sha3_hash import Sha3HashOperator

        op = Sha3HashOperator()
        assert op.operate("test") == op.operate("test")

    def test_different_inputs_different_hashes(self):
        from app.operators.sha3_hash import Sha3HashOperator

        op = Sha3HashOperator()
        assert op.operate("abc") != op.operate("def")


# ---------------------------------------------------------------------------
# Integration tests — hashing via /anonymize_unique
# ---------------------------------------------------------------------------


class TestDLHashAnonymize:
    def _setup_hash_app(self, client):
        """Register a test app with IN_DRIVING_LICENSE: hash."""
        resp = client.post("/apps", json={"app_name": "__test_dl_hash"})
        app_id = resp.json()["app_id"]
        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "hash"},
        )
        return app_id

    def test_anonymize_hashes_dl_when_configured(self, client):
        app_id = self._setup_hash_app(client)
        dl_number = "MH 14 2019 0012345"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"My driving license is {dl_number}.", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        expected_hash = hashlib.sha3_256(dl_number.encode()).hexdigest()
        assert expected_hash in body["anonymized_text"]
        assert dl_number not in body["anonymized_text"]
        # hash_mapping should contain the original value
        assert expected_hash in body["hash_mapping"]
        assert body["hash_mapping"][expected_hash] == dl_number

    def test_anonymize_unique_hashes_dl_and_excludes_from_mapping(self, client):
        app_id = self._setup_hash_app(client)
        dl_number = "KA-09-2023-5554321"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"DL number: {dl_number}.", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        expected_hash = hashlib.sha3_256(dl_number.encode()).hexdigest()
        assert expected_hash in body["anonymized_text"]
        assert dl_number not in body["anonymized_text"]
        # Hashed entities should NOT appear in entity_mapping
        for key in body["entity_mapping"]:
            assert "IN_DRIVING_LICENSE" not in key

    def test_deanonymize_does_not_restore_hashed_dl(self, client):
        app_id = self._setup_hash_app(client)
        text = "License: TN 01 2018 9990001. Contact John Smith."
        anon_resp = client.post(
            "/anonymize_unique", json={"text": text, "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        restored = deanon_resp.json()["text"]
        # DL should remain hashed (not restored)
        assert "TN 01 2018 9990001" not in restored
        dl_hash = hashlib.sha3_256("TN 01 2018 9990001".encode()).hexdigest()
        assert dl_hash in restored

    def test_deanonymize_restores_hashed_dl_when_include_hashed(self, client):
        app_id = self._setup_hash_app(client)
        dl_number = "TN 01 2018 9990001"
        text = f"License: {dl_number}. Contact John Smith."
        anon_resp = client.post(
            "/anonymize_unique", json={"text": text, "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()

        deanon_resp = client.post(
            "/deanonymize",
            json={
                "id": anon_body["id"],
                "text": anon_body["anonymized_text"],
                "include_hashed": True,
            },
        )
        assert deanon_resp.status_code == 200
        restored = deanon_resp.json()["text"]
        assert dl_number in restored


# ---------------------------------------------------------------------------
# Integration tests — replace strategy via config
# ---------------------------------------------------------------------------


class TestDLReplaceStrategy:
    def test_anonymize_replaces_dl_by_default(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={"text": "My driving license is MH 14 2019 0012345.", "language": "en"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_DRIVING_LICENSE_" in body["anonymized_text"]
        assert "MH 14 2019 0012345" not in body["anonymized_text"]

    def test_anonymize_unique_uses_placeholder_by_default(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={"text": "DL number: KA-09-2023-5554321.", "language": "en"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_DRIVING_LICENSE_" in body["anonymized_text"]
        dl_keys = [k for k in body["entity_mapping"] if "IN_DRIVING_LICENSE" in k]
        assert len(dl_keys) >= 1

    def test_round_trip_when_replace(self, client):
        original = "License: TN 01 2018 9990001."
        anon_resp = client.post(
            "/anonymize_unique", json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        assert "TN 01 2018 9990001" in deanon_resp.json()["text"]


# ---------------------------------------------------------------------------
# Integration tests — per-app config change affects anonymization
# ---------------------------------------------------------------------------


class TestAppConfigChangeAffectsAnonymization:
    def test_config_change_via_app_endpoint(self, client):
        """Verify switching per-app config between replace and hash takes effect."""
        # Register an app (uses conftest's shared fakeredis)
        reg_resp = client.post("/apps", json={"app_name": "ConfigTestApp"})
        assert reg_resp.status_code == 201
        app_id = reg_resp.json()["app_id"]

        dl_number = "MH 14 2019 0012345"

        # Default is replace
        resp1 = client.post(
            "/anonymize_unique",
            json={"text": f"DL: {dl_number}", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert "{{IN_DRIVING_LICENSE_" in resp1.json()["anonymized_text"]

        # Switch to hash
        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "hash"},
        )
        resp2 = client.post(
            "/anonymize_unique",
            json={"text": f"DL: {dl_number}", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        expected_hash = hashlib.sha3_256(dl_number.encode()).hexdigest()
        assert expected_hash in resp2.json()["anonymized_text"]
