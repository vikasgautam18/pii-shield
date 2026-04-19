"""Tests for entity-type allow-list endpoints and filtering."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _create_app(client, name="TestApp"):
    resp = client.post("/apps", json={"app_name": name})
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# Entity-type allow-list CRUD endpoints
# ---------------------------------------------------------------------------


class TestEntityTypeAllowListEndpoints:
    def test_get_empty_entity_type_allow_list(self, client):
        created = _create_app(client)
        resp = client.get(f"/apps/{created['app_id']}/entity-type-allow-list")
        assert resp.status_code == 200
        assert resp.json()["entity_type_allow_list"] == []

    def test_set_and_get_entity_type_allow_list(self, client):
        created = _create_app(client)
        app_id = created["app_id"]

        resp = client.put(
            f"/apps/{app_id}/entity-type-allow-list",
            json={"entity_type_allow_list": ["EMAIL_ADDRESS", "PHONE_NUMBER"]},
        )
        assert resp.status_code == 200
        assert resp.json()["entity_type_allow_list"] == ["EMAIL_ADDRESS", "PHONE_NUMBER"]

        resp = client.get(f"/apps/{app_id}/entity-type-allow-list")
        assert resp.status_code == 200
        assert resp.json()["entity_type_allow_list"] == ["EMAIL_ADDRESS", "PHONE_NUMBER"]

    def test_entity_type_allow_list_nonexistent_app(self, client):
        resp = client.get("/apps/nonexistent/entity-type-allow-list")
        assert resp.status_code == 404

    def test_set_entity_type_allow_list_nonexistent_app(self, client):
        resp = client.put(
            "/apps/nonexistent/entity-type-allow-list",
            json={"entity_type_allow_list": ["EMAIL_ADDRESS"]},
        )
        assert resp.status_code == 404

    def test_set_entity_type_allow_list_bad_body(self, client):
        created = _create_app(client)
        resp = client.put(
            f"/apps/{created['app_id']}/entity-type-allow-list",
            json={"wrong_key": "bad"},
        )
        assert resp.status_code == 400

    def test_delete_app_removes_entity_type_allow_list(self, client):
        created = _create_app(client)
        app_id = created["app_id"]

        client.put(
            f"/apps/{app_id}/entity-type-allow-list",
            json={"entity_type_allow_list": ["EMAIL_ADDRESS"]},
        )
        resp = client.get(f"/apps/{app_id}/entity-type-allow-list")
        assert resp.json()["entity_type_allow_list"] == ["EMAIL_ADDRESS"]

        client.delete(f"/apps/{app_id}")

        resp = client.get(f"/apps/{app_id}/entity-type-allow-list")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Entity-type filtering during anonymization
# ---------------------------------------------------------------------------


class TestEntityTypeFiltering:
    def test_per_request_entity_type_allow_list(self, client):
        """Entity types in entity_type_allow_list should be excluded from results."""
        text = "Contact john@example.com or call +91 9811034567"
        resp = client.post("/anonymize_unique", json={
            "text": text,
            "entity_type_allow_list": ["EMAIL_ADDRESS", "URL"],
        })
        assert resp.status_code == 200
        data = resp.json()
        # Email should NOT be anonymized
        assert "john@example.com" in data["anonymized_text"]
        # Phone should still be anonymized
        assert "+91 9811034567" not in data["anonymized_text"]

    def test_per_app_entity_type_allow_list(self, client):
        """Per-app entity-type allow-list should exclude types during anonymization."""
        created = _create_app(client)
        app_id = created["app_id"]

        client.put(
            f"/apps/{app_id}/entity-type-allow-list",
            json={"entity_type_allow_list": ["EMAIL_ADDRESS", "URL"]},
        )

        text = "Contact john@example.com or call +91 9811034567"
        resp = client.post(
            "/anonymize_unique",
            json={"text": text},
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "john@example.com" in data["anonymized_text"]
        assert "+91 9811034567" not in data["anonymized_text"]

    def test_merged_entity_type_allow_lists(self, client):
        """Per-request and per-app entity-type allow-lists should be merged."""
        created = _create_app(client)
        app_id = created["app_id"]

        # App excludes EMAIL_ADDRESS and URL
        client.put(
            f"/apps/{app_id}/entity-type-allow-list",
            json={"entity_type_allow_list": ["EMAIL_ADDRESS", "URL"]},
        )

        text = "Contact john@example.com, call +91 9811034567, name is Rajesh Kumar"
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": text,
                "entity_type_allow_list": ["PERSON"],
            },
            headers={"X-App-Id": app_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        # EMAIL, URL, and PERSON should be preserved
        assert "john@example.com" in data["anonymized_text"]
        assert "Rajesh Kumar" in data["anonymized_text"]
        # Phone should still be anonymized
        assert "+91 9811034567" not in data["anonymized_text"]

    def test_empty_entity_type_allow_list_no_effect(self, client):
        """An empty entity_type_allow_list should not affect results."""
        text = "Contact john@example.com"
        resp = client.post("/anonymize_unique", json={
            "text": text,
            "entity_type_allow_list": [],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "john@example.com" not in data["anonymized_text"]
