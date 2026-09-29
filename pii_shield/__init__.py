# pii_shield — PII detection and anonymization library

from pii_shield.batch import BatchProcessor, BatchResult
from pii_shield.codecs import FernetValueCodec
from pii_shield.conversation import ConversationAnonymizer
from pii_shield.engine import PiiShieldEngine
from pii_shield.errors import (
    DeanonymizationError,
    DetectionError,
    InvalidInputError,
    PiiShieldError,
    SessionStoreError,
)
from pii_shield.mapping_store import (
    InMemoryMappingStore,
    JsonFileMappingStore,
    MappingStore,
    SqliteMappingStore,
)
from pii_shield.middleware import PiiMiddleware
from pii_shield.models import (
    AnonymizeResult,
    AnonymizeStats,
    DetectedEntity,
    EntityConfig,
)
from pii_shield.observability import Event, EventHook
from pii_shield.policy import AnonymizationPolicy
from pii_shield.session_store import (
    ConversationState,
    InMemorySessionStore,
    SessionStore,
    SqliteSessionStore,
    ValueCodec,
)
from pii_shield.streaming import StreamingDeanonymizer

__all__ = [
    # Engine & batch
    "PiiShieldEngine",
    "BatchProcessor",
    "BatchResult",
    # Conversation / middleware (stateful, multi-turn)
    "PiiMiddleware",
    "ConversationAnonymizer",
    "AnonymizationPolicy",
    "StreamingDeanonymizer",
    # Session (conversation) stores
    "SessionStore",
    "ConversationState",
    "InMemorySessionStore",
    "SqliteSessionStore",
    "ValueCodec",
    "FernetValueCodec",
    # Per-call mapping stores (batch)
    "MappingStore",
    "InMemoryMappingStore",
    "SqliteMappingStore",
    "JsonFileMappingStore",
    # Data models
    "AnonymizeResult",
    "AnonymizeStats",
    "DetectedEntity",
    "EntityConfig",
    # Observability
    "Event",
    "EventHook",
    # Errors
    "PiiShieldError",
    "DetectionError",
    "InvalidInputError",
    "DeanonymizationError",
    "SessionStoreError",
]
