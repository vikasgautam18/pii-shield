"""Local mapping stores for batch deanonymization.

These stores persist anonymization mappings without requiring Redis.
Use them when running PII Shield as a library for batch processing.

Three implementations:
- ``InMemoryMappingStore`` — dict-backed, for small batches
- ``SqliteMappingStore`` — SQLite-backed, for large batches / persistence
- ``JsonFileMappingStore`` — one JSON file per record, for debugging
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from pii_shield.models import AnonymizeResult


@runtime_checkable
class MappingStore(Protocol):
    """Protocol for anonymization mapping persistence."""

    def save(self, result: AnonymizeResult, record_id: str | None = None) -> str:
        """Persist a result and return its unique ID."""
        ...

    def get(self, record_id: str) -> AnonymizeResult | None:
        """Retrieve a result by ID, or None if not found."""
        ...

    def delete(self, record_id: str) -> bool:
        """Remove a record. Returns True if it existed."""
        ...


class InMemoryMappingStore:
    """Dict-backed mapping store — for small batches."""

    def __init__(self) -> None:
        self._store: dict[str, AnonymizeResult] = {}

    def save(self, result: AnonymizeResult, record_id: str | None = None) -> str:
        record_id = record_id or str(uuid.uuid4())
        self._store[record_id] = result
        return record_id

    def get(self, record_id: str) -> AnonymizeResult | None:
        return self._store.get(record_id)

    def delete(self, record_id: str) -> bool:
        if record_id in self._store:
            del self._store[record_id]
            return True
        return False

    def __len__(self) -> int:
        return len(self._store)


class SqliteMappingStore:
    """SQLite-backed mapping store — for large batches and persistence.

    Data survives process restarts.  Thread-safe (SQLite handles locking).
    """

    def __init__(self, db_path: str | Path = "pii_mappings.db") -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
                id TEXT PRIMARY KEY,
                anonymized_text TEXT NOT NULL,
                entity_mapping TEXT NOT NULL DEFAULT '{}',
                hash_mapping TEXT NOT NULL DEFAULT '{}',
                encrypt_mapping TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._conn.commit()

    def save(self, result: AnonymizeResult, record_id: str | None = None) -> str:
        record_id = record_id or str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT OR REPLACE INTO mappings
                (id, anonymized_text, entity_mapping, hash_mapping, encrypt_mapping)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                record_id,
                result.anonymized_text,
                json.dumps(result.entity_mapping),
                json.dumps(result.hash_mapping),
                json.dumps(result.encrypt_mapping),
            ),
        )
        self._conn.commit()
        return record_id

    def get(self, record_id: str) -> AnonymizeResult | None:
        row = self._conn.execute(
            "SELECT anonymized_text, entity_mapping, hash_mapping, encrypt_mapping "
            "FROM mappings WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        return AnonymizeResult(
            anonymized_text=row[0],
            entity_mapping=json.loads(row[1]),
            hash_mapping=json.loads(row[2]),
            encrypt_mapping=json.loads(row[3]),
        )

    def delete(self, record_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM mappings WHERE id = ?", (record_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM mappings").fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()


class JsonFileMappingStore:
    """File-backed mapping store — one JSON file per record.

    Useful for debugging and inspection of individual mappings.
    """

    def __init__(self, directory: str | Path = "./mappings") -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, record_id: str) -> Path:
        return self._dir / f"{record_id}.json"

    def save(self, result: AnonymizeResult, record_id: str | None = None) -> str:
        record_id = record_id or str(uuid.uuid4())
        data = {
            "anonymized_text": result.anonymized_text,
            "entity_mapping": result.entity_mapping,
            "hash_mapping": result.hash_mapping,
            "encrypt_mapping": result.encrypt_mapping,
        }
        self._path(record_id).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return record_id

    def get(self, record_id: str) -> AnonymizeResult | None:
        path = self._path(record_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return AnonymizeResult(
            anonymized_text=data["anonymized_text"],
            entity_mapping=data.get("entity_mapping", {}),
            hash_mapping=data.get("hash_mapping", {}),
            encrypt_mapping=data.get("encrypt_mapping", {}),
        )

    def delete(self, record_id: str) -> bool:
        path = self._path(record_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def __len__(self) -> int:
        return len(list(self._dir.glob("*.json")))
