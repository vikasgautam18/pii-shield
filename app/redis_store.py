"""Redis-backed application registry.

Stores registered applications and their per-entity PII strategy
preferences. Connects to an external Redis server via the REDIS_URL
environment variable (default: redis://localhost:6379/0).

Key schema — app registry:
  app:{app_id}:name   → string  (application name)
  app:{app_id}:config → JSON    (entity_type → strategy map)
  apps                → set     (all registered app IDs)

Key schema — anonymization sessions:
  session:{uuid}      → hash    (see state_store.py for field details)
"""

import json
import logging
import os
import uuid

import redis
import redis.asyncio as aioredis
from presidio_anonymizer.entities import OperatorConfig

from pii_shield.operator_config import Strategy, operator_config_store, ENCRYPTION_BACKEND

logger = logging.getLogger("pii-shield")

_encrypt_operator = (
    "pqc_encrypt" if ENCRYPTION_BACKEND == "pqc" else "fernet_encrypt"
)

_OPERATOR_CONFIG_MAP: dict[str, OperatorConfig] = {
    "replace": OperatorConfig("replace"),
    "hash": OperatorConfig("sha3_hash"),
    "encrypt": OperatorConfig(_encrypt_operator),
    "fake": OperatorConfig("fake_data"),
}

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_AUTH_MODE = os.getenv("REDIS_AUTH_MODE", "key").lower()  # "key" or "entra"
REDIS_POOL_SIZE = int(os.getenv("REDIS_POOL_SIZE", "20"))

# Entra connection config
_ENTRA_HOST = os.getenv("REDIS_HOST", "")
_ENTRA_PORT = int(os.getenv("REDIS_PORT", "6380"))
_ENTRA_USERNAME = os.getenv("REDIS_ENTRA_USERNAME", "")


class _EntraCredentialProvider(redis.credentials.CredentialProvider):
    """Credential provider for Azure Entra ID authentication with Redis.

    Called by redis-py's ConnectionPool when creating or reconnecting a
    connection — NOT on every command.  ``DefaultAzureCredential`` caches
    tokens internally (~55 min), so ``get_token()`` is near-free after
    the first call.
    """

    _SCOPE = "https://redis.azure.com/.default"

    def __init__(self, username: str = ""):
        self._username = username
        self._credential = None

    def _ensure_credential(self):
        if self._credential is None:
            from azure.identity import DefaultAzureCredential
            self._credential = DefaultAzureCredential()
            logger.info("Redis Entra auth: DefaultAzureCredential initialized")

    def get_credentials(self):
        self._ensure_credential()
        token = self._credential.get_token(self._SCOPE).token
        if self._username:
            return self._username, token
        return (token,)


if REDIS_AUTH_MODE == "key":
    logger.info("Redis connection: access key auth (pool=%d) → %s", REDIS_POOL_SIZE, REDIS_URL.split("@")[-1] if "@" in REDIS_URL else REDIS_URL)
    _pool = redis.ConnectionPool.from_url(REDIS_URL, decode_responses=True, max_connections=REDIS_POOL_SIZE)
    _async_pool = aioredis.ConnectionPool.from_url(REDIS_URL, decode_responses=True, max_connections=REDIS_POOL_SIZE)
else:
    logger.info("Redis connection: Entra auth (pool=%d) → %s:%d (TLS), user=%s", REDIS_POOL_SIZE, _ENTRA_HOST, _ENTRA_PORT, _ENTRA_USERNAME[:8] if _ENTRA_USERNAME else "(auto)")
    _cred_provider = _EntraCredentialProvider(_ENTRA_USERNAME)
    _pool = redis.ConnectionPool(
        host=_ENTRA_HOST, port=_ENTRA_PORT, db=0,
        credential_provider=_cred_provider,
        connection_class=redis.SSLConnection,
        max_connections=REDIS_POOL_SIZE,
        decode_responses=True,
    )
    _async_pool = aioredis.ConnectionPool(
        host=_ENTRA_HOST, port=_ENTRA_PORT, db=0,
        credential_provider=_cred_provider,
        connection_class=aioredis.connection.SSLConnection,
        max_connections=REDIS_POOL_SIZE,
        decode_responses=True,
    )


def _get_redis() -> redis.Redis:
    return redis.Redis(connection_pool=_pool)


def get_redis_client() -> redis.Redis:
    """Return the sync Redis client. Exposed so tests can monkey-patch it."""
    return _get_redis()


def _get_async_redis() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=_async_pool)


def get_async_redis_client() -> aioredis.Redis:
    """Return the async Redis client. Exposed so tests can monkey-patch it."""
    return _get_async_redis()


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


def register_app(app_name: str, r: redis.Redis | None = None) -> dict:
    """Register a new application and return its details."""
    r = r or get_redis_client()
    app_id = str(uuid.uuid4())
    # Seed with the current global defaults so the app starts with sensible config
    default_config = operator_config_store.get_all()
    r.set(f"app:{app_id}:name", app_name)
    r.set(f"app:{app_id}:config", json.dumps(default_config))
    r.sadd("apps", app_id)
    return {"app_id": app_id, "app_name": app_name, "config": default_config}


def get_app(app_id: str, r: redis.Redis | None = None) -> dict | None:
    """Return app details or None if not found."""
    r = r or get_redis_client()
    name = r.get(f"app:{app_id}:name")
    if name is None:
        return None
    config_raw = r.get(f"app:{app_id}:config") or "{}"
    return {
        "app_id": app_id,
        "app_name": name,
        "config": json.loads(config_raw),
    }


def count_apps(r: redis.Redis | None = None) -> int:
    """Return the number of currently registered apps."""
    r = r or get_redis_client()
    return r.scard("apps")


def list_apps(r: redis.Redis | None = None) -> list[dict]:
    """Return a list of all registered apps."""
    r = r or get_redis_client()
    app_ids = r.smembers("apps")
    apps = []
    for app_id in sorted(app_ids):
        app = get_app(app_id, r)
        if app is not None:
            apps.append(app)
    return apps


def update_app_config(
    app_id: str, entity_type: str, strategy: Strategy, r: redis.Redis | None = None
) -> dict | None:
    """Update a single entity strategy for an app. Returns updated details or None."""
    r = r or get_redis_client()
    if not r.exists(f"app:{app_id}:name"):
        return None
    config_raw = r.get(f"app:{app_id}:config") or "{}"
    config: dict = json.loads(config_raw)
    config[entity_type] = strategy
    r.set(f"app:{app_id}:config", json.dumps(config))
    return get_app(app_id, r)


def delete_app(app_id: str, r: redis.Redis | None = None) -> bool:
    """Delete an app. Returns True if it existed."""
    r = r or get_redis_client()
    if not r.exists(f"app:{app_id}:name"):
        return False
    r.delete(
        f"app:{app_id}:name",
        f"app:{app_id}:config",
        f"app:{app_id}:allow_list",
        f"app:{app_id}:entity_type_allow_list",
        f"app:{app_id}:entity_keyword_allow_list",
    )
    r.srem("apps", app_id)
    return True


# ---------------------------------------------------------------------------
# Per-app allow-list
# ---------------------------------------------------------------------------


def get_app_allow_list(app_id: str, r: redis.Redis | None = None) -> list[str]:
    """Return the allow-list for an app (terms excluded from anonymization)."""
    r = r or get_redis_client()
    raw = r.get(f"app:{app_id}:allow_list")
    if raw is None:
        return []
    return json.loads(raw)


def set_app_allow_list(
    app_id: str, allow_list: list[str], r: redis.Redis | None = None
) -> list[str]:
    """Set the allow-list for an app. Returns the stored list or None if app not found."""
    r = r or get_redis_client()
    if not r.exists(f"app:{app_id}:name"):
        return None
    r.set(f"app:{app_id}:allow_list", json.dumps(allow_list))
    return allow_list


# ---------------------------------------------------------------------------
# Per-app entity-type allow-list
# ---------------------------------------------------------------------------


def get_app_entity_type_allow_list(app_id: str, r: redis.Redis | None = None) -> list[str]:
    """Return the entity-type allow-list for an app (entity types excluded from anonymization)."""
    r = r or get_redis_client()
    raw = r.get(f"app:{app_id}:entity_type_allow_list")
    if raw is None:
        return []
    return json.loads(raw)


def set_app_entity_type_allow_list(
    app_id: str, entity_type_allow_list: list[str], r: redis.Redis | None = None
) -> list[str] | None:
    """Set the entity-type allow-list for an app. Returns the stored list or None if app not found."""
    r = r or get_redis_client()
    if not r.exists(f"app:{app_id}:name"):
        return None
    r.set(f"app:{app_id}:entity_type_allow_list", json.dumps(entity_type_allow_list))
    return entity_type_allow_list


def get_app_entity_keyword_allow_list(app_id: str, r: redis.Redis | None = None) -> dict[str, list[str]]:
    """Return the entity-keyword allow-list for an app.

    Format: ``{entity_type: [keyword1, keyword2, ...]}``
    Skips a specific detected text only when matched as a specific entity type.
    """
    r = r or get_redis_client()
    raw = r.get(f"app:{app_id}:entity_keyword_allow_list")
    if raw is None:
        return {}
    return json.loads(raw)


def set_app_entity_keyword_allow_list(
    app_id: str, entity_keyword_allow_list: dict[str, list[str]], r: redis.Redis | None = None
) -> dict[str, list[str]] | None:
    """Set the entity-keyword allow-list for an app. Returns the stored dict or None if app not found."""
    r = r or get_redis_client()
    if not r.exists(f"app:{app_id}:name"):
        return None
    r.set(f"app:{app_id}:entity_keyword_allow_list", json.dumps(entity_keyword_allow_list))
    return entity_keyword_allow_list


# ---------------------------------------------------------------------------
# Operator helpers
# ---------------------------------------------------------------------------


def build_operators_for_app(
    app_id: str, entity_types: list[str], r: redis.Redis | None = None
) -> dict[str, OperatorConfig]:
    """Build a Presidio operators dict using the app's config merged over global defaults."""
    r = r or get_redis_client()
    # Start with global defaults
    global_config = operator_config_store.get_all()

    # Overlay app-specific overrides
    config_raw = r.get(f"app:{app_id}:config") or "{}"
    app_config: dict = json.loads(config_raw)
    merged = {**global_config, **app_config}

    operators: dict[str, OperatorConfig] = {}
    for et in entity_types:
        strategy = merged.get(et, "replace")
        operators[et] = _OPERATOR_CONFIG_MAP.get(strategy, _OPERATOR_CONFIG_MAP["replace"])
    return operators


def get_app_strategy(app_id: str, entity_type: str, r: redis.Redis | None = None) -> Strategy:
    """Return the strategy for an entity type under an app's config, falling back to global."""
    r = r or get_redis_client()
    config_raw = r.get(f"app:{app_id}:config") or "{}"
    app_config: dict = json.loads(config_raw)
    if entity_type in app_config:
        return app_config[entity_type]
    return operator_config_store.get_strategy(entity_type)
