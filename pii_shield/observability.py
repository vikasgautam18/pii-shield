"""Lightweight observability primitives for PII Shield (no OTel dependency).

Two complementary flavours, both keeping the core library free of any
telemetry framework:

* **Return-stats (pull)** — :class:`~pii_shield.models.AnonymizeStats` is
  attached to every :class:`~pii_shield.models.AnonymizeResult`.  The caller
  reads timings/counts and forwards them to its own telemetry.
* **Callback (push)** — pass an ``on_event`` callable; the library invokes it
  with a plain :class:`Event` at the end of an operation.  The callable maps
  the event onto OpenTelemetry / StatsD / logs.

Where the time of one call went is measured by a :class:`StepRecorder`, which
only collects floats and counts that end up in those two carriers.

The library never imports ``opentelemetry`` — the ``Event`` is just data.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
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

_now = time.perf_counter


class _Lap:
    """Times a ``with`` block; ``ms`` holds the duration once the block is over, also when it raised.

    A class rather than a ``@contextmanager`` generator, which costs about twice as much per use.
    """

    __slots__ = ("_start", "ms")

    def __init__(self) -> None:
        self.ms = 0.0

    def __enter__(self) -> "_Lap":
        self._start = _now()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.ms = (_now() - self._start) * 1000.0


class _Step(_Lap):
    """A lap that is also filed under a step name of its recorder, and makes that step the active one."""

    __slots__ = ("_recorder", "_name", "_outer")

    def __init__(self, recorder: "StepRecorder", name: str) -> None:
        self.ms = 0.0
        self._recorder = recorder
        self._name = name

    def __enter__(self) -> "_Step":
        recorder = self._recorder
        self._outer = recorder._active
        recorder._active = self._name
        self._start = _now()  # last, so the bookkeeping above is not part of the step
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        end = _now()
        recorder = self._recorder
        recorder._active = self._outer
        name = self._name
        ms = self.ms = (end - self._start) * 1000.0
        steps = recorder.steps
        steps[name] = steps.get(name, 0.0) + ms
        recorder.timeline.append((name, (self._start - recorder.started) * 1000.0, ms))


class StepRecorder:
    """Collects where the time of one ``anonymize()`` call went, as plain data.

    * ``steps``: milliseconds per top-level step, in execution order, summed when a step runs twice.  Steps are
      exclusive: they are meant to follow one another, never to nest.
    * ``timeline``: ``(step, offset in ms from the creation of the recorder, duration in ms)`` per run of a step,
      for laying the steps out as spans.
    * ``details``: inclusive sub-measures keyed ``"<step>.<detail>"``, see ``DETAIL_NAMES``; each belongs to the
      step that is running when it is reported and is not part of any sum.
    * ``ner_windows``: how many windows the NER model ran.

    One recorder belongs to one call and one thread.  Code deep inside Presidio and Hugging Face, which cannot be
    handed an extra argument, reaches it through :func:`current_recorder`: ``with recorder:`` makes it the current
    one for that block.  Each timer costs well under a microsecond (``tests/test_step_timing.py`` prices it).
    """

    __slots__ = ("started", "steps", "details", "timeline", "ner_windows", "_active", "_token")

    def __init__(self) -> None:
        self.started = _now()
        self.steps: dict[str, float] = {}
        self.details: dict[str, float] = {}
        self.timeline: list[tuple[str, float, float]] = []
        self.ner_windows = 0
        self._active: str | None = None
        self._token = None

    def step(self, name: str) -> _Step:
        """Time a ``with`` block as the step *name*; the context's ``ms`` is its duration once it is over."""
        return _Step(self, name)

    def add_detail(self, detail: str, ms: float) -> None:
        """Add *ms* to ``"<active step>.<detail>"``; ignored outside a step or for a name not in ``DETAIL_NAMES``."""
        active = self._active
        if active is None or detail not in DETAIL_NAMES:
            return
        key = f"{active}.{detail}"
        details = self.details
        details[key] = details.get(key, 0.0) + ms

    def add_windows(self, count: int = 1) -> None:
        self.ner_windows += count

    def ordered_details(self) -> dict[str, float]:
        """The details, steps in execution order and each step's details in ``DETAIL_NAMES`` order."""
        details = self.details
        ordered: dict[str, float] = {}
        for step in self.steps:
            for detail in DETAIL_NAMES:
                key = f"{step}.{detail}"
                if key in details:
                    ordered[key] = details[key]
        return ordered

    def __enter__(self) -> "StepRecorder":
        self._token = _current.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        token, self._token = self._token, None
        _current.reset(token)


class _NoRecorder(StepRecorder):
    """Records nothing.  It is what the hooks find outside a call, so neither they nor the engine branch on it.

    Its steps still measure their block (``ms``): the engine reads its legacy ``nlp_ms``/``recovery_ms`` from them.
    """

    __slots__ = ()

    def step(self, name: str) -> _Lap:
        return _Lap()

    def add_detail(self, detail: str, ms: float) -> None:
        pass

    def add_windows(self, count: int = 1) -> None:
        pass

    def __enter__(self) -> "_NoRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass


NO_RECORDER: StepRecorder = _NoRecorder()

_current: ContextVar[StepRecorder] = ContextVar("pii_shield_step_recorder", default=NO_RECORDER)


def current_recorder() -> StepRecorder:
    """The recorder of the ``anonymize()`` call running in this context, else the shared no-op recorder."""
    return _current.get()


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
