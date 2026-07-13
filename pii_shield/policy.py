"""Named, reusable anonymization policy for PII Shield.

``AnonymizationPolicy`` bundles the four independent "what/how to anonymize"
levers that are otherwise passed as separate per-call arguments:

* ``strategies``                — per-entity-type strategy (replace/hash/encrypt/fake)
* ``allow_list``                — exact terms never anonymized (any entity type)
* ``entity_type_allow_list``    — whole entity types passed through unmasked
* ``entity_keyword_allow_list`` — skip a value only when detected as a given type

Bundling them into one object lets a host application construct a policy once
(from code, a dict, or a YAML file), name it, store it, and reuse it across an
entire conversation — while keeping the door open to selecting a different
policy per user/tenant later without any structural change.

This module has no dependencies beyond the standard library and
``pii_shield.models`` (YAML is imported lazily only in :meth:`from_yaml`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pii_shield.models import EntityConfig, Strategy


@dataclass
class AnonymizationPolicy:
    """A reusable bundle of anonymization controls.

    Parameters
    ----------
    strategies :
        Mapping of ``entity_type -> strategy``.  Entities not listed default
        to ``"replace"``.
    allow_list :
        Exact terms to exclude from anonymization, regardless of entity type.
    entity_type_allow_list :
        Entity types to exclude from anonymization entirely.
    entity_keyword_allow_list :
        ``{entity_type: [keywords]}`` — skip a specific value only when it is
        detected as that entity type.
    entity_type_include_list :
        If non-empty, anonymize **only** these entity types (a positive filter,
        the inverse of ``entity_type_allow_list``); every other detected type is
        left untouched.
    score_threshold :
        Optional per-policy confidence override.  ``None`` uses the engine's
        default threshold.
    language :
        ISO 639-1 language code applied when the policy drives a call.
    """

    strategies: dict[str, Strategy] = field(default_factory=dict)
    allow_list: list[str] = field(default_factory=list)
    entity_type_allow_list: set[str] = field(default_factory=set)
    entity_keyword_allow_list: dict[str, list[str]] = field(default_factory=dict)
    entity_type_include_list: set[str] = field(default_factory=set)
    score_threshold: float | None = None
    language: str = "en"

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "AnonymizationPolicy":
        """Build a policy from a plain dict (e.g. parsed JSON/YAML)."""
        data = data or {}
        return cls(
            strategies=dict(data.get("strategies", {}) or {}),
            allow_list=list(data.get("allow_list", []) or []),
            entity_type_allow_list=set(data.get("entity_type_allow_list", []) or []),
            entity_keyword_allow_list={
                k: list(v)
                for k, v in (data.get("entity_keyword_allow_list", {}) or {}).items()
            },
            entity_type_include_list=set(data.get("entity_type_include_list", []) or []),
            score_threshold=data.get("score_threshold"),
            language=data.get("language", "en") or "en",
        )

    @classmethod
    def from_yaml(cls, path: str) -> "AnonymizationPolicy":
        """Load a policy from a YAML file.

        The YAML document may contain any subset of the policy fields.
        """
        import yaml

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"Policy YAML at {path!r} must be a mapping, got {type(data).__name__}"
            )
        return cls.from_dict(data)

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize the policy to a JSON/YAML-friendly dict."""
        return {
            "strategies": dict(self.strategies),
            "allow_list": list(self.allow_list),
            "entity_type_allow_list": sorted(self.entity_type_allow_list),
            "entity_keyword_allow_list": {
                k: list(v) for k, v in self.entity_keyword_allow_list.items()
            },
            "entity_type_include_list": sorted(self.entity_type_include_list),
            "score_threshold": self.score_threshold,
            "language": self.language,
        }

    def to_entity_config(self) -> EntityConfig:
        """Return an :class:`EntityConfig` carrying this policy's strategies."""
        return EntityConfig(strategies=dict(self.strategies))
