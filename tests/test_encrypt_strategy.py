import os
import hashlib

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.operator_config import operator_config_store


@pytest.fixture(autouse=True)
def _reset_config():
    """Reset operator config to defaults before each test."""
    operator_config_store.set_strategy("IN_DRIVING_LICENSE", "replace")
    yield


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Ensure a valid Fernet key is available for all tests."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PII_SHIELD_ENCRYPTION_KEY", key)


@pytest.fixture()
def client():
    return TestClient(app)


def _register(client, name="TestApp"):
    resp = client.post("/apps", json={"app_name": name})
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# Unit tests — FernetEncryptOperator
# ---------------------------------------------------------------------------


class TestFernetEncryptOperator:
    def test_produces_valid_fernet_token(self):
        from cryptography.fernet import Fernet
        from app.operators.fernet_encrypt import FernetEncryptOperator

        op = FernetEncryptOperator()
        token = op.operate("MH 14 2019 0012345")
        # Should be a valid Fernet token (decryptable)
        f = Fernet(os.environ["PII_SHIELD_ENCRYPTION_KEY"].encode())
        plaintext = f.decrypt(token.encode()).decode()
        assert plaintext == "MH 14 2019 0012345"

    def test_decrypt_reverses_encrypt(self):
        from app.operators.fernet_encrypt import FernetEncryptOperator, decrypt

        op = FernetEncryptOperator()
        token = op.operate("hello world")
        assert decrypt(token) == "hello world"

    def test_different_inputs_different_ciphertexts(self):
        from app.operators.fernet_encrypt import FernetEncryptOperator

        op = FernetEncryptOperator()
        assert op.operate("abc") != op.operate("def")

    def test_non_deterministic(self):
        from app.operators.fernet_encrypt import FernetEncryptOperator

        op = FernetEncryptOperator()
        # Fernet uses random IV, so same input should produce different tokens
        t1 = op.operate("same")
        t2 = op.operate("same")
        assert t1 != t2

    def test_missing_key_raises_error(self, monkeypatch):
        from app.operators.fernet_encrypt import FernetEncryptOperator

        monkeypatch.delenv("PII_SHIELD_ENCRYPTION_KEY", raising=False)
        op = FernetEncryptOperator()
        with pytest.raises(RuntimeError, match="PII_SHIELD_ENCRYPTION_KEY"):
            op.operate("test")


# ---------------------------------------------------------------------------
# Integration tests — encrypt strategy via /anonymize_unique
# ---------------------------------------------------------------------------


class TestEncryptAnonymize:
    def _setup_encrypt_app(self, client):
        """Register a test app with IN_DRIVING_LICENSE: encrypt."""
        resp = client.post("/apps", json={"app_name": "__test_dl_encrypt"})
        app_id = resp.json()["app_id"]
        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "encrypt"},
        )
        return app_id

    def test_anonymize_encrypts_dl_when_configured(self, client):
        app_id = self._setup_encrypt_app(client)
        dl_number = "MH 14 2019 0012345"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"My driving license is {dl_number}.", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Original should not appear in anonymized text
        assert dl_number not in body["anonymized_text"]
        # encrypt_mapping should contain the original value
        assert len(body["encrypt_mapping"]) > 0
        original_values = list(body["encrypt_mapping"].values())
        assert dl_number in original_values
        # entity_mapping should NOT contain encrypted entities
        for key in body["entity_mapping"]:
            assert "IN_DRIVING_LICENSE" not in key

    def test_anonymize_encrypt_via_app_config(self, client):
        created = _register(client)
        app_id = created["app_id"]

        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "encrypt"},
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
        assert len(body["encrypt_mapping"]) > 0


# ---------------------------------------------------------------------------
# Integration tests — deanonymize with encrypt
# ---------------------------------------------------------------------------


class TestEncryptDeanonymize:
    def _setup_encrypt_app(self, client):
        """Register a test app with IN_DRIVING_LICENSE: encrypt."""
        resp = client.post("/apps", json={"app_name": "__test_dl_encrypt_deanon"})
        app_id = resp.json()["app_id"]
        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "encrypt"},
        )
        return app_id

    def _anonymize_with_encrypt(self, client, text):
        app_id = self._setup_encrypt_app(client)
        resp = client.post(
            "/anonymize_unique",
            json={"text": text, "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        return resp.json()

    def test_deanonymize_does_not_restore_encrypted_by_default(self, client):
        dl = "TN 01 2018 9990001"
        anon = self._anonymize_with_encrypt(client, f"License: {dl}. Contact John Smith.")

        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"]},
        )
        assert resp.status_code == 200
        restored = resp.json()["text"]
        # Encrypted value should NOT be restored by default
        assert dl not in restored

    def test_deanonymize_restores_encrypted_when_flag_set(self, client):
        dl = "TN 01 2018 9990001"
        anon = self._anonymize_with_encrypt(client, f"License: {dl}. Contact John Smith.")

        resp = client.post(
            "/deanonymize",
            json={
                "id": anon["id"],
                "text": anon["anonymized_text"],
                "include_encrypted": True,
            },
        )
        assert resp.status_code == 200
        restored = resp.json()["text"]
        assert dl in restored

    def test_round_trip_with_encrypt(self, client):
        dl = "MH 14 2019 0012345"
        original = f"My DL is {dl}."
        anon = self._anonymize_with_encrypt(client, original)

        resp = client.post(
            "/deanonymize",
            json={
                "id": anon["id"],
                "text": anon["anonymized_text"],
                "include_encrypted": True,
            },
        )
        assert resp.status_code == 200
        assert dl in resp.json()["text"]


# ---------------------------------------------------------------------------
# Config validation — accepts encrypt
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_update_strategy_to_encrypt(self, client):
        created = _register(client)
        app_id = created["app_id"]

        resp = client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "encrypt"},
        )
        assert resp.status_code == 200
        assert resp.json()["config"]["IN_DRIVING_LICENSE"] == "encrypt"

    def test_invalid_strategy_still_rejected(self, client):
        created = _register(client)
        resp = client.put(
            f"/apps/{created['app_id']}/config",
            json={"entity_type": "PERSON", "strategy": "bad"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# State store — encrypt_mapping persistence
# ---------------------------------------------------------------------------


class TestStateStoreEncryptMapping:
    def test_save_and_get_with_encrypt_mapping(self):
        import asyncio
        from app.state_store import AnonymizationRecord, AnonymizationStore

        s = AnonymizationStore()
        rec = AnonymizationRecord(
            original_text="DL: MH 14 2019 0012345",
            anonymized_text="DL: gAAA...",
            entity_mapping={},
            app_id="test-app-id",
            encrypt_mapping={"gAAA...": "MH 14 2019 0012345"},
        )
        rid = asyncio.get_event_loop().run_until_complete(s.save(rec))
        loaded = asyncio.get_event_loop().run_until_complete(s.get(rid))
        assert loaded is not None
        assert loaded.encrypt_mapping == {"gAAA...": "MH 14 2019 0012345"}
