"""Stateful, multi-turn conversation anonymizer.

:class:`ConversationAnonymizer` wraps a stateless
:class:`~pii_shield.engine.PiiShieldEngine` and a :class:`SessionStore` to keep
a **growing, stable** value→placeholder mapping across every turn of one
conversation.  The same PII value therefore maps to the **same placeholder**
on every turn (e.g. ``Rahul Sharma`` stays ``{{PERSON_1}}``), which preserves
coreference for the downstream LLM and lets a later turn's answer be
de-anonymized against the accumulated mapping.

Typical usage (host application owns the LLM call and the store)::

    engine = PiiShieldEngine()                 # process-wide singleton
    store = InMemorySessionStore()             # or your Redis/Postgres impl
    policy = AnonymizationPolicy()             # global policy

    conv = ConversationAnonymizer(engine, "conversation-123", store, policy)
    safe = conv.anonymize(user_message).anonymized_text   # → send to LLM
    answer = conv.deanonymize(llm_reply)                  # restore PII

The engine remains stateless and reusable across all conversations; all
per-conversation state lives in the pluggable store.
"""

from __future__ import annotations

import time
from collections import Counter

from pii_shield.engine import PiiShieldEngine
from pii_shield.models import AnonymizeResult, AnonymizeStats, DetectedEntity
from pii_shield.observability import Event, EventHook, emit
from pii_shield.operator_config import _DEFAULTS
from pii_shield.policy import AnonymizationPolicy
from pii_shield.session_store import (
    ConversationState,
    InMemorySessionStore,
    SessionStore,
)


class ConversationAnonymizer:
    """Anonymize/de-anonymize the turns of a single conversation consistently.

    Parameters
    ----------
    engine :
        A shared :class:`PiiShieldEngine` (create once, reuse everywhere).
    conversation_id :
        Stable identifier for this conversation; used as the store key.
    store :
        A :class:`SessionStore` implementation.  Defaults to an in-memory
        store (single-process only) — supply a shared store for multi-replica
        deployments.
    policy :
        The :class:`AnonymizationPolicy` applied to every turn.  Defaults to an
        empty policy (all entities ``replace``, no allow-lists).
    ttl :
        Optional lifetime (seconds) applied to the stored state on every save.
    """

    def __init__(
        self,
        engine: PiiShieldEngine,
        conversation_id: str,
        store: SessionStore | None = None,
        policy: AnonymizationPolicy | None = None,
        ttl: int | None = None,
    ) -> None:
        self._engine = engine
        self._conversation_id = conversation_id
        self._store = store if store is not None else InMemorySessionStore()
        self._policy = policy or AnonymizationPolicy()
        self._ttl = ttl

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def policy(self) -> AnonymizationPolicy:
        return self._policy

    # ------------------------------------------------------------------
    # Anonymize
    # ------------------------------------------------------------------

    def anonymize(
        self,
        text: str,
        on_event: EventHook | None = None,
    ) -> AnonymizeResult:
        """Anonymize one turn, reusing/extending the conversation's mapping.

        Returns an :class:`AnonymizeResult` whose mappings are the **full
        accumulated** conversation mappings (a superset each turn), so the
        caller can de-anonymize this turn's downstream output immediately.
        """
        t0 = time.perf_counter()
        state = self._store.load(self._conversation_id) or ConversationState()

        # Strategy lookup: engine defaults overlaid with the policy.
        strategies = dict(_DEFAULTS)
        strategies.update(self._policy.strategies)

        # Treat already-issued placeholders/fakes as opaque: add them to the
        # allow-list so a value the engine already emitted is never re-detected.
        allow_list = list(self._policy.allow_list)
        if state.entity_mapping:
            allow_list.extend(state.entity_mapping.keys())

        non_overlapping = self._engine._run_pipeline(
            text,
            self._policy.language,
            allow_list or None,
            self._policy.entity_type_allow_list or None,
            self._policy.entity_keyword_allow_list or None,
            score_threshold=self._policy.score_threshold,
            entity_type_include_list=self._policy.entity_type_include_list or None,
        )
        detect_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        sorted_results = sorted(non_overlapping, key=lambda r: r.start, reverse=True)
        replacements = self._engine._assign_replacements(
            text,
            sorted_results,
            strategies,
            type_counters=state.type_counters,
            value_to_placeholder=state.value_to_placeholder,
            entity_mapping=state.entity_mapping,
            hash_mapping=state.hash_mapping,
            encrypt_mapping=state.encrypt_mapping,
        )
        result_text = self._engine._build_text(text, replacements)
        anonymize_ms = (time.perf_counter() - t1) * 1000.0

        self._store.save(self._conversation_id, state, ttl=self._ttl)

        entities = sorted(
            [
                DetectedEntity(
                    entity_type=r.entity_type,
                    start=r.start,
                    end=r.end,
                    score=r.score,
                    text=text[r.start : r.end],
                )
                for r in non_overlapping
            ],
            key=lambda e: e.start,
        )
        entity_counts = dict(Counter(e.entity_type for e in entities))
        total_ms = (time.perf_counter() - t0) * 1000.0
        stats = AnonymizeStats(
            detect_ms=detect_ms,
            anonymize_ms=anonymize_ms,
            total_ms=total_ms,
            entity_count=len(entities),
            entity_counts=entity_counts,
        )

        emit(
            on_event,
            Event(
                name="anonymize",
                conversation_id=self._conversation_id,
                entity_count=len(entities),
                entity_counts=entity_counts,
                duration_ms=total_ms,
                detect_ms=detect_ms,
                anonymize_ms=anonymize_ms,
            ),
        )

        return AnonymizeResult(
            anonymized_text=result_text,
            entity_mapping=dict(state.entity_mapping),
            hash_mapping=dict(state.hash_mapping),
            encrypt_mapping=dict(state.encrypt_mapping),
            entities=entities,
            stats=stats,
        )

    # ------------------------------------------------------------------
    # De-anonymize
    # ------------------------------------------------------------------

    def deanonymize(
        self,
        text: str,
        include_hashed: bool = False,
        include_encrypted: bool = True,
        on_event: EventHook | None = None,
    ) -> str:
        """Restore original PII in *text* using the accumulated mapping.

        ``include_hashed`` defaults to ``False`` because hashing is a one-way
        strategy; ``include_encrypted`` defaults to ``True`` so encrypted values
        round-trip.  If the conversation has no stored state yet, *text* is
        returned unchanged.
        """
        t0 = time.perf_counter()
        state = self._store.load(self._conversation_id)
        if state is None:
            return text

        restored = self._engine.deanonymize(
            text,
            entity_mapping=state.entity_mapping,
            hash_mapping=state.hash_mapping if include_hashed else None,
            encrypt_mapping=state.encrypt_mapping if include_encrypted else None,
        )
        emit(
            on_event,
            Event(
                name="deanonymize",
                conversation_id=self._conversation_id,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            ),
        )
        return restored

    def streaming_deanonymizer(
        self,
        include_hashed: bool = False,
        include_encrypted: bool = True,
    ) -> "StreamingDeanonymizer":
        """Return a :class:`StreamingDeanonymizer` bound to this conversation's mapping.

        Use it to restore PII in a **streamed** LLM response without leaking
        placeholders split across chunk boundaries.
        """
        from pii_shield.streaming import StreamingDeanonymizer

        state = self._store.load(self._conversation_id) or ConversationState()
        return StreamingDeanonymizer(
            entity_mapping=state.entity_mapping,
            hash_mapping=state.hash_mapping if include_hashed else None,
            encrypt_mapping=state.encrypt_mapping if include_encrypted else None,
        )

    # ------------------------------------------------------------------
    # Introspection / lifecycle
    # ------------------------------------------------------------------

    @property
    def mapping(self) -> dict[str, str]:
        """Return a copy of the current accumulated placeholder→value mapping."""
        state = self._store.load(self._conversation_id)
        return dict(state.entity_mapping) if state else {}

    def clear(self) -> bool:
        """Delete all stored state for this conversation."""
        return self._store.delete(self._conversation_id)
