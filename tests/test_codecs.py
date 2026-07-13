"""Tests for pii_shield.codecs.FernetValueCodec and its use in a SessionStore."""

import pytest

from pii_shield.codecs import FernetValueCodec
from pii_shield.session_store import (
    ConversationState,
    InMemorySessionStore,
    SqliteSessionStore,
)


def _state():
    return ConversationState(
        entity_mapping={"{{PERSON_1}}": "Rahul Sharma"},
        value_to_placeholder={("PERSON", "Rahul Sharma"): "{{PERSON_1}}"},
        type_counters={"PERSON": 1},
    )


def test_generate_key_and_roundtrip():
    codec = FernetValueCodec(FernetValueCodec.generate_key())
    token = codec.encode("Rahul Sharma")
    assert token != "Rahul Sharma"
    assert codec.decode(token) == "Rahul Sharma"


def test_accepts_bytes_key():
    key = FernetValueCodec.generate_key().encode("ascii")
    codec = FernetValueCodec(key)
    assert codec.decode(codec.encode("hi")) == "hi"


def test_encrypts_state_at_rest_in_memory():
    codec = FernetValueCodec(FernetValueCodec.generate_key())
    store = InMemorySessionStore(value_codec=codec)
    store.save("c1", _state())
    raw, _ = store._store["c1"]
    # The stored blob must not contain the plaintext PII.
    assert "Rahul Sharma" not in raw
    assert store.load("c1").entity_mapping == {"{{PERSON_1}}": "Rahul Sharma"}


def test_encrypts_state_at_rest_sqlite(tmp_path):
    import sqlite3

    codec = FernetValueCodec(FernetValueCodec.generate_key())
    db_path = tmp_path / "s.db"
    store = SqliteSessionStore(db_path, value_codec=codec)
    store.save("c1", _state())
    # Read the raw row directly — must be ciphertext, not plaintext PII.
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT state FROM conversations WHERE conversation_id = ?", ("c1",)
        ).fetchone()
    assert "Rahul Sharma" not in row[0]
    assert store.load("c1").entity_mapping["{{PERSON_1}}"] == "Rahul Sharma"
    store.close()


def test_wrong_key_cannot_decrypt(tmp_path):
    from pii_shield.errors import SessionStoreError

    codec_a = FernetValueCodec(FernetValueCodec.generate_key())
    store = InMemorySessionStore(value_codec=codec_a)
    store.save("c1", _state())
    # Swap in a different key -> decode fails -> SessionStoreError.
    store._codec = FernetValueCodec(FernetValueCodec.generate_key())
    with pytest.raises(SessionStoreError):
        store.load("c1")
