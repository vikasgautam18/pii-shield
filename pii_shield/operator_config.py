"""Per-entity-type anonymization strategy configuration.

Stores a mapping of entity type → strategy ("replace", "hash", or "encrypt").
Entities not explicitly configured default to "replace".

The "encrypt" strategy routes to either the PQC backend (ML-KEM-768 + AES-256-GCM)
or the legacy Fernet backend (AES-128-CBC + HMAC-SHA256) based on the
ENCRYPTION_BACKEND environment variable (default: "pqc").
"""

import os
import threading
from typing import Literal

from presidio_anonymizer.entities import OperatorConfig

Strategy = Literal["replace", "hash", "encrypt", "fake"]

ENCRYPTION_BACKEND = os.getenv("ENCRYPTION_BACKEND", "pqc").lower()

_DEFAULTS: dict[str, Strategy] = {
    "IN_DRIVING_LICENSE": "replace",
    "IN_PIN_CODE": "replace",
}

_encrypt_operator = (
    "pqc_encrypt" if ENCRYPTION_BACKEND == "pqc" else "fernet_encrypt"
)

_OPERATOR_CONFIG_MAP: dict[Strategy, OperatorConfig] = {
    "replace": OperatorConfig("replace"),
    "hash": OperatorConfig("sha3_hash"),
    "encrypt": OperatorConfig(_encrypt_operator),
    "fake": OperatorConfig("fake_data"),
}


class OperatorConfigStore:
    """Thread-safe in-memory store for per-entity anonymization strategies."""

    def __init__(self) -> None:
        self._config: dict[str, Strategy] = dict(_DEFAULTS)
        self._lock = threading.Lock()

    def get_strategy(self, entity_type: str) -> Strategy:
        with self._lock:
            return self._config.get(entity_type, "replace")

    def get_all(self) -> dict[str, Strategy]:
        with self._lock:
            return dict(self._config)

    def set_strategy(self, entity_type: str, strategy: Strategy) -> None:
        with self._lock:
            if strategy == "replace" and entity_type not in _DEFAULTS:
                self._config.pop(entity_type, None)
            else:
                self._config[entity_type] = strategy

    def build_operators(self, entity_types: list[str]) -> dict[str, OperatorConfig]:
        """Build a Presidio operators dict for the given entity types."""
        with self._lock:
            operators: dict[str, OperatorConfig] = {}
            for et in entity_types:
                strategy = self._config.get(et, "replace")
                operators[et] = _OPERATOR_CONFIG_MAP[strategy]
            return operators


operator_config_store = OperatorConfigStore()
