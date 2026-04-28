import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.operator_config import operator_config_store


@pytest.fixture(autouse=True)
def _reset_config():
    """Reset global operator config to defaults."""
    operator_config_store.set_strategy("IN_DRIVING_LICENSE", "replace")
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def _register(client, name="TestApp"):
    resp = client.post("/apps", json={"app_name": name})
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# POST /apps — Registration
# ---------------------------------------------------------------------------


class TestRegisterApp:
    def test_register_returns_201(self, client):
        resp = client.post("/apps", json={"app_name": "MyApp"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["app_name"] == "MyApp"
        uuid.UUID(body["app_id"])  # valid UUID

    def test_register_seeds_default_config(self, client):
        body = _register(client)
        assert "IN_DRIVING_LICENSE" in body["config"]
        assert body["config"]["IN_DRIVING_LICENSE"] == "replace"

    def test_register_multiple_apps(self, client):
        app1 = _register(client, "App1")
        app2 = _register(client, "App2")
        assert app1["app_id"] != app2["app_id"]


# ---------------------------------------------------------------------------
# GET /apps/{app_id}
# ---------------------------------------------------------------------------


class TestGetApp:
    def test_get_existing_app(self, client):
        created = _register(client)
        resp = client.get(f"/apps/{created['app_id']}")
        assert resp.status_code == 200
        assert resp.json()["app_name"] == "TestApp"

    def test_get_nonexistent_app(self, client):
        resp = client.get("/apps/nonexistent-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /apps/{app_id}/config
# ---------------------------------------------------------------------------


class TestUpdateAppConfig:
    def test_update_strategy(self, client):
        created = _register(client)
        app_id = created["app_id"]

        resp = client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "replace"},
        )
        assert resp.status_code == 200
        assert resp.json()["config"]["IN_DRIVING_LICENSE"] == "replace"

    def test_add_new_entity_config(self, client):
        created = _register(client)
        app_id = created["app_id"]

        resp = client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "EMAIL_ADDRESS", "strategy": "hash"},
        )
        assert resp.status_code == 200
        assert resp.json()["config"]["EMAIL_ADDRESS"] == "hash"

    def test_update_nonexistent_app(self, client):
        resp = client.put(
            "/apps/nonexistent-id/config",
            json={"entity_type": "PERSON", "strategy": "hash"},
        )
        assert resp.status_code == 404

    def test_invalid_strategy(self, client):
        created = _register(client)
        resp = client.put(
            f"/apps/{created['app_id']}/config",
            json={"entity_type": "PERSON", "strategy": "bad"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /apps/{app_id}
# ---------------------------------------------------------------------------


class TestDeleteApp:
    def test_delete_existing(self, client):
        created = _register(client)
        resp = client.delete(f"/apps/{created['app_id']}")
        assert resp.status_code == 204

        # Confirm gone
        resp = client.get(f"/apps/{created['app_id']}")
        assert resp.status_code == 404

    def test_delete_nonexistent(self, client):
        resp = client.delete("/apps/nonexistent-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Per-app allow-list endpoints
# ---------------------------------------------------------------------------


class TestAppAllowList:
    def test_get_empty_allow_list(self, client):
        created = _register(client)
        resp = client.get(f"/apps/{created['app_id']}/allow-list")
        assert resp.status_code == 200
        assert resp.json()["allow_list"] == []

    def test_set_and_get_allow_list(self, client):
        created = _register(client)
        app_id = created["app_id"]

        resp = client.put(
            f"/apps/{app_id}/allow-list",
            json={"allow_list": ["Contoso Bank", "Woodgrove Bank"]},
        )
        assert resp.status_code == 200
        assert resp.json()["allow_list"] == ["Contoso Bank", "Woodgrove Bank"]

        resp = client.get(f"/apps/{app_id}/allow-list")
        assert resp.status_code == 200
        assert resp.json()["allow_list"] == ["Contoso Bank", "Woodgrove Bank"]

    def test_allow_list_nonexistent_app(self, client):
        resp = client.get("/apps/nonexistent/allow-list")
        assert resp.status_code == 404

    def test_set_allow_list_nonexistent_app(self, client):
        resp = client.put(
            "/apps/nonexistent/allow-list",
            json={"allow_list": ["Test"]},
        )
        assert resp.status_code == 404

    def test_set_allow_list_bad_body(self, client):
        created = _register(client)
        resp = client.put(
            f"/apps/{created['app_id']}/allow-list",
            json={"wrong_field": "test"},
        )
        assert resp.status_code == 400

    def test_app_allow_list_used_during_anonymize(self, client):
        """Per-app allow-list should exclude terms during anonymization."""
        created = _register(client)
        app_id = created["app_id"]

        # Set allow-list for the app
        client.put(
            f"/apps/{app_id}/allow-list",
            json={"allow_list": ["Contoso Bank"]},
        )

        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Account at Contoso Bank, Chennai branch.",
                "language": "en",
            },
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "Contoso Bank" in body["anonymized_text"]

    def test_delete_app_removes_allow_list(self, client):
        created = _register(client)
        app_id = created["app_id"]

        client.put(
            f"/apps/{app_id}/allow-list",
            json={"allow_list": ["Test Corp"]},
        )
        client.delete(f"/apps/{app_id}")
        resp = client.get(f"/apps/{app_id}/allow-list")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /apps — List all apps
# ---------------------------------------------------------------------------


class TestListApps:
    def test_list_empty(self, client):
        resp = client.get("/apps")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_after_register(self, client):
        _register(client, "App1")
        _register(client, "App2")
        resp = client.get("/apps")
        assert resp.status_code == 200
        apps = resp.json()
        assert len(apps) == 2
        names = {a["app_name"] for a in apps}
        assert names == {"App1", "App2"}

    def test_list_after_delete(self, client):
        created = _register(client, "ToDelete")
        client.delete(f"/apps/{created['app_id']}")
        resp = client.get("/apps")
        assert resp.status_code == 200
        assert all(a["app_id"] != created["app_id"] for a in resp.json())


# ---------------------------------------------------------------------------
# X-App-Id header integration
# ---------------------------------------------------------------------------


class TestAppIdHeader:
    def test_anonymize_uses_app_config(self, client):
        created = _register(client)
        app_id = created["app_id"]

        # Change app config: DL → hash (instead of default replace)
        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "hash"},
        )

        dl = "MH 14 2019 0012345"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"DL: {dl}", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        expected_hash = hashlib.sha3_256(dl.encode()).hexdigest()
        assert expected_hash in resp.json()["anonymized_text"]

    def test_anonymize_without_header_uses_global(self, client):
        dl = "MH 14 2019 0012345"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"DL: {dl}", "language": "en"},
        )
        assert resp.status_code == 200
        # Default is now "replace"
        assert "{{IN_DRIVING_LICENSE_" in resp.json()["anonymized_text"]
    def test_anonymize_unique_uses_app_config(self, client):
        created = _register(client)
        app_id = created["app_id"]

        # Default app config is now replace for DL
        dl = "KA-09-2023-5554321"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"DL: {dl}", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "{{IN_DRIVING_LICENSE_" in body["anonymized_text"]
        dl_keys = [k for k in body["entity_mapping"] if "IN_DRIVING_LICENSE" in k]
        assert len(dl_keys) >= 1

    def test_anonymize_unique_app_hash_strategy(self, client):
        created = _register(client)
        app_id = created["app_id"]

        client.put(
            f"/apps/{app_id}/config",
            json={"entity_type": "IN_DRIVING_LICENSE", "strategy": "hash"},
        )

        dl = "KA-09-2023-5554321"
        resp = client.post(
            "/anonymize_unique",
            json={"text": f"DL: {dl}", "language": "en"},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        expected_hash = hashlib.sha3_256(dl.encode()).hexdigest()
        assert expected_hash in body["anonymized_text"]
        # Hashed DL should not be in entity_mapping
        for key in body["entity_mapping"]:
            assert "IN_DRIVING_LICENSE" not in key

    def test_invalid_app_id_returns_404(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={"text": "Hello", "language": "en"},
            headers={"X-App-Id": "bad-id"},
        )
        assert resp.status_code == 404

    def test_deanonymize_with_invalid_app_id(self, client):
        resp = client.post(
            "/deanonymize",
            json={"id": "some-id", "text": "test"},
            headers={"X-App-Id": "bad-id"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Redis persistence (data stored correctly)
# ---------------------------------------------------------------------------


class TestRedisPersistence:
    def test_data_survives_in_redis(self, _fake_redis, client):
        created = _register(client)
        app_id = created["app_id"]

        # Verify data is in Redis
        assert _fake_redis.get(f"app:{app_id}:name") == "TestApp"
        assert _fake_redis.sismember("apps", app_id)

    def test_delete_removes_from_redis(self, _fake_redis, client):
        created = _register(client)
        app_id = created["app_id"]
        client.delete(f"/apps/{app_id}")

        assert _fake_redis.get(f"app:{app_id}:name") is None
        assert not _fake_redis.sismember("apps", app_id)
