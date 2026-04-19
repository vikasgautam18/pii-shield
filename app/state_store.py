import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.redis_store import get_async_redis_client

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))  # 24 hours


@dataclass
class AnonymizationRecord:
    """A single anonymization session."""

    original_text: str
    anonymized_text: str
    entity_mapping: dict[str, str]
    app_id: str = ""
    hash_mapping: dict[str, str] = field(default_factory=dict)
    encrypt_mapping: dict[str, str] = field(default_factory=dict)


class AnonymizationStore:
    """Redis-backed store for anonymization sessions.

    Each call to ``/anonymize_unique`` creates a record stored as a Redis
    HASH under the key ``session:{uuid}``.  The ``/deanonymize`` endpoint
    retrieves the record to reverse placeholders.

    Keys are set with a configurable TTL (default 24 h, via
    ``SESSION_TTL_SECONDS`` env var).  Set to ``0`` to disable expiry.
    """

    @staticmethod
    def _key(record_id: str) -> str:
        return f"session:{record_id}"

    @staticmethod
    def new_id() -> str:
        """Generate a record ID without I/O (used before background save)."""
        return str(uuid.uuid4())

    async def save(self, record: AnonymizationRecord, record_id: str | None = None) -> str:
        """Persist a record and return its unique ID."""
        r = get_async_redis_client()
        record_id = record_id or self.new_id()
        key = self._key(record_id)
        pipe = r.pipeline(transaction=False)
        pipe.hset(key, mapping={
            "app_id": record.app_id,
            "original_text": record.original_text,
            "anonymized_text": record.anonymized_text,
            "entity_mapping": json.dumps(record.entity_mapping),
            "hash_mapping": json.dumps(record.hash_mapping),
            "encrypt_mapping": json.dumps(record.encrypt_mapping),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        if SESSION_TTL_SECONDS > 0:
            pipe.expire(key, SESSION_TTL_SECONDS)
        await pipe.execute()
        return record_id

    async def get(self, record_id: str) -> AnonymizationRecord | None:
        """Retrieve a record by ID, or None if not found."""
        r = get_async_redis_client()
        data = await r.hgetall(self._key(record_id))
        if not data:
            return None
        return AnonymizationRecord(
            original_text=data["original_text"],
            anonymized_text=data["anonymized_text"],
            entity_mapping=json.loads(data["entity_mapping"]),
            app_id=data.get("app_id", ""),
            hash_mapping=json.loads(data.get("hash_mapping", "{}")),
            encrypt_mapping=json.loads(data.get("encrypt_mapping", "{}")),
        )

    async def delete(self, record_id: str) -> bool:
        """Remove a record. Returns True if it existed."""
        r = get_async_redis_client()
        return await r.delete(self._key(record_id)) > 0


# Singleton instance used across the application
store = AnonymizationStore()
