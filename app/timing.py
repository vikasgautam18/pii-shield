"""Where the time of a request goes: the step report behind ``Server-Timing``, the step spans and the histogram.

``/anonymize_unique`` is cut into steps that follow one another and never overlap, in execution order::

    auth, app_config, queue_wait, nlp_pass1, analyze, recovery_pass2, post_processing, handling, store_save

The library times its five (``AnonymizeStats.steps``). The API times the rest, because only it can: ``auth``
(left on ``request.state`` by ``app.auth``), ``app_config`` (Redis behind a 5 s cache), ``queue_wait`` (the wait
for a free detection thread, see ``_run_in_thread_timed`` in ``app.main``) and ``store_save``. What remains of the
request is ``other`` (the library's gate helpers and entity list, the handler's own work between the steps such as
the counters and the response model, the hand-offs between threads), derived and never below 0. ``total`` runs from
the start of the request, authentication included, to the moment the header is built, so serialising the response
is not in it. A *detail* such as ``nlp_pass1.ner_model`` is a measure inside a step: it is reported next to its step
and never added to the sum. ``/deanonymize`` has a lighter report: ``auth``, ``app_config``, ``store_get``,
``restore``, ``other``, ``total``.

Everything runs on the event-loop thread after the work is done, from numbers the library returns as plain data,
so the library needs no OpenTelemetry. The step spans are created after the fact with explicit start and end
times; their parents and the histogram attributes are fixed by contract (see the docstrings below).
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import NamedTuple

from fastapi import Response
from opentelemetry import metrics, trace
from opentelemetry.trace import Span

from app.telemetry import BOUNDS_MS
from pii_shield.models import AnonymizeStats
from pii_shield.observability import DETAIL_NAMES, LIBRARY_STEPS

logger = logging.getLogger("pii-shield")
tracer = trace.get_tracer("pii-shield")

# The top-level steps of each endpoint in execution order; dashboards, the header and the tests rely on these names.
ANONYMIZE_STEPS = ("auth", "app_config", "queue_wait", *LIBRARY_STEPS, "store_save")
DEANONYMIZE_STEPS = ("auth", "app_config", "store_get", "restore")
# The steps that happen inside the ``pii_shield.anonymize`` span; every other step is a child of the server span.
PIPELINE_STEPS = frozenset(("queue_wait", *LIBRARY_STEPS))
# The ``request.state`` attribute ``app.auth.authenticate`` leaves: (perf_counter and time_ns at its start, its ms).
AUTH_TIMING_ATTR = "auth_timing"

_OFF = frozenset(("false", "0", "no", "off"))
_DETAIL_RANK = {name: rank for rank, name in enumerate(DETAIL_NAMES)}


def _switched_on(name: str) -> bool:
    return os.getenv(name, "true").strip().lower() not in _OFF


def server_timing_enabled() -> bool:
    """``SERVER_TIMING`` (default on); ``false``, ``0``, ``no`` or ``off`` leaves the header out."""
    return _switched_on("SERVER_TIMING")


def step_spans_enabled() -> bool:
    """``PII_STEP_SPANS`` (default on); off turns the step spans off and leaves every other span alone."""
    return _switched_on("PII_STEP_SPANS")


class Step(NamedTuple):
    """One top-level step as it ran: its duration and the wall-clock moment it started (for its span)."""

    name: str
    ms: float
    start_ns: int


class PoolTiming(NamedTuple):
    """What the thread pool reports about one detection job (see ``_run_in_thread_timed`` in ``app.main``)."""

    queue_ms: float  # waited for a free thread
    started_ns: int  # time.time_ns() when the job started: the origin of the library's timeline


@dataclass
class StepReport:
    """The merged steps of one request. ``details`` maps a step to ``{detail: ms}`` (those of a step that did not run
    are not reported); ``other_ms`` is never below 0."""

    steps: list[Step]
    details: dict[str, dict[str, float]]
    ner_windows: int
    other_ms: float
    total_ms: float


class _Timer:
    """The context manager behind ``RequestClock.step``: a class, not ``@contextmanager``, to stay cheap."""

    __slots__ = ("_clock", "_name", "_ns", "_perf")

    def __init__(self, clock: "RequestClock", name: str) -> None:
        self._clock = clock
        self._name = name

    def __enter__(self) -> "_Timer":
        self._ns, self._perf = time.time_ns(), time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        self._clock.record(self._name, self._ns, (time.perf_counter() - self._perf) * 1000.0)


class RequestClock:
    """Times the steps the API runs itself and merges them with what the library measured into a ``StepReport``."""

    __slots__ = ("started", "_api")

    def __init__(self, started: float | None = None) -> None:
        # perf_counter reading at the start of the request; durations come from it, span positions from time_ns.
        self.started = time.perf_counter() if started is None else started
        self._api: dict[str, Step] = {}

    @classmethod
    def begin(cls, request) -> "RequestClock":
        """Start the clock of a request, back-dated to the start of authentication when that ran."""
        stashed = getattr(request.state, AUTH_TIMING_ATTR, None)
        if stashed is None:
            return cls()
        started, started_ns, ms = stashed
        clock = cls(started)
        clock.record("auth", started_ns, ms)
        return clock

    def record(self, name: str, start_ns: int, ms: float) -> None:
        self._api[name] = Step(name, ms, start_ns)

    def step(self, name: str) -> _Timer:
        """``with clock.step("store_save"): ...`` times the block as that step."""
        return _Timer(self, name)

    def anonymize_report(self, stats: AnonymizeStats, pool: PoolTiming, now: float | None = None) -> StepReport:
        """The report of ``/anonymize_unique``; ``total`` ends now (a ``perf_counter`` reading, or *now*)."""
        api = self._api
        steps = [api[name] for name in ("auth", "app_config") if name in api]
        steps.append(Step("queue_wait", pool.queue_ms, pool.started_ns - round(pool.queue_ms * 1e6)))
        steps.extend(_library_steps(stats, pool.started_ns))
        if "store_save" in api:
            steps.append(api["store_save"])
        return self._report(steps, _group_details(stats.details), stats.ner_windows, now)

    def deanonymize_report(self, now: float | None = None) -> StepReport:
        """The report of ``/deanonymize``."""
        steps = [self._api[name] for name in DEANONYMIZE_STEPS if name in self._api]
        return self._report(steps, {}, 0, now)

    def _report(self, steps: list[Step], details: dict, ner_windows: int, now: float | None) -> StepReport:
        total_ms = ((time.perf_counter() if now is None else now) - self.started) * 1000.0
        other_ms = max(0.0, total_ms - sum(step.ms for step in steps))
        return StepReport(steps, details, ner_windows, other_ms, total_ms)


def _library_steps(stats: AnonymizeStats, anchor_ns: int) -> list[Step]:
    """The library's steps, positioned on the wall clock from *anchor_ns*, when its ``anonymize()`` began.

    The durations come from ``stats.steps`` and the positions from ``stats.timeline``; a step missing from the
    timeline is laid out right after the one before it.
    """
    offsets: dict[str, float] = {}
    for name, offset_ms, _ in stats.timeline:
        offsets.setdefault(name, offset_ms)
    steps = []
    cursor_ms = 0.0
    for name, ms in stats.steps.items():
        offset_ms = offsets.get(name, cursor_ms)
        steps.append(Step(name, ms, anchor_ns + round(offset_ms * 1e6)))
        cursor_ms = offset_ms + ms
    return steps


def _group_details(details: dict[str, float]) -> dict[str, dict[str, float]]:
    """``{"analyze.dedup": 0.7}`` as ``{"analyze": {"dedup": 0.7}}``, each step's details in ``DETAIL_NAMES`` order."""
    grouped: dict[str, dict[str, float]] = {}
    for key, ms in details.items():
        step, _, detail = key.partition(".")
        if detail:
            grouped.setdefault(step, {})[detail] = ms
    for step, items in grouped.items():
        if len(items) > 1:
            grouped[step] = dict(sorted(items.items(), key=lambda item: _DETAIL_RANK.get(item[0], len(_DETAIL_RANK))))
    return grouped


def server_timing_header(report: StepReport) -> str:
    """The ``Server-Timing`` value: ``name;dur=ms`` entries, two decimals, no description, comma separated.

    Execution order, every step immediately followed by its details (named ``<step>.<detail>``), then ``other``,
    then ``total``. ``other`` is worked out from the numbers as printed, so the printed steps and ``other`` add up
    to the printed total instead of drifting by the rounding of eleven values.
    """
    parts = []
    shown = 0.0
    for step in report.steps:
        ms = round(step.ms, 2)
        shown += ms
        parts.append(f"{step.name};dur={ms:.2f}")
        for detail, detail_ms in report.details.get(step.name, {}).items():
            parts.append(f"{step.name}.{detail};dur={detail_ms:.2f}")
    total = round(report.total_ms, 2)
    parts.append(f"other;dur={max(0.0, total - shown):.2f}")
    parts.append(f"total;dur={total:.2f}")
    return ", ".join(parts)


def steps_for_log(report: StepReport | None) -> str:
    """``step:ms,step:ms,...,other:ms`` (one decimal) for the request log line; empty without a report."""
    if report is None:
        return ""
    parts = [f"{step.name}:{step.ms:.1f}" for step in report.steps]
    parts.append(f"other:{report.other_ms:.1f}")
    return ",".join(parts)


def anonymize_log_fields(report: StepReport | None) -> str:
    """`` total_ms=<ms> ner_windows=<n> steps=<step>:<ms>,...,other:<ms>`` to append to the ``/anonymize_unique`` log line.

    ``total_ms`` is the ``total`` of the ``Server-Timing`` header. Empty without a report (reporting failed, and the
    log line is then the one it was before the step timings existed).
    """
    if report is None:
        return ""
    return f" total_ms={report.total_ms:.1f} ner_windows={report.ner_windows} steps={steps_for_log(report)}"


def deanonymize_log_fields(report: StepReport | None) -> str:
    """`` total_ms=<ms>`` to append to the ``/deanonymize`` log line; empty without a report."""
    return "" if report is None else f" total_ms={report.total_ms:.1f}"


def create_step_histogram(meter: metrics.Meter) -> metrics.Histogram:
    """The ``pii.step.duration`` instrument; ``BOUNDS_MS`` keeps a step of tens of seconds out of the last bucket."""
    return meter.create_histogram(
        "pii.step.duration",
        unit="ms",
        description=(
            "Time one request spent in each step of /anonymize_unique (a name with a dot is a detail inside a step, "
            "not to be added to the steps)"
        ),
        explicit_bucket_boundaries_advisory=BOUNDS_MS,
    )


step_duration = create_step_histogram(metrics.get_meter("pii-shield"))


def record_step_metrics(report: StepReport, size: str) -> None:
    """Record every step, ``other`` and every detail in ``pii.step.duration`` with the attributes ``step`` and ``size``.

    Never ``app_id`` or ``app_name``: the number of series is steps x sizes x workers, and stays that way.
    """
    record = step_duration.record
    for step in report.steps:
        record(step.ms, {"step": step.name, "size": size})
        for detail, detail_ms in report.details.get(step.name, {}).items():
            record(detail_ms, {"step": f"{step.name}.{detail}", "size": size})
    record(report.other_ms, {"step": "other", "size": size})


def _interval(span: Span, now_ns: int) -> tuple[int, int]:
    """The start and end of *span* in ns; a span that is still running ends no earlier than now."""
    return getattr(span, "start_time", None) or 0, getattr(span, "end_time", None) or now_ns


def emit_step_spans(report: StepReport, server_span: Span, pipeline_span: Span | None = None) -> int:
    """Create one INTERNAL span per step that ran, after the fact, and return how many.

    The span is named after the step and carries ``pii.step`` and, for a step with details,
    ``pii.detail.<detail>_ms`` rounded to 0.1. ``queue_wait`` and the library steps are children of
    *pipeline_span* (``pii_shield.anonymize``), every other step of *server_span*, the span the request runs in.
    ``other`` and ``total`` are not spans. Each child is clamped into its parent's interval: the library's
    offsets come from ``perf_counter`` and the span times from the wall clock, and a few microseconds of drift
    between the two must never put a child outside its parent. Nothing is created when the trace is not
    recorded (sampled out, or no SDK), as the children would be dropped anyway.
    """
    if not server_span.is_recording():
        return 0
    now_ns = time.time_ns()
    server_context = trace.set_span_in_context(server_span)
    server_interval = _interval(server_span, now_ns)
    if pipeline_span is None:
        pipeline_context, pipeline_interval = server_context, server_interval
    else:
        pipeline_context, pipeline_interval = trace.set_span_in_context(pipeline_span), _interval(pipeline_span, now_ns)
    created = 0
    for step in report.steps:
        if step.name in PIPELINE_STEPS:
            context, (parent_start, parent_end) = pipeline_context, pipeline_interval
        else:
            context, (parent_start, parent_end) = server_context, server_interval
        start = min(max(step.start_ns, parent_start), parent_end)
        end = min(max(step.start_ns + round(step.ms * 1e6), start), parent_end)
        attributes = {"pii.step": step.name}
        for detail, detail_ms in report.details.get(step.name, {}).items():
            attributes[f"pii.detail.{detail}_ms"] = round(detail_ms, 1)
        tracer.start_span(step.name, context=context, attributes=attributes, start_time=start).end(end_time=end)
        created += 1
    return created


def finish_anonymize(
    clock: RequestClock,
    stats: AnonymizeStats,
    pool: PoolTiming,
    *,
    size: str,
    response: Response,
    server_span: Span,
    pipeline_span: Span,
) -> StepReport | None:
    """Report the steps of a finished ``/anonymize_unique``: the header, the histogram and the spans.

    ``total`` ends here, when the header is built; recording the metrics and creating the spans (about 0.15 ms)
    come after it. Returns the report, or ``None`` when something failed: telemetry must never turn a request that
    succeeded (the session is already saved) into an error.
    """
    try:
        report = clock.anonymize_report(stats, pool)
        if server_timing_enabled():
            response.headers["Server-Timing"] = server_timing_header(report)
        record_step_metrics(report, size)
        if step_spans_enabled():
            emit_step_spans(report, server_span, pipeline_span)
        return report
    except Exception:
        logger.warning("Could not report the step timings; the response is not affected", exc_info=True)
        return None


def finish_deanonymize(clock: RequestClock, response: Response) -> StepReport | None:
    """The ``Server-Timing`` header of a finished ``/deanonymize`` (no spans, no histogram: it is the light report)."""
    try:
        report = clock.deanonymize_report()
        if server_timing_enabled():
            response.headers["Server-Timing"] = server_timing_header(report)
        return report
    except Exception:
        logger.warning("Could not report the step timings; the response is not affected", exc_info=True)
        return None
