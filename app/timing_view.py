"""Pure helpers behind the Playground's timing panel; no Streamlit import, so they unit-test without a UI runtime.

Imported flat (``from timing_view import ...``) by ``streamlit_app.py``, like ``themes`` and ``ui_auth``.

The API reports where a request spent its time in a W3C ``Server-Timing`` header (``metric = name *( ";" param )``
with the params ``dur=<ms>`` and ``desc=<token | quoted-string>``) and names the request's trace in a ``traceresponse``
header. Both cross a network hop and may have been rewritten by a proxy (lines merged, metrics added, whitespace
changed), so every function here is total: malformed input gives an empty or partial result, never an exception.

Decisions worth knowing:

* Repeated names are **summed**. The spec allows a name more than once (e.g. two cache lookups) and adding keeps all
  the time that was reported; the API itself never repeats a name.
* A metric without a usable ``dur`` (missing, not a plain decimal number, negative, not finite) is skipped.
* ``desc`` is parsed only so that commas and semicolons inside its quoted string are not mistaken for separators.
* A metric the API does not define is shown under its own name (a newer API may add steps), unless it is longer than
  ``total``: that is a proxy's view of the origin, not a part of the server's time, and is left out.
* At most ``MAX_HEADER_CHARS`` characters and ``MAX_METRICS`` distinct metrics are read. A metric cut by the first limit
  is dropped, because a half-read number would be a wrong number.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Top-level steps in execution order. The anonymize and de-anonymize sets never occur in the same response, so one
# list orders both; ``other`` is the derived remainder and closes the list.
STEP_ORDER: tuple[str, ...] = (
    "auth",
    "app_config",
    "queue_wait",
    "nlp_pass1",
    "analyze",
    "recovery_pass2",
    "post_processing",
    "handling",
    "store_save",
    "store_get",
    "restore",
    "other",
)
DETAIL_ORDER: tuple[str, ...] = ("ner_model", "context_enhance", "dedup")

# name -> (label for the table, short label for the chart axis and the metric tile, which truncates long text)
_STEP_LABELS: dict[str, tuple[str, str]] = {
    "auth": ("Sign-in check", "Sign-in check"),
    "app_config": ("Application settings", "App settings"),
    "queue_wait": ("Waiting for a detection thread", "Queue wait"),
    "nlp_pass1": ("NLP pass 1 (spaCy + NER model)", "NLP pass 1"),
    "analyze": ("Recognizers (regex, validators, scoring, dedup)", "Recognizers"),
    "recovery_pass2": ("Second NER pass (case recovery)", "Second NER pass"),
    "post_processing": ("Post-processing", "Post-processing"),
    "handling": ("Handling (replace / hash / fake)", "Handling"),
    "store_save": ("Saving the session", "Save session"),
    "store_get": ("Loading the session", "Load session"),
    "restore": ("Restoring the original text", "Restore text"),
    "other": ("Other", "Other"),
    "total": ("Total", "Total"),
}
_DETAIL_LABELS: dict[str, str] = {
    "ner_model": "NER model",
    "context_enhance": "Context scoring",
    "dedup": "Duplicate removal",
}

MAX_HEADER_CHARS = 16_384
MAX_METRICS = 64
_MAX_NAME_CHARS = 64

_STEP_RANK = {name: rank for rank, name in enumerate(STEP_ORDER)}
_DETAIL_RANK = {name: rank for rank, name in enumerate(DETAIL_ORDER)}

_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")  # RFC 9110 "token"
# ASCII digits only: Python's float() would also take "nan", "inf", "1_0" and non-ASCII digits.
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_HEX = re.compile(r"[0-9a-f]+")
_UNSAFE_IN_URL = re.compile(r"[\x00-\x20\x7f<>]")


# ── Server-Timing ────────────────────────────────────────────────────────────


def parse_server_timing(header: str | bytes | None) -> dict[str, float]:
    """Parse a ``Server-Timing`` header into ``{metric name: milliseconds}`` in order of first appearance.

    Never raises. Metrics that cannot be read are left out; see the module docstring for the rules.
    """
    if isinstance(header, bytes):
        header = header.decode("latin-1")  # header bytes are ISO-8859-1; this cannot fail
    if not isinstance(header, str) or not header:
        return {}
    truncated = len(header) > MAX_HEADER_CHARS
    metrics = _split_metrics(header[:MAX_HEADER_CHARS])
    if truncated:
        metrics.pop()
    timing: dict[str, float] = {}
    for parts in metrics:
        parsed = _parse_metric(parts)
        if parsed is None:
            continue
        name, duration = parsed
        if name not in timing and len(timing) >= MAX_METRICS:
            continue
        summed = timing.get(name, 0.0) + duration
        if math.isfinite(summed):
            timing[name] = summed
    return timing


def _split_metrics(text: str) -> list[list[str]]:
    """Split on top-level commas into metrics and on top-level semicolons into ``[name, param, ...]``.

    A quoted string is only recognised as a parameter value (right after ``=``), where the grammar puts it. A stray
    quote anywhere else is an ordinary character, so one garbled metric cannot swallow the metrics that follow it.
    Inside a quoted string a backslash escapes the next character; a string that never closes runs to the end.
    """
    metrics: list[list[str]] = []
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    after_equals = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"' and after_equals:
            quoted = True
        elif char in ",;":
            parts.append(text[start:index])
            start = index + 1
            if char == ",":
                metrics.append(parts)
                parts = []
        if not char.isspace():
            after_equals = char == "="
    parts.append(text[start:])
    metrics.append(parts)
    return metrics


def _parse_metric(parts: list[str]) -> tuple[str, float] | None:
    name = parts[0].strip()
    if len(name) > _MAX_NAME_CHARS or not _TOKEN.fullmatch(name):
        return None
    for raw in parts[1:]:
        key, equals, value = raw.partition("=")
        if equals and key.strip().lower() == "dur":
            duration = _duration(value.strip())  # the first dur wins; a bad one invalidates the metric
            return None if duration is None else (name, duration)
    return None


def _duration(value: str) -> float | None:
    if len(value) >= 2 and value[0] == value[-1] == '"':  # dur="12.5" is legal: the value may be a quoted string
        value = value[1:-1]
    if not _NUMBER.fullmatch(value):
        return None
    number = float(value)
    return number + 0.0 if math.isfinite(number) and number >= 0 else None  # + 0.0 turns -0.0 into 0.0


# ── Steps, details, shares ───────────────────────────────────────────────────


def split_timing(timing: Mapping[str, float]) -> tuple[dict[str, float], dict[str, float], float | None]:
    """Separate ``(top-level steps, details, total)``.

    A detail is ``<step>.<detail>`` (inclusive of its step, so never to be added to it); ``total`` is returned on its
    own. Values that are not finite, non-negative numbers are dropped.
    """
    steps: dict[str, float] = {}
    details: dict[str, float] = {}
    total: float | None = None
    for name, ms in timing.items():
        if not isinstance(name, str) or not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms < 0:
            continue
        if name == "total":
            total = float(ms)
        elif _split_detail(name) is not None:
            details[name] = float(ms)
        else:
            steps[name] = float(ms)
    return steps, details, total


def _split_detail(name: str) -> tuple[str, str] | None:
    parent, dot, detail = name.partition(".")
    return (parent, detail) if dot and parent and detail else None


def order_steps(names: Iterable[str]) -> list[str]:
    """Order top-level step names by the contract's execution order; unknown names follow, in their given order."""
    return sorted(names, key=lambda name: _STEP_RANK.get(name, len(_STEP_RANK)))


def step_shares(steps: Mapping[str, float], total: float | None = None) -> dict[str, float]:
    """Each step's fraction of ``total``, or of the sum of the steps when ``total`` is missing or not positive."""
    denominator = _denominator(steps, total)
    return {name: _share(ms, denominator) for name, ms in steps.items()}


def _denominator(steps: Mapping[str, float], total: float | None) -> float:
    return total if total is not None and total > 0 else sum(steps.values())


def _share(ms: float, denominator: float) -> float:
    return ms / denominator if math.isfinite(denominator) and denominator > 0 else 0.0


def slowest_step(steps: Mapping[str, float]) -> str | None:
    """Name of the step with the largest duration; the earliest one in the mapping's order wins a tie."""
    best: str | None = None
    for name, ms in steps.items():
        if best is None or ms > steps[best]:
            best = name
    return best


# ── Labels ───────────────────────────────────────────────────────────────────


def label(name: str) -> str:
    """Friendly label for a step or detail name (for the table); an unknown name is shown as it is."""
    return _label(name, 0)


def short_label(name: str) -> str:
    """Shorter label for places with little width (chart axis, metric tile)."""
    return _label(name, 1)


def _label(name: str, which: int) -> str:
    if name in _STEP_LABELS:
        return _STEP_LABELS[name][which]
    split = _split_detail(name)
    if split is not None and split[0] in _STEP_LABELS and split[1] in _DETAIL_LABELS:
        return _DETAIL_LABELS[split[1]]
    return name


# ── The breakdown the panel renders ──────────────────────────────────────────


@dataclass(frozen=True)
class Row:
    name: str  # "analyze" or "analyze.dedup"
    label: str
    short_label: str
    ms: float
    share: float  # of the server total, so a detail's share is already part of its step's
    parent: str | None = None  # the step a detail belongs to; None for a top-level step

    @property
    def is_detail(self) -> bool:
        return self.parent is not None


@dataclass(frozen=True)
class Breakdown:
    total_ms: float
    total_reported: bool  # False: ``total`` was missing and total_ms is the sum of the steps
    rows: tuple[Row, ...]  # each top-level step in execution order, followed by its details
    slowest: Row | None  # the slowest top-level step

    @property
    def steps(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if not row.is_detail)


def build_breakdown(timing: Mapping[str, float]) -> Breakdown | None:
    """Turn parsed ``Server-Timing`` metrics into what the panel shows; ``None`` when there is nothing to show."""
    steps, details, total = split_timing(timing)
    if total is not None and total > 0:
        # Longer than the whole request cannot be a part of it: that is a proxy's view of the origin (Cloudflare's
        # cfOrigin, say), which includes this server's time. Only a name the API does not define can be foreign.
        steps = {name: ms for name, ms in steps.items() if name in _STEP_RANK or ms <= total}
    if not steps and total is None:
        return None
    total_ms = sum(steps.values()) if total is None else total
    if not math.isfinite(total_ms):
        total_ms = 0.0
    denominator = _denominator(steps, total)

    rows: list[Row] = []
    claimed: set[str] = set()
    for name in order_steps(steps):
        rows.append(_row(name, steps[name], denominator))
        for key in _ordered_details(details, parent=name):
            rows.append(_row(key, details[key], denominator, parent=name))
            claimed.add(key)
    # A detail whose step is missing (something rewrote the header) is still shown, after the steps.
    for key in _ordered_details({k: v for k, v in details.items() if k not in claimed}):
        rows.append(_row(key, details[key], denominator, parent=key.partition(".")[0]))

    slowest_name = slowest_step({row.name: row.ms for row in rows if not row.is_detail})
    slowest = next((row for row in rows if row.name == slowest_name and not row.is_detail), None)
    return Breakdown(total_ms=total_ms, total_reported=total is not None, rows=tuple(rows), slowest=slowest)


def _row(name: str, ms: float, denominator: float, parent: str | None = None) -> Row:
    return Row(name, label(name), short_label(name), ms, _share(ms, denominator), parent)


def _ordered_details(details: Mapping[str, float], parent: str | None = None) -> list[str]:
    keys = [key for key in details if parent is None or key.partition(".")[0] == parent]
    return sorted(
        keys,
        key=lambda key: (
            _STEP_RANK.get(key.partition(".")[0], len(_STEP_RANK)),
            _DETAIL_RANK.get(key.partition(".")[2], len(_DETAIL_RANK)),
        ),
    )


def format_ms(ms: float) -> str:
    """Compact duration for a metric tile: ``0.41 ms``, ``42.5 ms``, ``788 ms``, ``12,345 ms``."""
    if not math.isfinite(ms):
        return "n/a"
    if abs(ms) < 10:
        return f"{ms:.2f} ms"
    if abs(ms) < 100:
        return f"{ms:.1f} ms"
    return f"{ms:,.0f} ms"


def format_share(share: float) -> str:
    """A fraction as a whole-number percentage; a non-zero share under 1% reads ``<1%`` rather than ``0%``."""
    if not math.isfinite(share):
        return "n/a"
    percent = share * 100
    return "<1%" if 0 < percent < 1 else f"{percent:.0f}%"


# ── Trace context ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TraceResponse:
    trace_id: str  # 32 lowercase hex characters
    span_id: str  # 16 lowercase hex characters
    sampled: bool  # the trace-flags "sampled" bit: when False the trace was never exported


def parse_traceresponse(header: str | bytes | None) -> TraceResponse | None:
    """Parse a W3C ``traceresponse`` header (``00-<trace-id>-<span-id>-<flags>``); ``None`` when it is not valid.

    Tolerant of case and surrounding whitespace, and of several values merged into one comma-separated header
    (the first valid one wins). Never raises.
    """
    if isinstance(header, bytes):
        header = header.decode("latin-1")
    if not isinstance(header, str):
        return None
    for candidate in header[:MAX_HEADER_CHARS].split(","):
        parsed = _parse_trace_context(candidate.strip().lower())
        if parsed is not None:
            return parsed
    return None


def _parse_trace_context(value: str) -> TraceResponse | None:
    parts = value.split("-")
    if len(parts) < 4:
        return None
    version, trace_id, span_id, flags = parts[:4]
    # Version 00 has exactly four fields; later versions may append more, which are ignored.
    if version == "ff" or (version == "00" and len(parts) != 4):
        return None
    if not (_is_hex(version, 2) and _is_hex(trace_id, 32) and _is_hex(span_id, 16) and _is_hex(flags, 2)):
        return None
    if set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    return TraceResponse(trace_id, span_id, bool(int(flags, 16) & 1))


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and _HEX.fullmatch(value) is not None


def trace_id_from_traceresponse(header: str | bytes | None) -> str | None:
    """The 32-hex trace id in a ``traceresponse`` header, or ``None``."""
    parsed = parse_traceresponse(header)
    return parsed.trace_id if parsed else None


def trace_url(template: str | None, trace_id: str | None) -> str | None:
    """Fill ``{trace_id}`` in an operator-supplied URL template; ``None`` when no valid link can be made.

    The template must be an http(s) URL that contains ``{trace_id}`` and no whitespace or ``<``/``>`` (percent-encode
    them); ``trace_id`` must be a real 32-hex id, so nothing else can reach the URL. Other braces in the template are
    left alone (``str.format`` would choke on a JSON fragment in a Grafana explore URL).
    """
    if not isinstance(template, str) or not isinstance(trace_id, str):
        return None
    template = template.strip()
    if "{trace_id}" not in template or not _is_hex(trace_id, 32) or set(trace_id) == {"0"}:
        return None
    url = template.replace("{trace_id}", trace_id)
    if not url.lower().startswith(("http://", "https://")) or _UNSAFE_IN_URL.search(url):
        return None
    return url


def trace_caption(trace: TraceResponse | None, template: str | None = None) -> str | None:
    """Markdown line naming this request's trace, with a link when ``template`` yields one; ``None`` without a trace.

    An unsampled trace gets no link: it was never exported, so the link would open an empty page. A template that
    is set but unusable is called out, rather than silently showing no link.
    """
    if trace is None:
        return None
    line = f"Trace ID `{trace.trace_id}`"
    if not trace.sampled:
        return f"{line} · not sampled, so no trace was stored"
    if not template or not template.strip():
        return line
    url = trace_url(template, trace.trace_id)
    if url is None:
        return f"{line} · TRACE_URL_TEMPLATE must be an http(s) URL containing {{trace_id}}"
    # Parentheses are legal in a URL but would end a markdown link early; their escapes mean the same thing.
    return f"{line} · [Open trace]({url.replace('(', '%28').replace(')', '%29')})"
