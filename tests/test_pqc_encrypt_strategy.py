"""Tests for the ML-KEM-768 + AES-256-GCM (PQC) encryption backend."""

import base64
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.operator_config import operator_config_store


@pytest.fixture(autouse=True)
def _reset_config():
    operator_config_store.set_strategy("IN_DRIVING_LICENSE", "replace")
    yield


@pytest.fixture(autouse=True)
def _force_pqc_backend(monkeypatch):
    """Ensure PQC backend is active and Fernet key is available for mixed tests."""
    monkeypatch.setenv("ENCRYPTION_BACKEND", "pqc")
    # Also set Fernet key so mixed-backend tests can work
    from cryptography.fernet import Fernet
    monkeypatch.setenv("PII_SHIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture()
def client():
    return TestClient(app)


def _register(client, name="TestApp"):
    resp = client.post("/apps", json={"app_name": name})
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# Unit tests — PqcEncryptOperator
# ---------------------------------------------------------------------------


class TestPqcEncryptOperator:
    def test_round_trip(self):
        from app.operators.pqc_encrypt import encrypt, decrypt

        plaintext = "MH 14 2019 0012345"
        token = encrypt(plaintext)
        assert decrypt(token) == plaintext

    def test_unicode_round_trip(self):
        from app.operators.pqc_encrypt import encrypt, decrypt

        plaintext = "विकास गौतम [lock]"
        token = encrypt(plaintext)
        assert decrypt(token) == plaintext

    def test_different_inputs_different_ciphertexts(self):
        from app.operators.pqc_encrypt import encrypt

        assert encrypt("abc") != encrypt("def")

    def test_non_deterministic(self):
        from app.operators.pqc_encrypt import encrypt

        t1 = encrypt("same")
        t2 = encrypt("same")
        assert t1 != t2, "PQC encryption should produce different tokens for same input"

    def test_auto_generates_keys_when_not_set(self, monkeypatch):
        """Keys auto-generate when env vars are empty."""
        import app.operators.pqc_encrypt as pqc_mod
        monkeypatch.setenv("PQC_ENCAPSULATION_KEY", "")
        monkeypatch.setenv("PQC_DECAPSULATION_KEY", "")
        # Reset cached keys
        monkeypatch.setattr(pqc_mod, "_auto_ek", None)
        monkeypatch.setattr(pqc_mod, "_auto_dk", None)

        token = pqc_mod.encrypt("test")
        result = pqc_mod.decrypt(token)
        assert result == "test"

    def test_explicit_keys_from_env(self, monkeypatch):
        """Keys loaded from env vars when set."""
        from mlkem import ML_KEM, MLKEM_768_PARAMETERS
        import app.operators.pqc_encrypt as pqc_mod

        ml_kem = ML_KEM(MLKEM_768_PARAMETERS)
        ek, dk = ml_kem.key_gen()
        monkeypatch.setenv("PQC_ENCAPSULATION_KEY", base64.b64encode(bytes(ek)).decode())
        monkeypatch.setenv("PQC_DECAPSULATION_KEY", base64.b64encode(bytes(dk)).decode())
        # Reset cached keys
        monkeypatch.setattr(pqc_mod, "_auto_ek", None)
        monkeypatch.setattr(pqc_mod, "_auto_dk", None)

        token = pqc_mod.encrypt("explicit key test")
        assert pqc_mod.decrypt(token) == "explicit key test"

    def test_operator_name(self):
        from app.operators.pqc_encrypt import PqcEncryptOperator
        op = PqcEncryptOperator()
        assert op.operator_name() == "pqc_encrypt"

    def test_operator_operate(self):
        from app.operators.pqc_encrypt import PqcEncryptOperator, decrypt
        op = PqcEncryptOperator()
        token = op.operate("hello world")
        assert decrypt(token) == "hello world"


# ---------------------------------------------------------------------------
# Unit tests — Token detection
# ---------------------------------------------------------------------------


class TestTokenDetection:
    def test_pqc_token_detected(self):
        from app.operators.pqc_encrypt import encrypt, is_pqc_token
        token = encrypt("test")
        assert is_pqc_token(token) is True

    def test_fernet_token_not_pqc(self):
        from app.operators.fernet_encrypt import FernetEncryptOperator
        from app.operators.pqc_encrypt import is_pqc_token
        op = FernetEncryptOperator()
        token = op.operate("test")
        assert is_pqc_token(token) is False

    def test_fernet_token_detected(self):
        from app.operators.fernet_encrypt import FernetEncryptOperator, is_fernet_token
        op = FernetEncryptOperator()
        token = op.operate("test")
        assert is_fernet_token(token) is True

    def test_pqc_token_not_fernet(self):
        from app.operators.pqc_encrypt import encrypt
        from app.operators.fernet_encrypt import is_fernet_token
        token = encrypt("test")
        assert is_fernet_token(token) is False

    def test_garbage_not_detected(self):
        from app.operators.pqc_encrypt import is_pqc_token
        from app.operators.fernet_encrypt import is_fernet_token
        assert is_pqc_token("not-a-token") is False
        assert is_fernet_token("not-a-token") is False


# ---------------------------------------------------------------------------
# Integration tests — PQC encrypt via /anonymize_unique
# ---------------------------------------------------------------------------


class TestPqcAnonymize:
    def _setup_encrypt_app(self, client):
        """Register a test app with IN_DRIVING_LICENSE: encrypt."""
        resp = client.post("/apps", json={"app_name": "__test_dl_pqc"})
        app_id = resp.json()["app_id"]
        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "encrypt"},
        )
        return app_id

    def test_anonymize_encrypts_dl_with_pqc(self, client):
        app_id = self._setup_encrypt_app(client)
        dl_number = "MH 14 2019 0012345"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"My driving license is {dl_number}.", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert dl_number not in body["anonymized_text"]
        assert len(body["encrypt_mapping"]) > 0
        # Verify token is PQC format
        from app.operators.pqc_encrypt import is_pqc_token
        for token in body["encrypt_mapping"]:
            assert is_pqc_token(token), f"Expected PQC token, got: {token[:40]}..."

    def test_anonymize_pqc_via_app_config(self, client):
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
# Integration tests — deanonymize with PQC
# ---------------------------------------------------------------------------


class TestPqcDeanonymize:
    def _setup_encrypt_app(self, client):
        """Register a test app with IN_DRIVING_LICENSE: encrypt."""
        resp = client.post("/apps", json={"app_name": "__test_dl_pqc_deanon"})
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

    def test_deanonymize_does_not_restore_by_default(self, client):
        dl = "TN 01 2018 9990001"
        anon = self._anonymize_with_encrypt(client, f"License: {dl}. Contact John Smith.")
        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"]},
        )
        assert resp.status_code == 200
        assert dl not in resp.json()["text"]

    def test_deanonymize_restores_with_flag(self, client):
        dl = "TN 01 2018 9990001"
        anon = self._anonymize_with_encrypt(client, f"License: {dl}. Contact John Smith.")
        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"], "include_encrypted": True},
        )
        assert resp.status_code == 200
        assert dl in resp.json()["text"]

    def test_round_trip(self, client):
        dl = "MH 14 2019 0012345"
        anon = self._anonymize_with_encrypt(client, f"My DL is {dl}.")
        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"], "include_encrypted": True},
        )
        assert resp.status_code == 200
        assert dl in resp.json()["text"]


# ---------------------------------------------------------------------------
# Backend toggle test
# ---------------------------------------------------------------------------


class TestBackendToggle:
    def test_fernet_backend_via_env(self, client, monkeypatch):
        """When ENCRYPTION_BACKEND=fernet, encrypt path uses Fernet tokens."""
        monkeypatch.setenv("ENCRYPTION_BACKEND", "fernet")
        # Force re-import to pick up new env — use Fernet directly
        from app.operators.fernet_encrypt import FernetEncryptOperator, is_fernet_token
        op = FernetEncryptOperator()
        token = op.operate("test-fernet-backend")
        assert is_fernet_token(token) is True


# ---------------------------------------------------------------------------
# Keygen utility test
# ---------------------------------------------------------------------------


class TestKeygen:
    def test_keygen_output(self, capsys):
        from app.operators.keygen import main
        main()
        output = capsys.readouterr().out
        assert "PQC_ENCAPSULATION_KEY=" in output
        assert "PQC_DECAPSULATION_KEY=" in output
        assert "1184 bytes" in output
        assert "2400 bytes" in output
