"""Tests for pii_shield.session_store — ConversationState + reference stores."""

import pytest

from pii_shield.errors import SessionStoreError
from pii_shield.session_store import (
    ConversationState,
    InMemorySessionStore,
    SqliteSessionStore,
)


# ---------------------------------------------------------------------------
# ConversationState serialization
# ---------------------------------------------------------------------------


def test_conversation_state_roundtrip_tuple_keys():
    state = ConversationState(
        entity_mapping={"{{PERSON_1}}": "Rahul Sharma"},
        encrypt_mapping={"cipherX": "secret"},
        value_to_placeholder={("PERSON", "Rahul Sharma"): "{{PERSON_1}}"},
        type_counters={"PERSON": 1},
    )
    data = state.to_dict()
    # value_to_placeholder is flattened to triples for JSON safety
    assert data["value_to_placeholder"] == [["PERSON", "Rahul Sharma", "{{PERSON_1}}"]]

    rebuilt = ConversationState.from_dict(data)
    assert rebuilt.entity_mapping == state.entity_mapping
    assert rebuilt.value_to_placeholder == state.value_to_placeholder
    assert rebuilt.type_counters == {"PERSON": 1}


# ---------------------------------------------------------------------------
# Store CRUD (both reference implementations)
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemorySessionStore()
    else:
        s = SqliteSessionStore(tmp_path / "sess.db")
        yield s
        s.close()


def _state():
    return ConversationState(
        entity_mapping={"{{PERSON_1}}": "Rahul"},
        value_to_placeholder={("PERSON", "Rahul"): "{{PERSON_1}}"},
        type_counters={"PERSON": 1},
    )


def test_save_load_delete(store):
    assert store.load("c1") is None
    store.save("c1", _state())
    loaded = store.load("c1")
    assert loaded is not None
    assert loaded.entity_mapping == {"{{PERSON_1}}": "Rahul"}
    assert loaded.value_to_placeholder == {("PERSON", "Rahul"): "{{PERSON_1}}"}
    assert store.delete("c1") is True
    assert store.load("c1") is None
    assert store.delete("c1") is False


def test_overwrite(store):
    store.save("c1", _state())
    s2 = _state()
    s2.entity_mapping["{{PERSON_2}}"] = "Priya"
    store.save("c1", s2)
    assert store.load("c1").entity_mapping.get("{{PERSON_2}}") == "Priya"


def test_sqlite_recovers_if_file_deleted(tmp_path):
    """A deleted/rotated DB file must not permanently break the store."""
    db_path = tmp_path / "sess.db"
    store = SqliteSessionStore(db_path)
    store.save("c1", _state())
    # Simulate the file being removed out from under a long-running process.
    db_path.unlink()
    # The next write must succeed (schema/file recreated), not raise READONLY.
    store.save("c2", _state())
    assert store.load("c2") is not None
    store.close()


def test_ttl_expiry(store, monkeypatch):
    import pii_shield.session_store as ss

    clock = {"t": 1000.0}
    monkeypatch.setattr(ss.time, "time", lambda: clock["t"])

    store.save("c1", _state(), ttl=60)
    assert store.load("c1") is not None  # not yet expired
    clock["t"] = 1000.0 + 61
    assert store.load("c1") is None  # expired -> evicted


def test_no_ttl_persists(store, monkeypatch):
    import pii_shield.session_store as ss

    clock = {"t": 1000.0}
    monkeypatch.setattr(ss.time, "time", lambda: clock["t"])
    store.save("c1", _state(), ttl=None)
    clock["t"] = 1000.0 + 10_000_000
    assert store.load("c1") is not None


# ---------------------------------------------------------------------------
# ValueCodec (at-rest protection hook)
# ---------------------------------------------------------------------------


class _ReverseCodec:
    """Trivial reversible codec for testing the at-rest hook."""

    def encode(self, text: str) -> str:
        return text[::-1]

    def decode(self, token: str) -> str:
        return token[::-1]


def test_value_codec_applied_in_memory():
    store = InMemorySessionStore(value_codec=_ReverseCodec())
    store.save("c1", _state())
    # Raw stored blob is codec-encoded (reversed), not plain JSON.
    raw, _ = store._store["c1"]
    assert not raw.lstrip().startswith("{")
    # But load transparently decodes.
    assert store.load("c1").entity_mapping == {"{{PERSON_1}}": "Rahul"}


def test_value_codec_decode_failure_raises(tmp_path):
    class _Broken:
        def encode(self, text):
            return text

        def decode(self, token):
            raise RuntimeError("boom")

    store = InMemorySessionStore(value_codec=_Broken())
    store.save("c1", _state())
    with pytest.raises(SessionStoreError):
        store.load("c1")
