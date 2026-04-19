"""Per-entity-type anonymization strategy configuration.

Re-exports from pii_shield.operator_config for backward compatibility.
"""

from pii_shield.operator_config import (  # noqa: F401
    ENCRYPTION_BACKEND,
    Strategy,
    OperatorConfigStore,
    operator_config_store,
)
