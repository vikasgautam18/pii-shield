"""Library data classes for PII Shield.

These are plain dataclasses (not Pydantic) so the library has no web-framework
dependency.  The FastAPI service layer in ``app/models.py`` defines its own
Pydantic request/response models.
"""

from dataclasses import dataclass, field
from typing import Literal

Strategy = Literal["replace", "hash", "encrypt", "fake"]


@dataclass
class EntityConfig:
    """Per-entity-type anonymization strategy configuration.

    Entities not listed in ``strategies`` default to ``"replace"``.
    """

    strategies: dict[str, Strategy] = field(default_factory=dict)


@dataclass
class DetectedEntity:
    """A single PII entity found in text."""

    entity_type: str
    start: int
    end: int
    score: float
    text: str


@dataclass
class AnonymizeResult:
    """Result of anonymizing a single text string."""

    anonymized_text: str
    entity_mapping: dict[str, str] = field(default_factory=dict)
    hash_mapping: dict[str, str] = field(default_factory=dict)
    encrypt_mapping: dict[str, str] = field(default_factory=dict)
    entities: list[DetectedEntity] = field(default_factory=list)
