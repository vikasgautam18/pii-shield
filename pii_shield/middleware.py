"""High-level middleware facade for embedding PII Shield in an LLM application.

:class:`PiiMiddleware` ties together the three long-lived pieces — a stateless
:class:`~pii_shield.engine.PiiShieldEngine`, a shared
:class:`~pii_shield.session_store.SessionStore`, and a global
:class:`~pii_shield.policy.AnonymizationPolicy` — behind a small, ergonomic API
keyed by ``conversation_id``:

    middleware = PiiMiddleware(policy=AnonymizationPolicy.from_yaml("policy.yml"))

    safe = middleware.anonymize("conv-1", user_message).anonymized_text  # → LLM
    answer = middleware.deanonymize("conv-1", llm_reply)                 # restore

    # streamed responses
    sd = middleware.streaming_deanonymizer("conv-1")
    for chunk in llm_stream:
        emit(sd.feed(chunk))
    emit(sd.flush())

Construct **one** ``PiiMiddleware`` per process and reuse it across all
conversations; the engine loads its NLP model once and is safe to share.

.. note::

   Turns of the *same* conversation should be processed sequentially (the
   natural request/response cadence of a chat).  The per-conversation state is
   loaded, extended, and saved on each call, so concurrent turns of one
   conversation could race; different conversations never contend.
"""

from __future__ import annotations

import asyncio

from pii_shield.conversation import ConversationAnonymizer
from pii_shield.engine import PiiShieldEngine
from pii_shield.models import AnonymizeResult
from pii_shield.observability import EventHook
from pii_shield.policy import AnonymizationPolicy
from pii_shield.session_store import InMemorySessionStore, SessionStore
from pii_shield.streaming import StreamingDeanonymizer


class PiiMiddleware:
    """Process-wide facade for conversation-aware anonymize/de-anonymize.

    Parameters
    ----------
    engine :
        Shared :class:`PiiShieldEngine`.  Created with defaults if omitted
        (loads the NLP model once — reuse this instance).
    store :
        A :class:`SessionStore` for conversation state.  Defaults to in-memory
        (single-process); supply a shared backend for multi-replica use.
    policy :
        Global :class:`AnonymizationPolicy` applied to every conversation.
    ttl :
        Optional lifetime (seconds) applied to stored conversation state.
    """

    def __init__(
        self,
        engine: PiiShieldEngine | None = None,
        store: SessionStore | None = None,
        policy: AnonymizationPolicy | None = None,
        ttl: int | None = None,
    ) -> None:
        self._engine = engine or PiiShieldEngine()
        self._store = store if store is not None else InMemorySessionStore()
        self._policy = policy or AnonymizationPolicy()
        self._ttl = ttl

    @property
    def engine(self) -> PiiShieldEngine:
        return self._engine

    @property
    def store(self) -> SessionStore:
        return self._store

    @property
    def policy(self) -> AnonymizationPolicy:
        return self._policy

    def conversation(self, conversation_id: str) -> ConversationAnonymizer:
        """Return an ephemeral :class:`ConversationAnonymizer` for *conversation_id*.

        Cheap to create — all durable state lives in the shared store.
        """
        return ConversationAnonymizer(
            self._engine,
            conversation_id,
            store=self._store,
            policy=self._policy,
            ttl=self._ttl,
        )

    # ------------------------------------------------------------------
    # Sync API
    # ------------------------------------------------------------------

    def anonymize(
        self,
        conversation_id: str,
        text: str,
        on_event: EventHook | None = None,
    ) -> AnonymizeResult:
        """Anonymize a turn of *conversation_id* (fails closed on detection error)."""
        return self.conversation(conversation_id).anonymize(text, on_event=on_event)

    def deanonymize(
        self,
        conversation_id: str,
        text: str,
        include_hashed: bool = False,
        include_encrypted: bool = True,
        on_event: EventHook | None = None,
    ) -> str:
        """Restore PII in *text* using *conversation_id*'s accumulated mapping."""
        return self.conversation(conversation_id).deanonymize(
            text,
            include_hashed=include_hashed,
            include_encrypted=include_encrypted,
            on_event=on_event,
        )

    def streaming_deanonymizer(
        self,
        conversation_id: str,
        include_hashed: bool = False,
        include_encrypted: bool = True,
    ) -> StreamingDeanonymizer:
        """Return a :class:`StreamingDeanonymizer` for a streamed LLM response."""
        return self.conversation(conversation_id).streaming_deanonymizer(
            include_hashed=include_hashed,
            include_encrypted=include_encrypted,
        )

    def clear(self, conversation_id: str) -> bool:
        """Delete all stored state for *conversation_id*."""
        return self._store.delete(conversation_id)

    # ------------------------------------------------------------------
    # Async helpers (offload CPU-bound NLP inference off the event loop)
    # ------------------------------------------------------------------

    async def anonymize_async(
        self,
        conversation_id: str,
        text: str,
        on_event: EventHook | None = None,
    ) -> AnonymizeResult:
        """Async wrapper around :meth:`anonymize` via a worker thread."""
        return await asyncio.to_thread(
            self.anonymize, conversation_id, text, on_event
        )

    async def deanonymize_async(
        self,
        conversation_id: str,
        text: str,
        include_hashed: bool = False,
        include_encrypted: bool = True,
        on_event: EventHook | None = None,
    ) -> str:
        """Async wrapper around :meth:`deanonymize` via a worker thread."""
        return await asyncio.to_thread(
            self.deanonymize,
            conversation_id,
            text,
            include_hashed,
            include_encrypted,
            on_event,
        )
