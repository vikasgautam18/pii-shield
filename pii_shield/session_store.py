"""Conversation/session state persistence for stateful multi-turn anonymization.

The stateful :class:`~pii_shield.conversation.ConversationAnonymizer` keeps a
**growing, stable** value→placeholder mapping across the turns of a single
conversation, so that (for example) the same person is always ``{{PERSON_1}}``.
That accumulated mapping lives in a :class:`ConversationState` and is persisted
through a :class:`SessionStore`.

.. warning::

   A :class:`ConversationState` contains the **original PII in plaintext** —
   that is exactly what ``deanonymize()`` looks up.  Any backing store is
   therefore a PII vault and MUST be protected accordingly (network isolation,
   encryption at rest, short TTLs).  Pass a :class:`ValueCodec` to the
   reference stores to transparently encrypt the serialized state at rest
   (e.g. using PII Shield's Fernet or PQC operators).

The core library ships two reference implementations — :class:`InMemorySessionStore`
and :class:`SqliteSessionStore`.  For a multi-process / multi-replica
deployment, implement the :class:`SessionStore` Protocol against your own
shared backend (Redis, Postgres, Cosmos DB, …); the library intentionally has
no hard dependency on any of them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from pii_shield.errors import SessionStoreError


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------


@dataclass
class ConversationState:
    """Accumulated anonymization state for one conversation.

    Contains everything needed to (a) keep placeholders consistent across
    turns and (b) reverse them later:

    * ``entity_mapping``       — placeholder / fake value → original PII
    * ``hash_mapping``         — sha3 hash → original PII (one-way; informational)
    * ``encrypt_mapping``      — ciphertext token → original PII
    * ``value_to_placeholder`` — ``(entity_type, value)`` → placeholder, so a
                                 repeated value reuses the same placeholder
    * ``type_counters``        — per-entity-type running counter (numbering)
    """

    entity_mapping: dict[str, str] = field(default_factory=dict)
    hash_mapping: dict[str, str] = field(default_factory=dict)
    encrypt_mapping: dict[str, str] = field(default_factory=dict)
    value_to_placeholder: dict[tuple[str, str], str] = field(default_factory=dict)
    type_counters: dict[str, int] = field(default_factory=dict)
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict (tuple keys are flattened to triples)."""
        return {
            "entity_mapping": dict(self.entity_mapping),
            "hash_mapping": dict(self.hash_mapping),
            "encrypt_mapping": dict(self.encrypt_mapping),
            "value_to_placeholder": [
                [et, val, ph]
                for (et, val), ph in self.value_to_placeholder.items()
            ],
            "type_counters": dict(self.type_counters),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationState":
        """Rebuild a state from its :meth:`to_dict` form."""
        v2p = {
            (row[0], row[1]): row[2]
            for row in data.get("value_to_placeholder", [])
        }
        return cls(
            entity_mapping=dict(data.get("entity_mapping", {})),
            hash_mapping=dict(data.get("hash_mapping", {})),
            encrypt_mapping=dict(data.get("encrypt_mapping", {})),
            value_to_placeholder=v2p,
            type_counters=dict(data.get("type_counters", {})),
            created_at=data.get("created_at", _utcnow_iso()),
            updated_at=data.get("updated_at", _utcnow_iso()),
        )


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ValueCodec(Protocol):
    """Reversible codec used to protect serialized state at rest (optional).

    ``encode`` is applied to the JSON string before persistence; ``decode``
    reverses it on load.  Wire this to a symmetric cipher (e.g. Fernet) or to
    PII Shield's PQC operator for at-rest confidentiality.
    """

    def encode(self, text: str) -> str: ...

    def decode(self, token: str) -> str: ...


@runtime_checkable
class SessionStore(Protocol):
    """Persistence Protocol for per-conversation :class:`ConversationState`.

    Implement this against any shared backend for multi-process deployments.
    ``ttl`` is an optional lifetime in **seconds**; ``None`` means no expiry.
    """

    def load(self, conversation_id: str) -> ConversationState | None:
        """Return the stored state for *conversation_id*, or ``None``."""
        ...

    def save(
        self,
        conversation_id: str,
        state: ConversationState,
        ttl: int | None = None,
    ) -> None:
        """Persist *state* for *conversation_id* with an optional TTL (seconds)."""
        ...

    def delete(self, conversation_id: str) -> bool:
        """Remove a conversation's state.  Returns ``True`` if it existed."""
        ...


# ---------------------------------------------------------------------------
# Reference implementations
# ---------------------------------------------------------------------------


class InMemorySessionStore:
    """Dict-backed session store — single-process use, tests, and notebooks.

    State is stored as a serialized (and optionally :class:`ValueCodec`-encoded)
    blob so behaviour matches the persistent stores exactly.  Not shared across
    processes — use a distributed backend for multi-replica deployments.
    """

    def __init__(self, value_codec: ValueCodec | None = None) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._codec = value_codec
        self._lock = threading.Lock()

    def _encode(self, state: ConversationState) -> str:
        blob = json.dumps(state.to_dict())
        return self._codec.encode(blob) if self._codec else blob

    def _decode(self, raw: str) -> ConversationState:
        blob = self._codec.decode(raw) if self._codec else raw
        return ConversationState.from_dict(json.loads(blob))

    def load(self, conversation_id: str) -> ConversationState | None:
        with self._lock:
            entry = self._store.get(conversation_id)
            if entry is None:
                return None
            raw, expires_at = entry
            if expires_at is not None and time.time() > expires_at:
                self._store.pop(conversation_id, None)
                return None
        try:
            return self._decode(raw)
        except Exception as exc:
            raise SessionStoreError(
                f"Failed to decode session state for {conversation_id!r}: {exc}"
            ) from exc

    def save(
        self,
        conversation_id: str,
        state: ConversationState,
        ttl: int | None = None,
    ) -> None:
        state.updated_at = _utcnow_iso()
        try:
            raw = self._encode(state)
        except Exception as exc:
            raise SessionStoreError(
                f"Failed to encode session state for {conversation_id!r}: {exc}"
            ) from exc
        expires_at = time.time() + ttl if ttl and ttl > 0 else None
        with self._lock:
            self._store[conversation_id] = (raw, expires_at)

    def delete(self, conversation_id: str) -> bool:
        with self._lock:
            return self._store.pop(conversation_id, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


class SqliteSessionStore:
    """SQLite-backed session store — survives restarts, safe across threads.

    Uses a **short-lived connection per operation** rather than one long-lived
    connection.  This is the robust pattern for embedded/interactive use
    (Streamlit, multi-threaded servers): it avoids cross-thread connection
    affinity issues and self-heals if the database file is deleted, rotated, or
    moved out from under a long-running process (the table is recreated on the
    next write).  For multi-process / multi-replica deployments, back the
    :class:`SessionStore` Protocol with a networked store instead.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            state           TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            expires_at      REAL
        )
    """

    def __init__(
        self,
        db_path: str | Path = "pii_sessions.db",
        value_codec: ValueCodec | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._db_path = str(db_path)
        self._codec = value_codec
        self._timeout = timeout
        self._lock = threading.Lock()
        # Create the schema up front (best effort).
        with self._connect() as conn:
            conn.execute(self._SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection with the schema ensured."""
        conn = sqlite3.connect(
            self._db_path, timeout=self._timeout, check_same_thread=False
        )
        conn.execute(self._SCHEMA)
        return conn

    def _encode(self, state: ConversationState) -> str:
        blob = json.dumps(state.to_dict())
        return self._codec.encode(blob) if self._codec else blob

    def _decode(self, raw: str) -> ConversationState:
        blob = self._codec.decode(raw) if self._codec else raw
        return ConversationState.from_dict(json.loads(blob))

    def load(self, conversation_id: str) -> ConversationState | None:
        with self._lock:
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT state, expires_at FROM conversations WHERE conversation_id = ?",
                        (conversation_id,),
                    ).fetchone()
                    if row is None:
                        return None
                    raw, expires_at = row
                    if expires_at is not None and time.time() > expires_at:
                        conn.execute(
                            "DELETE FROM conversations WHERE conversation_id = ?",
                            (conversation_id,),
                        )
                        conn.commit()
                        return None
            except sqlite3.Error as exc:
                raise SessionStoreError(
                    f"Failed to load session state for {conversation_id!r}: {exc}"
                ) from exc
        try:
            return self._decode(raw)
        except Exception as exc:
            raise SessionStoreError(
                f"Failed to decode session state for {conversation_id!r}: {exc}"
            ) from exc

    def save(
        self,
        conversation_id: str,
        state: ConversationState,
        ttl: int | None = None,
    ) -> None:
        state.updated_at = _utcnow_iso()
        try:
            raw = self._encode(state)
        except Exception as exc:
            raise SessionStoreError(
                f"Failed to encode session state for {conversation_id!r}: {exc}"
            ) from exc
        expires_at = time.time() + ttl if ttl and ttl > 0 else None
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO conversations
                            (conversation_id, state, updated_at, expires_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (conversation_id, raw, state.updated_at, expires_at),
                    )
                    conn.commit()
            except sqlite3.Error as exc:
                raise SessionStoreError(
                    f"Failed to save session state for {conversation_id!r}: {exc}"
                ) from exc

    def delete(self, conversation_id: str) -> bool:
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        "DELETE FROM conversations WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                    conn.commit()
                    return cur.rowcount > 0
            except sqlite3.Error as exc:
                raise SessionStoreError(
                    f"Failed to delete session state for {conversation_id!r}: {exc}"
                ) from exc

    def close(self) -> None:
        """No-op — connections are short-lived and closed after each operation."""
        return None
