"""Lightweight observability primitives for PII Shield (no OTel dependency).

Two complementary flavours, both keeping the core library free of any
telemetry framework:

* **Return-stats (pull)** — :class:`~pii_shield.models.AnonymizeStats` is
  attached to every :class:`~pii_shield.models.AnonymizeResult`.  The caller
  reads timings/counts and forwards them to its own telemetry.
* **Callback (push)** — pass an ``on_event`` callable; the library invokes it
  with a plain :class:`Event` at the end of an operation.  The callable maps
  the event onto OpenTelemetry / StatsD / logs.

The library never imports ``opentelemetry`` — the ``Event`` is just data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("pii-shield")

# The high-level steps of one anonymize() call, in execution order.  They are exclusive (they never overlap), so
# the time of a call is their sum plus what the call does between them.  Dashboards, the Server-Timing header
# and the tests rely on these names: add one only together with everything that reads it.
LIBRARY_STEPS = ("nlp_pass1", "analyze", "recovery_pass2", "post_processing", "handling")

# Inclusive sub-measures of a step, reported as "<step>.<detail>": time inside the NER model, in the context
# scoring and in duplicate removal, summed over what runs inside that step.
DETAIL_NAMES = ("ner_model", "context_enhance", "dedup")


@dataclass
class Event:
    """A single observability event emitted by an anonymize/deanonymize call."""

    name: str  # "anonymize" | "deanonymize"
    conversation_id: str | None = None
    entity_count: int = 0
    entity_counts: dict[str, int] = field(default_factory=dict)
    duration_ms: float = 0.0
    detect_ms: float = 0.0
    anonymize_ms: float = 0.0
    nlp_ms: float = 0.0
    recovery_ms: float = 0.0
    recovery_pass: bool = False
    steps: dict[str, float] = field(default_factory=dict)  # as AnonymizeStats.steps


EventHook = Callable[[Event], None]


def emit(hook: EventHook | None, event: Event) -> None:
    """Invoke *hook* with *event*, swallowing any error.

    Telemetry must never break the anonymization hot path (which the host may
    rely on to fail *closed*), so a raising hook is logged and ignored.
    """
    if hook is None:
        return
    try:
        hook(event)
    except Exception:
        logger.warning("on_event hook raised; ignoring", exc_info=True)
