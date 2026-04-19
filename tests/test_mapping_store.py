"""Tests for pii_shield.mapping_store — InMemory, SQLite, JsonFile stores."""

import tempfile
from pathlib import Path

import pytest

from pii_shield.mapping_store import (
    InMemoryMappingStore,
    JsonFileMappingStore,
    SqliteMappingStore,
)
from pii_shield.models import AnonymizeResult


def _sample_result() -> AnonymizeResult:
    return AnonymizeResult(
        anonymized_text="Hello {{PERSON_1}}",
        entity_mapping={"{{PERSON_1}}": "Rahul"},
        hash_mapping={"abc123": "secret"},
        encrypt_mapping={"enc456": "hidden"},
    )


# ---------------------------------------------------------------------------
# InMemoryMappingStore
# ---------------------------------------------------------------------------

class TestInMemoryMappingStore:
    def test_save_and_get(self):
        store = InMemoryMappingStore()
        result = _sample_result()
        rid = store.save(result)
        loaded = store.get(rid)
        assert loaded is not None
        assert loaded.anonymized_text == result.anonymized_text
        assert loaded.entity_mapping == result.entity_mapping
        assert loaded.hash_mapping == result.hash_mapping
        assert loaded.encrypt_mapping == result.encrypt_mapping

    def test_save_with_custom_id(self):
        store = InMemoryMappingStore()
        rid = store.save(_sample_result(), record_id="my-id")
        assert rid == "my-id"
        assert store.get("my-id") is not None

    def test_get_missing_returns_none(self):
        store = InMemoryMappingStore()
        assert store.get("nonexistent") is None

    def test_delete(self):
        store = InMemoryMappingStore()
        rid = store.save(_sample_result())
        assert store.delete(rid) is True
        assert store.get(rid) is None
        assert store.delete(rid) is False

    def test_len(self):
        store = InMemoryMappingStore()
        assert len(store) == 0
        store.save(_sample_result())
        store.save(_sample_result())
        assert len(store) == 2


# ---------------------------------------------------------------------------
# SqliteMappingStore
# ---------------------------------------------------------------------------

class TestSqliteMappingStore:
    def test_save_and_get(self, tmp_path):
        store = SqliteMappingStore(tmp_path / "test.db")
        result = _sample_result()
        rid = store.save(result)
        loaded = store.get(rid)
        assert loaded is not None
        assert loaded.anonymized_text == result.anonymized_text
        assert loaded.entity_mapping == result.entity_mapping
        assert loaded.hash_mapping == result.hash_mapping
        store.close()

    def test_persistence_across_instances(self, tmp_path):
        db_path = tmp_path / "persist.db"
        store1 = SqliteMappingStore(db_path)
        rid = store1.save(_sample_result())
        store1.close()

        store2 = SqliteMappingStore(db_path)
        loaded = store2.get(rid)
        assert loaded is not None
        assert loaded.entity_mapping == {"{{PERSON_1}}": "Rahul"}
        store2.close()

    def test_get_missing_returns_none(self, tmp_path):
        store = SqliteMappingStore(tmp_path / "test.db")
        assert store.get("nonexistent") is None
        store.close()

    def test_delete(self, tmp_path):
        store = SqliteMappingStore(tmp_path / "test.db")
        rid = store.save(_sample_result())
        assert store.delete(rid) is True
        assert store.get(rid) is None
        assert store.delete(rid) is False
        store.close()

    def test_len(self, tmp_path):
        store = SqliteMappingStore(tmp_path / "test.db")
        assert len(store) == 0
        store.save(_sample_result())
        store.save(_sample_result())
        assert len(store) == 2
        store.close()

    def test_save_with_custom_id(self, tmp_path):
        store = SqliteMappingStore(tmp_path / "test.db")
        rid = store.save(_sample_result(), record_id="custom-id")
        assert rid == "custom-id"
        assert store.get("custom-id") is not None
        store.close()


# ---------------------------------------------------------------------------
# JsonFileMappingStore
# ---------------------------------------------------------------------------

class TestJsonFileMappingStore:
    def test_save_and_get(self, tmp_path):
        store = JsonFileMappingStore(tmp_path / "mappings")
        result = _sample_result()
        rid = store.save(result)
        loaded = store.get(rid)
        assert loaded is not None
        assert loaded.anonymized_text == result.anonymized_text
        assert loaded.entity_mapping == result.entity_mapping

    def test_creates_directory(self, tmp_path):
        store_dir = tmp_path / "deep" / "nested" / "dir"
        store = JsonFileMappingStore(store_dir)
        store.save(_sample_result())
        assert store_dir.exists()

    def test_get_missing_returns_none(self, tmp_path):
        store = JsonFileMappingStore(tmp_path / "mappings")
        assert store.get("nonexistent") is None

    def test_delete(self, tmp_path):
        store = JsonFileMappingStore(tmp_path / "mappings")
        rid = store.save(_sample_result())
        assert store.delete(rid) is True
        assert store.get(rid) is None
        assert store.delete(rid) is False

    def test_len(self, tmp_path):
        store = JsonFileMappingStore(tmp_path / "mappings")
        assert len(store) == 0
        store.save(_sample_result())
        store.save(_sample_result())
        assert len(store) == 2

    def test_file_is_readable_json(self, tmp_path):
        store = JsonFileMappingStore(tmp_path / "mappings")
        rid = store.save(_sample_result())
        import json
        content = (tmp_path / "mappings" / f"{rid}.json").read_text()
        data = json.loads(content)
        assert "entity_mapping" in data
        assert data["entity_mapping"]["{{PERSON_1}}"] == "Rahul"
