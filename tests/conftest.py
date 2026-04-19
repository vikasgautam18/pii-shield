"""Shared fixtures for the PII Shield test suite."""

import fakeredis
import fakeredis.aioredis
import pytest

from app import redis_store


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """Replace both sync and async Redis clients with fakeredis for all tests.

    A shared FakeServer ensures sync admin endpoints and async hot-path
    endpoints see the same data.
    """
    server = fakeredis.FakeServer()
    fake_sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(redis_store, "get_redis_client", lambda: fake_sync)

    # Create a fresh async client per call so it binds to the current event loop.
    # Patch everywhere the function is imported by name (Python import aliasing).
    async_factory = lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(redis_store, "get_async_redis_client", async_factory)
    monkeypatch.setattr("app.main.get_async_redis_client", async_factory)
    monkeypatch.setattr("app.state_store.get_async_redis_client", async_factory)

    # Clear the in-memory app cache to prevent stale data leaking between tests
    from app.main import _app_cache
    _app_cache.clear()

    yield fake_sync
    fake_sync.flushall()
    _app_cache.clear()
