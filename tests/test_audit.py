"""Tests for the SQLite audit store and /audit-log endpoint."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import audit_store
from app.main import app


@pytest.fixture(autouse=True)
def _temp_audit_db(monkeypatch, tmp_path):
    """Use a temporary SQLite DB for each test."""
    db_path = str(tmp_path / "test_audit.db")
    monkeypatch.setattr(audit_store, "AUDIT_DB_PATH", db_path)
    yield db_path


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _create_app(client, name="AuditTestApp"):
    resp = client.post("/apps", json={"app_name": name})
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# Unit tests for audit_store module
# ---------------------------------------------------------------------------


class TestAuditStore:
    def test_record_event_and_retrieve(self):
        audit_store.record_event("APP_REGISTERED", "app-123", "MyApp", {"key": "val"})
        logs = audit_store.get_audit_log()
        assert len(logs) == 1
        assert logs[0]["action"] == "APP_REGISTERED"
        assert logs[0]["app_id"] == "app-123"
        assert logs[0]["app_name"] == "MyApp"
        assert '"key"' in logs[0]["details"]

    def test_filter_by_app_id(self):
        audit_store.record_event("APP_REGISTERED", "app-1", "App1")
        audit_store.record_event("APP_REGISTERED", "app-2", "App2")
        logs = audit_store.get_audit_log(app_id="app-1")
        assert len(logs) == 1
        assert logs[0]["app_id"] == "app-1"

    def test_filter_by_action(self):
        audit_store.record_event("APP_REGISTERED", "app-1", "App1")
        audit_store.record_event("APP_DELETED", "app-1", "App1")
        logs = audit_store.get_audit_log(action="APP_DELETED")
        assert len(logs) == 1
        assert logs[0]["action"] == "APP_DELETED"

    def test_limit(self):
        for i in range(5):
            audit_store.record_event("APP_REGISTERED", f"app-{i}", f"App{i}")
        logs = audit_store.get_audit_log(limit=3)
        assert len(logs) == 3

    def test_newest_first(self):
        audit_store.record_event("APP_REGISTERED", "app-first", "First")
        audit_store.record_event("APP_REGISTERED", "app-second", "Second")
        logs = audit_store.get_audit_log()
        assert logs[0]["app_id"] == "app-second"
        assert logs[1]["app_id"] == "app-first"

    def test_string_details(self):
        audit_store.record_event("CONFIG_UPDATED", "app-1", "App1", "simple string")
        logs = audit_store.get_audit_log()
        assert logs[0]["details"] == "simple string"


# ---------------------------------------------------------------------------
# Integration tests via API endpoints
# ---------------------------------------------------------------------------


class TestAuditEndpoint:
    def test_register_creates_audit_entry(self, client):
        _create_app(client, "AuditApp")
        resp = client.get("/audit-log")
        assert resp.status_code == 200
        entries = resp.json()
        assert any(e["action"] == "APP_REGISTERED" and e["app_name"] == "AuditApp" for e in entries)

    def test_update_config_creates_audit_entry(self, client):
        created = _create_app(client)
        app_id = created["app_id"]
        client.put(f"/apps/{app_id}/config", json={"entity_type": "EMAIL_ADDRESS", "strategy": "hash"})
        resp = client.get("/audit-log")
        entries = resp.json()
        assert any(e["action"] == "CONFIG_UPDATED" and e["app_id"] == app_id for e in entries)

    def test_update_allow_list_creates_audit_entry(self, client):
        created = _create_app(client)
        app_id = created["app_id"]
        client.put(f"/apps/{app_id}/allow-list", json={"allow_list": ["Test Corp"]})
        resp = client.get("/audit-log")
        entries = resp.json()
        match = [e for e in entries if e["action"] == "ALLOW_LIST_UPDATED" and e["app_id"] == app_id]
        assert len(match) == 1
        assert match[0]["app_name"] == "AuditTestApp"

    def test_update_entity_type_allow_list_creates_audit_entry(self, client):
        created = _create_app(client)
        app_id = created["app_id"]
        client.put(f"/apps/{app_id}/entity-type-allow-list", json={"entity_type_allow_list": ["EMAIL_ADDRESS"]})
        resp = client.get("/audit-log")
        entries = resp.json()
        match = [
            e for e in entries
            if e["action"] == "ENTITY_TYPE_ALLOW_LIST_UPDATED" and e["app_id"] == app_id
        ]
        assert len(match) == 1
        assert match[0]["app_name"] == "AuditTestApp"

    def test_delete_creates_audit_entry(self, client):
        created = _create_app(client)
        app_id = created["app_id"]
        client.delete(f"/apps/{app_id}")
        resp = client.get("/audit-log")
        entries = resp.json()
        assert any(e["action"] == "APP_DELETED" and e["app_id"] == app_id for e in entries)

    def test_audit_log_filter_by_app_id(self, client):
        app1 = _create_app(client, "App1")
        app2 = _create_app(client, "App2")
        resp = client.get("/audit-log", params={"app_id": app1["app_id"]})
        entries = resp.json()
        assert all(e["app_id"] == app1["app_id"] for e in entries)

    def test_audit_log_filter_by_action(self, client):
        created = _create_app(client)
        client.delete(f"/apps/{created['app_id']}")
        resp = client.get("/audit-log", params={"action": "APP_DELETED"})
        entries = resp.json()
        assert all(e["action"] == "APP_DELETED" for e in entries)

    def test_audit_log_limit(self, client):
        for i in range(5):
            _create_app(client, f"LimitApp{i}")
        resp = client.get("/audit-log", params={"limit": 2})
        entries = resp.json()
        assert len(entries) == 2

    def test_audit_log_empty(self, client):
        resp = client.get("/audit-log")
        assert resp.status_code == 200
        assert resp.json() == []
