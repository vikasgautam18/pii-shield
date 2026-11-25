"""The per-step telemetry of a request: ``Server-Timing``, the step spans, ``pii.step.duration`` and the log line.

Most tests replace the engine with one that returns hand-made ``AnonymizeStats`` after a fixed pause, so every
number they check is known; ``TestRealEngine`` runs the real engine and checks that the header, the spans and the
histogram all say what the stats it returned carry. Spans are read from an ``InMemorySpanExporter`` on the
process-wide tracer provider, metrics from an ``InMemoryMetricReader`` on a private meter provider (the global one
can be set only once per process).
"""

import logging
import re
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.trace import SpanKind

from app import audit_store, main, timing
from app.auth import JwksCache, TokenValidator, configure_auth
from app.timing import PoolTiming, RequestClock, Step, StepReport
from pii_shield.models import AnonymizeResult, AnonymizeStats, DetectedEntity
from pii_shield.nlp_engine import get_nlp_engine_name
from pii_shield.observability import DETAIL_NAMES, LIBRARY_STEPS
from tests._auth_helpers import FakeIdp

ALLOWED_SPANS = set(timing.ANONYMIZE_STEPS) | set(timing.DEANONYMIZE_STEPS)

# What the stand-in engine reports: a recovery pass ran, the NER model ran three windows. The timeline fits inside
# the 30 ms the stand-in takes, as the real one fits inside the real call.
LIB_STEPS = {"nlp_pass1": 6.0, "analyze": 3.0, "recovery_pass2": 4.0, "post_processing": 1.0, "handling": 0.5}
LIB_DETAILS = {
    "nlp_pass1.ner_model": 5.0,
    "analyze.context_enhance": 0.4,
    "analyze.dedup": 0.2,
    "recovery_pass2.ner_model": 3.5,
}
LIB_TIMELINE = [
    ("nlp_pass1", 0.0, 6.0),
    ("analyze", 6.0, 3.0),
    ("recovery_pass2", 9.0, 4.0),
    ("post_processing", 13.0, 1.0),
    ("handling", 14.0, 0.5),
]

ENTRY = re.compile(r"^(?P<name>[a-z_0-9]+(?:\.[a-z_0-9]+)?);dur=(?P<ms>\d+\.\d{2})$")


def parse_server_timing(header: str) -> list[tuple[str, float]]:
    entries = []
    for item in header.split(", "):
        match = ENTRY.match(item)
        assert match, f"not a name;dur=N entry: {item!r} in {header!r}"
        entries.append((match["name"], float(match["ms"])))
    return entries


def make_stats(**overrides) -> AnonymizeStats:
    fields = dict(
        detect_ms=14.5,
        anonymize_ms=0.5,
        total_ms=15.0,
        nlp_ms=6.0,
        recovery_ms=4.0,
        recovery_pass=True,
        steps=dict(LIB_STEPS),
        details=dict(LIB_DETAILS),
        timeline=list(LIB_TIMELINE),
        ner_windows=3,
    )
    fields.update(overrides)
    return AnonymizeStats(**fields)


@pytest.fixture(autouse=True)
def _temp_audit_db(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_store, "AUDIT_DB_PATH", str(tmp_path / "audit.db"))


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture(scope="module")
def _exporter():
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.shutdown()  # a span processor cannot be removed; this makes it inert


@pytest.fixture()
def spans(_exporter):
    _exporter.clear()
    return _exporter


@pytest.fixture()
def step_metrics(monkeypatch):
    """The ``pii.step.duration`` histogram of the app, redirected to a reader the test can look into."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    monkeypatch.setattr(timing, "step_duration", timing.create_step_histogram(provider.get_meter("test")))
    return reader


@pytest.fixture()
def fake_library(monkeypatch):
    """An engine that takes 30 ms and reports ``LIB_*``, so the API's side of the contract can be checked exactly."""

    def anonymize(**kwargs):
        time.sleep(0.03)
        entity = DetectedEntity(entity_type="PERSON", start=0, end=6, score=0.9, text="Rajesh")
        return AnonymizeResult(
            anonymized_text="{PERSON_1} wrote",
            entity_mapping={"{PERSON_1}": "Rajesh"},
            entities=[entity],
            stats=make_stats(),
        )

    monkeypatch.setattr(main.engine, "anonymize", anonymize)


def histogram_points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def anonymize(client, text="Rajesh wrote the report", headers=None):
    response = client.post("/anonymize_unique", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response


def finished_by_name(exporter) -> dict:
    return {span.name: span for span in exporter.get_finished_spans()}


def server_span(exporter):
    (span,) = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
    return span


def step_spans(exporter) -> list:
    return [s for s in exporter.get_finished_spans() if s.attributes and "pii.step" in s.attributes]


def register_app(client, name="TimingApp") -> str:
    return client.post("/apps", json={"app_name": name}).json()["app_id"]


class TestServerTimingHeader:
    def test_it_lists_every_step_then_other_then_total_in_the_contract_grammar(self, client, fake_library):
        header = anonymize(client).headers["Server-Timing"]
        names = [name for name, _ in parse_server_timing(header)]
        assert names == [
            "queue_wait",
            "nlp_pass1", "nlp_pass1.ner_model",
            "analyze", "analyze.context_enhance", "analyze.dedup",
            "recovery_pass2", "recovery_pass2.ner_model",
            "post_processing",
            "handling",
            "store_save",
            "other",
            "total",
        ]  # fmt: skip
        assert "desc=" not in header

    def test_the_library_durations_are_passed_through(self, client, fake_library):
        entries = dict(parse_server_timing(anonymize(client).headers["Server-Timing"]))
        for key, expected in {**LIB_STEPS, **LIB_DETAILS}.items():
            assert entries[key] == pytest.approx(expected, abs=0.005)

    def test_the_steps_and_other_add_up_to_the_total(self, client, fake_library):
        entries = parse_server_timing(anonymize(client).headers["Server-Timing"])
        top_level = [ms for name, ms in entries if "." not in name and name != "total"]
        total = dict(entries)["total"]
        assert sum(top_level) == pytest.approx(total, abs=0.05)
        assert dict(entries)["other"] >= 0
        assert total >= 30  # the stand-in engine alone takes 30 ms

    def test_details_have_a_dot_and_follow_their_own_step(self, client, fake_library):
        names = [name for name, _ in parse_server_timing(anonymize(client).headers["Server-Timing"])]
        for index, name in enumerate(names):
            if "." in name:
                assert names[index - 1].split(".")[0] == name.split(".")[0]
        top_level = [name for name in names if "." not in name]
        assert set(top_level) <= set(timing.ANONYMIZE_STEPS) | {"other", "total"}

    def test_app_config_is_there_only_when_an_app_id_is_sent(self, client, fake_library):
        plain = [n for n, _ in parse_server_timing(anonymize(client).headers["Server-Timing"])]
        assert "app_config" not in plain and "auth" not in plain
        app_id = register_app(client)
        with_app = [
            n for n, _ in parse_server_timing(anonymize(client, headers={"X-App-Id": app_id}).headers["Server-Timing"])
        ]
        assert with_app[0] == "app_config" and with_app[1] == "queue_wait"

    def test_a_text_without_a_recovery_pass_has_no_such_step(self, client, monkeypatch):
        stats = make_stats(
            recovery_pass=False,
            steps={k: v for k, v in LIB_STEPS.items() if k != "recovery_pass2"},
            details={k: v for k, v in LIB_DETAILS.items() if not k.startswith("recovery_pass2")},
            timeline=[t for t in LIB_TIMELINE if t[0] != "recovery_pass2"],
        )
        monkeypatch.setattr(main.engine, "anonymize", lambda **kwargs: AnonymizeResult("x", stats=stats))
        names = [n for n, _ in parse_server_timing(anonymize(client).headers["Server-Timing"])]
        assert "recovery_pass2" not in names and "recovery_pass2.ner_model" not in names
        assert "nlp_pass1" in names and "handling" in names


class TestSwitches:
    @pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", "Off", " false "])
    def test_server_timing_can_be_switched_off(self, client, fake_library, monkeypatch, value):
        monkeypatch.setenv("SERVER_TIMING", value)
        response = anonymize(client)
        assert "server-timing" not in response.headers

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", ""])
    def test_anything_else_leaves_it_on(self, client, fake_library, monkeypatch, value):
        monkeypatch.setenv("SERVER_TIMING", value)
        assert "server-timing" in anonymize(client).headers

    def test_switching_server_timing_off_removes_it_from_deanonymize_too(self, client, fake_library, monkeypatch):
        body = anonymize(client).json()
        monkeypatch.setenv("SERVER_TIMING", "off")
        response = client.post("/deanonymize", json={"id": body["id"], "text": body["anonymized_text"]})
        assert response.status_code == 200 and "server-timing" not in response.headers

    def test_step_spans_off_removes_only_the_step_spans(self, client, fake_library, spans, step_metrics, monkeypatch):
        monkeypatch.setenv("PII_STEP_SPANS", "false")
        response = anonymize(client)
        by_name = finished_by_name(spans)
        assert step_spans(spans) == []
        assert {"pii_shield.anonymize"} <= set(by_name) and server_span(spans).name == "POST /anonymize_unique"
        assert by_name["pii_shield.anonymize"].attributes["pii.ner_windows"] == 3
        assert "server-timing" in response.headers
        assert histogram_points(step_metrics, "pii.step.duration")


class TestSpans:
    def test_the_steps_are_spans_named_after_them(self, client, fake_library, spans):
        anonymize(client, headers=None)
        found = step_spans(spans)
        assert [s.name for s in found if s.name in LIB_STEPS] == list(LIB_STEPS)
        assert {s.name for s in found} == {"queue_wait", *LIB_STEPS, "store_save"}
        assert len(found) <= 10
        for span in found:
            assert span.name in ALLOWED_SPANS
            assert span.kind == SpanKind.INTERNAL
            assert span.attributes["pii.step"] == span.name

    def test_other_and_total_are_never_spans(self, client, fake_library, spans):
        anonymize(client)
        assert not {"other", "total"} & {s.name for s in spans.get_finished_spans()}

    def test_the_parents_follow_the_contract(self, client, fake_library, spans):
        app_id = register_app(client)
        spans.clear()
        anonymize(client, headers={"X-App-Id": app_id})
        by_name = finished_by_name(spans)
        server, pipeline = server_span(spans), by_name["pii_shield.anonymize"]
        assert server.parent is None
        assert pipeline.parent.span_id == server.context.span_id
        for name in ("app_config", "store_save"):
            assert by_name[name].parent.span_id == server.context.span_id
        for name in ("queue_wait", *LIB_STEPS):
            assert by_name[name].parent.span_id == pipeline.context.span_id
        assert {s.context.trace_id for s in spans.get_finished_spans()} == {server.context.trace_id}

    def test_every_child_lies_inside_its_parent_and_the_steps_never_overlap(self, client, fake_library, spans):
        anonymize(client)
        by_id = {s.context.span_id: s for s in spans.get_finished_spans()}
        children = step_spans(spans)
        for child in children:
            parent = by_id[child.parent.span_id]
            assert parent.start_time <= child.start_time <= child.end_time <= parent.end_time, child.name
        ordered = sorted(children, key=lambda s: (s.start_time, s.end_time))
        for earlier, later in zip(ordered, ordered[1:]):
            assert earlier.end_time <= later.start_time, (earlier.name, later.name)

    def test_the_span_durations_are_the_step_durations(self, client, fake_library, spans):
        anonymize(client)
        by_name = finished_by_name(spans)
        for name, expected_ms in LIB_STEPS.items():
            span = by_name[name]
            assert (span.end_time - span.start_time) / 1e6 == pytest.approx(expected_ms, abs=0.01)
        library_start = by_name["nlp_pass1"].start_time
        assert by_name["analyze"].start_time - library_start == pytest.approx(6.0e6, abs=1e4)
        assert by_name["queue_wait"].end_time == library_start  # the wait ends when the library starts

    def test_a_step_with_details_carries_them_as_attributes(self, client, fake_library, spans):
        anonymize(client)
        by_name = finished_by_name(spans)
        detail_attributes = {
            name: {k: v for k, v in by_name[name].attributes.items() if k.startswith("pii.detail.")}
            for name in by_name
            if name in LIB_STEPS
        }
        assert detail_attributes == {
            "nlp_pass1": {"pii.detail.ner_model_ms": 5.0},
            "analyze": {"pii.detail.context_enhance_ms": 0.4, "pii.detail.dedup_ms": 0.2},
            "recovery_pass2": {"pii.detail.ner_model_ms": 3.5},
            "post_processing": {},
            "handling": {},
        }

    def test_the_anonymize_span_keeps_its_attributes_and_gets_the_window_count(self, client, fake_library, spans):
        anonymize(client, "Rajesh wrote the report")
        attributes = finished_by_name(spans)["pii_shield.anonymize"].attributes
        assert attributes["pii.ner_windows"] == 3
        for key in ("language", "entities_found", "placeholders_created"):
            assert f"pii.{key}" in attributes

    def test_the_http_receive_and_send_spans_are_gone(self, client, fake_library, spans):
        anonymize(client)
        names = [s.name for s in spans.get_finished_spans()]
        assert not [n for n in names if n.endswith((" http send", " http receive"))], names
        assert sorted(names) == sorted(
            ["POST /anonymize_unique", "pii_shield.anonymize", "queue_wait", *LIB_STEPS, "store_save"]
        )

    @pytest.mark.parametrize("kind", ["no SDK span", "sampled out"])
    def test_a_request_that_is_not_recorded_creates_no_step_spans(self, kind):
        dropped = TracerProvider(sampler=ALWAYS_OFF).get_tracer("t").start_span("server")
        parent = trace.INVALID_SPAN if kind == "no SDK span" else dropped
        assert not parent.is_recording()
        report = StepReport([Step("queue_wait", 1.0, 1)], {}, 0, 0.0, 1.0)
        assert timing.emit_step_spans(report, parent, parent) == 0


PLAIN_TEXT = "Please send the signed form to Rajesh Kumar tomorrow"  # properly cased: no second NER pass
CAPS_TEXT = "Dear RAJESH KUMAR, your form went to rajesh.kumar@gmail.com"
# Long enough to need several NER windows per pass; the ALL-CAPS names run a second pass.
LONG_CAPS_TEXT = (
    "Dear RAJESH KUMAR, the claim form for PRIYA SHARMA was mailed to rajesh.kumar@gmail.com on 12 March 2024. "
    + "Please keep this note with your records and call the branch office if anything looks wrong. " * 30
)
REAL_TEXTS = {"plain": (PLAIN_TEXT, False), "all-caps": (CAPS_TEXT, True), "long all-caps": (LONG_CAPS_TEXT, True)}


@pytest.fixture(params=list(REAL_TEXTS))
def real_call(request, client, spans, step_metrics, monkeypatch):
    """One request through the real engine, with the stats the engine returned for it."""
    text, recovery = REAL_TEXTS[request.param]
    seen = []
    real = main.engine.anonymize

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        seen.append(result.stats)
        return result

    monkeypatch.setattr(main.engine, "anonymize", spy)
    response = anonymize(client, text)
    assert len(seen) == 1
    return SimpleNamespace(text=text, recovery=recovery, response=response, stats=seen[0])


class TestRealEngine:
    """A request through the real engine: the header, the spans and the histogram all say what its stats carry."""

    def test_the_engine_reports_the_contract_steps(self, real_call):
        stats = real_call.stats
        assert list(stats.steps) == [s for s in LIBRARY_STEPS if real_call.recovery or s != "recovery_pass2"]
        assert stats.recovery_pass is real_call.recovery
        for key in stats.details:
            step, detail = key.split(".")
            assert step in stats.steps and detail in DETAIL_NAMES, key
        if get_nlp_engine_name() == "onnx":  # only the ONNX engine times the model and counts its windows
            assert "nlp_pass1.ner_model" in stats.details
            assert stats.ner_windows >= (2 if real_call.recovery else 1)
        else:
            assert stats.ner_windows == 0 and not any(key.endswith(".ner_model") for key in stats.details)

    def test_the_header_lists_the_steps_and_details_of_the_stats(self, real_call):
        stats = real_call.stats
        entries = parse_server_timing(real_call.response.headers["Server-Timing"])
        expected = ["queue_wait"]
        for step in stats.steps:
            expected += [step, *(key for key in stats.details if key.startswith(f"{step}."))]
        assert [name for name, _ in entries] == [*expected, "store_save", "other", "total"]
        shown = dict(entries)
        for key, ms in {**stats.steps, **stats.details}.items():
            assert shown[key] == pytest.approx(ms, abs=0.006), key
        top_level = [ms for name, ms in entries if "." not in name and name != "total"]
        assert sum(top_level) == pytest.approx(shown["total"], abs=0.05)

    def test_the_spans_are_the_steps_of_the_stats_nested_as_in_the_contract(self, real_call, spans):
        stats = real_call.stats
        by_id = {s.context.span_id: s for s in spans.get_finished_spans()}
        by_name = finished_by_name(spans)
        children = step_spans(spans)
        assert [c.name for c in children] == ["queue_wait", *stats.steps, "store_save"]
        for child in children:
            parent = by_id[child.parent.span_id]
            assert parent.start_time <= child.start_time <= child.end_time <= parent.end_time, child.name
            in_pipeline = child.name in timing.PIPELINE_STEPS
            assert parent.name == ("pii_shield.anonymize" if in_pipeline else "POST /anonymize_unique"), child.name
        for step, ms in stats.steps.items():  # as long as the step: the offsets were not merely clamped into place
            span = by_name[step]
            assert (span.end_time - span.start_time) / 1e6 == pytest.approx(ms, abs=0.02), step
            wanted = {
                f"pii.detail.{key.partition('.')[2]}_ms": round(value, 1)
                for key, value in stats.details.items()
                if key.startswith(f"{step}.")
            }
            assert {k: v for k, v in span.attributes.items() if k.startswith("pii.detail.")} == wanted, step
        assert by_name["pii_shield.anonymize"].attributes["pii.ner_windows"] == stats.ner_windows
        library_start = by_name["queue_wait"].end_time  # the library starts when the wait for a thread ends
        first_offset: dict[str, float] = {}
        for step, offset_ms, _ in stats.timeline:
            first_offset.setdefault(step, offset_ms)
        for step, offset_ms in first_offset.items():
            assert (by_name[step].start_time - library_start) / 1e6 == pytest.approx(offset_ms, abs=0.02), step

    def test_the_histogram_records_the_steps_of_the_stats(self, real_call, step_metrics):
        stats = real_call.stats
        points = {p.attributes["step"]: p for p in histogram_points(step_metrics, "pii.step.duration")}
        assert set(points) == {"queue_wait", *stats.steps, *stats.details, "store_save", "other"}
        for key, ms in {**stats.steps, **stats.details}.items():
            assert points[key].count == 1 and points[key].sum == pytest.approx(ms), key
        assert {p.attributes["size"] for p in points.values()} == {main._size_class(len(real_call.text))}
        shown = dict(parse_server_timing(real_call.response.headers["Server-Timing"]))
        assert points["other"].sum == pytest.approx(shown["other"], abs=0.06)


class TestHistogram:
    def test_the_attributes_are_exactly_step_and_size(self, client, fake_library, step_metrics):
        app_id = register_app(client, "SomeApp")
        anonymize(client, headers={"X-App-Id": app_id})
        points = histogram_points(step_metrics, "pii.step.duration")
        assert points
        for point in points:
            assert set(point.attributes) == {"step", "size"}
            assert point.attributes["size"] == "small"

    def test_every_step_other_and_detail_is_recorded_once_and_total_is_not(self, client, fake_library, step_metrics):
        anonymize(client)
        points = {p.attributes["step"]: p for p in histogram_points(step_metrics, "pii.step.duration")}
        assert set(points) == {"queue_wait", *LIB_STEPS, *LIB_DETAILS, "store_save", "other"}
        assert all(point.count == 1 for point in points.values())
        for key, expected in {**LIB_STEPS, **LIB_DETAILS}.items():
            assert points[key].sum == pytest.approx(expected)
        assert all(("." in key) == (key in LIB_DETAILS) for key in points)  # only details have a dot

    def test_the_size_label_follows_the_text(self, client, fake_library, step_metrics):
        anonymize(client, "x" * 600)
        sizes = {p.attributes["size"] for p in histogram_points(step_metrics, "pii.step.duration")}
        assert sizes == {"medium"}

    def test_the_histogram_agrees_with_the_header(self, client, fake_library, step_metrics):
        entries = dict(parse_server_timing(anonymize(client).headers["Server-Timing"]))
        points = {p.attributes["step"]: p for p in histogram_points(step_metrics, "pii.step.duration")}
        for name in ("queue_wait", "store_save"):
            assert points[name].sum == pytest.approx(entries[name], abs=0.006)
        assert points["other"].sum == pytest.approx(entries["other"], abs=0.06)


class TestErrorPathsAreUnaffected:
    def test_an_unknown_application_is_still_a_404_without_timing(self, client, fake_library):
        response = client.post("/anonymize_unique", json={"text": "x"}, headers={"X-App-Id": "missing"})
        assert response.status_code == 404
        assert response.json() == {"detail": "Application 'missing' not found."}
        assert "server-timing" not in response.headers

    def test_an_unknown_session_is_still_a_404_without_timing(self, client):
        response = client.post("/deanonymize", json={"id": "nope", "text": "x"})
        assert response.status_code == 404 and "server-timing" not in response.headers

    def test_an_unknown_route_is_still_a_404(self, client):
        response = client.get("/no-such-route")
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
        assert "server-timing" not in response.headers

    def test_an_engine_failure_is_still_a_500_with_the_same_body(self, client, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("model exploded")

        monkeypatch.setattr(main.engine, "anonymize", boom)
        response = client.post("/anonymize_unique", json={"text": "x"})
        assert response.status_code == 500 and response.json() == {"detail": "model exploded"}
        assert "server-timing" not in response.headers

    def test_a_failure_while_reporting_never_fails_the_request(self, client, fake_library, monkeypatch, caplog):
        def broken(report, size):
            raise RuntimeError("metrics are down")

        monkeypatch.setattr(timing, "record_step_metrics", broken)
        with caplog.at_level(logging.WARNING, logger="pii-shield"):
            response = client.post("/anonymize_unique", json={"text": "Rajesh wrote"})
        assert response.status_code == 200 and response.json()["anonymized_text"] == "{PERSON_1} wrote"
        assert "Could not report the step timings" in caplog.text


class TestDeanonymize:
    def test_the_light_header_has_the_restore_steps(self, client, fake_library):
        body = anonymize(client).json()
        response = client.post("/deanonymize", json={"id": body["id"], "text": body["anonymized_text"]})
        assert response.status_code == 200 and response.json()["text"] == "Rajesh wrote"
        entries = parse_server_timing(response.headers["Server-Timing"])
        assert [name for name, _ in entries] == ["store_get", "restore", "other", "total"]
        assert sum(ms for name, ms in entries if name != "total") == pytest.approx(dict(entries)["total"], abs=0.05)

    def test_app_config_comes_first_with_an_app_id(self, client, fake_library):
        app_id = register_app(client)
        body = anonymize(client, headers={"X-App-Id": app_id}).json()
        response = client.post(
            "/deanonymize", json={"id": body["id"], "text": body["anonymized_text"]}, headers={"X-App-Id": app_id}
        )
        assert [n for n, _ in parse_server_timing(response.headers["Server-Timing"])][:2] == ["app_config", "store_get"]

    def test_it_adds_no_step_spans_and_no_histogram_records(self, client, fake_library, spans, step_metrics):
        body = anonymize(client).json()
        spans.clear()
        client.post("/deanonymize", json={"id": body["id"], "text": body["anonymized_text"]})
        assert step_spans(spans) == []
        assert {"POST /deanonymize", "pii_shield.deanonymize"} == {s.name for s in spans.get_finished_spans()}
        steps_seen = {p.attributes["step"] for p in histogram_points(step_metrics, "pii.step.duration")}
        assert not {"store_get", "restore"} & steps_seen


class TestAuthentication:
    @pytest.fixture()
    def auth_on(self):
        idp = FakeIdp()
        settings = idp.settings()
        configure_auth(settings, TokenValidator(settings, JwksCache(settings, http_get=idp.http_get)))
        return idp

    def test_auth_is_the_first_step_and_a_child_of_the_server_span(self, client, fake_library, spans, auth_on):
        token = auth_on.token(roles=["PIIShield.User"])
        response = anonymize(client, headers={"Authorization": f"Bearer {token}"})
        entries = parse_server_timing(response.headers["Server-Timing"])
        assert entries[0][0] == "auth" and entries[1][0] == "queue_wait"
        assert sum(ms for name, ms in entries if "." not in name and name != "total") == pytest.approx(
            dict(entries)["total"], abs=0.05
        )
        server = server_span(spans)
        auth = finished_by_name(spans)["auth"]
        assert auth.parent.span_id == server.context.span_id
        assert server.start_time <= auth.start_time <= auth.end_time <= server.end_time
        assert auth.attributes["pii.step"] == "auth"
        assert (auth.end_time - auth.start_time) / 1e6 == pytest.approx(entries[0][1], abs=0.01)

    def test_deanonymize_reports_it_too(self, client, fake_library, auth_on):
        headers = {"Authorization": f"Bearer {auth_on.token(roles=['PIIShield.User'])}"}
        body = anonymize(client, headers=headers).json()
        payload = {"id": body["id"], "text": body["anonymized_text"]}
        response = client.post("/deanonymize", json=payload, headers=headers)
        assert [n for n, _ in parse_server_timing(response.headers["Server-Timing"])][:2] == ["auth", "store_get"]

    def test_the_total_starts_with_the_authentication(self, client, fake_library, auth_on, monkeypatch):
        delay = 0.05
        real = TokenValidator.avalidate

        async def slow(self, token):
            import asyncio

            await asyncio.sleep(delay)
            return await real(self, token)

        monkeypatch.setattr(TokenValidator, "avalidate", slow)
        token = auth_on.token(roles=["PIIShield.User"])
        entries = dict(parse_server_timing(anonymize(client, headers={"Authorization": f"Bearer {token}"}).headers[
            "Server-Timing"
        ]))
        assert entries["auth"] >= delay * 1000 - 1
        assert entries["total"] >= entries["auth"] + 30  # the 30 ms engine comes on top

    def test_a_public_route_and_a_rejected_request_leave_no_timing_behind(self, client, auth_on):
        assert "server-timing" not in client.get("/health").headers
        response = client.post("/anonymize_unique", json={"text": "x"})
        assert response.status_code == 401 and "server-timing" not in response.headers


class TestLogLine:
    def test_it_keeps_its_prefix_and_appends_the_size_the_total_the_windows_and_the_steps(
        self, client, fake_library, caplog
    ):
        caplog.set_level(logging.INFO, logger="pii-shield")
        body = anonymize(client, "Rajesh wrote the report").json()
        line = next(r.getMessage() for r in caplog.records if "Unique anonymization complete" in r.getMessage())
        assert line.startswith(f"Unique anonymization complete: 1 entities, id={body['id']} chars=23 total_ms=")
        match = re.search(r" chars=23 total_ms=(\d+\.\d) ner_windows=3 steps=(\S+)$", line)
        assert match, line
        pairs = [item.split(":") for item in match[2].split(",")]
        assert [name for name, _ in pairs] == ["queue_wait", *LIB_STEPS, "store_save", "other"]
        assert all(re.fullmatch(r"\d+\.\d", ms) for _, ms in pairs)
        assert dict(pairs)["nlp_pass1"] == "6.0" and dict(pairs)["handling"] == "0.5"
        assert float(match[1]) >= 30  # the stand-in engine alone takes 30 ms

    def test_the_total_is_the_one_of_the_header(self, client, fake_library, caplog):
        caplog.set_level(logging.INFO, logger="pii-shield")
        header_total = dict(parse_server_timing(anonymize(client).headers["Server-Timing"]))["total"]
        line = next(r.getMessage() for r in caplog.records if "Unique anonymization complete" in r.getMessage())
        assert float(re.search(r" total_ms=(\d+\.\d)", line)[1]) == pytest.approx(header_total, abs=0.06)

    def test_the_line_is_the_old_one_when_the_report_cannot_be_made(self, client, fake_library, monkeypatch, caplog):
        def broken(*args, **kwargs):
            raise RuntimeError("no report")

        monkeypatch.setattr(timing.RequestClock, "anonymize_report", broken)
        caplog.set_level(logging.INFO, logger="pii-shield")
        body = anonymize(client, "Rajesh wrote the report").json()
        line = next(r.getMessage() for r in caplog.records if "Unique anonymization complete" in r.getMessage())
        assert line == f"Unique anonymization complete: 1 entities, id={body['id']} chars=23"

    def test_deanonymize_logs_its_total(self, client, fake_library, caplog):
        body = anonymize(client).json()
        caplog.set_level(logging.INFO, logger="pii-shield")
        response = client.post("/deanonymize", json={"id": body["id"], "text": body["anonymized_text"]})
        line = next(r.getMessage() for r in caplog.records if "De-anonymization complete" in r.getMessage())
        match = re.fullmatch(rf"De-anonymization complete: id={body['id']} total_ms=(\d+\.\d)", line)
        assert match, line
        assert float(match[1]) == pytest.approx(dict(parse_server_timing(response.headers["Server-Timing"]))["total"], abs=0.06)


class TestReport:
    """The merge of the API's steps with the library's, from numbers made up by hand."""

    def test_the_header_follows_the_contract_example(self):
        report = StepReport(
            steps=[
                Step("app_config", 0.411, 0),
                Step("queue_wait", 0.119, 0),
                Step("nlp_pass1", 388.123, 0),
                Step("analyze", 15.8, 0),
                Step("recovery_pass2", 377.0, 0),
                Step("post_processing", 1.02, 0),
                Step("handling", 0.06, 0),
                Step("store_save", 2.41, 0),
            ],
            details={
                "nlp_pass1": {"ner_model": 361.03},
                "analyze": {"context_enhance": 1.61, "dedup": 0.7},
                "recovery_pass2": {"ner_model": 351.2},
            },
            ner_windows=5,
            other_ms=3.2,
            total_ms=788.14,
        )
        assert timing.server_timing_header(report) == (
            "app_config;dur=0.41, queue_wait;dur=0.12, nlp_pass1;dur=388.12, nlp_pass1.ner_model;dur=361.03, "
            "analyze;dur=15.80, analyze.context_enhance;dur=1.61, analyze.dedup;dur=0.70, "
            "recovery_pass2;dur=377.00, recovery_pass2.ner_model;dur=351.20, post_processing;dur=1.02, "
            "handling;dur=0.06, store_save;dur=2.41, other;dur=3.20, total;dur=788.14"
        )

    def test_other_is_worked_out_from_the_printed_numbers_and_never_goes_below_zero(self):
        steps = [Step("a", 0.004, 0)] * 3 + [Step("b", 1.0, 0)]
        report = StepReport(steps, {}, 0, 0.0, 1.0)
        header = dict(parse_server_timing(timing.server_timing_header(report)))
        assert header["other"] == 0.0 and header["total"] == 1.0  # 3 x 0.00 + 1.00 already is the total
        shrunk = StepReport([Step("a", 5.0, 0)], {}, 0, 0.0, 2.0)  # steps longer than the total: a clock quirk
        assert dict(parse_server_timing(timing.server_timing_header(shrunk)))["other"] == 0.0

    def test_the_printed_steps_and_other_add_up_to_the_printed_total(self):
        steps = [Step(f"s{i}", 1.0049, 0) for i in range(9)]
        report = StepReport(steps, {}, 0, 0.0, 9.0441 + 0.3)
        entries = parse_server_timing(timing.server_timing_header(report))
        assert sum(ms for name, ms in entries if name != "total") == pytest.approx(dict(entries)["total"], abs=1e-9)

    def test_a_report_lays_the_library_out_from_the_moment_the_pool_job_started(self):
        clock = RequestClock(started=100.0)
        clock.record("app_config", 1_000_000_000, 0.4)
        clock.record("store_save", 1_090_000_000, 2.0)
        pool = PoolTiming(queue_ms=1.5, started_ns=1_050_000_000)
        report = clock.anonymize_report(make_stats(), pool, now=100.1)
        by_name = {s.name: s for s in report.steps}
        assert [s.name for s in report.steps] == [
            "app_config", "queue_wait", "nlp_pass1", "analyze", "recovery_pass2", "post_processing", "handling",
            "store_save",
        ]  # fmt: skip
        assert by_name["queue_wait"].start_ns + 1_500_000 == pool.started_ns
        assert by_name["nlp_pass1"].start_ns == pool.started_ns
        assert by_name["analyze"].start_ns == pool.started_ns + 6_000_000
        assert by_name["handling"].start_ns == pool.started_ns + 14_000_000
        assert report.total_ms == pytest.approx(100.0)
        assert report.other_ms == pytest.approx(100.0 - (0.4 + 1.5 + sum(LIB_STEPS.values()) + 2.0))
        assert report.ner_windows == 3 and report.details["analyze"] == {"context_enhance": 0.4, "dedup": 0.2}

    def test_details_come_out_in_the_order_of_the_contract_whatever_the_order_they_arrive_in(self):
        stats = make_stats(details={"analyze.dedup": 0.2, "analyze.context_enhance": 0.4, "nlp_pass1.ner_model": 5.0})
        report = RequestClock(started=0.0).anonymize_report(stats, PoolTiming(0.0, 0), now=1.0)
        assert list(report.details["analyze"]) == ["context_enhance", "dedup"]
        header = timing.server_timing_header(report)
        assert header.index("analyze;") < header.index("analyze.context_enhance") < header.index("analyze.dedup")

    def test_a_library_without_a_timeline_is_laid_out_step_after_step(self):
        stats = make_stats(timeline=[])
        report = RequestClock(started=0.0).anonymize_report(stats, PoolTiming(0.0, 1_000), now=1.0)
        starts = [s.start_ns - 1_000 for s in report.steps if s.name in LIB_STEPS]
        assert starts == [0, 6_000_000, 9_000_000, 13_000_000, 14_000_000]

    def test_a_library_without_steps_leaves_only_the_api_steps(self):
        report = RequestClock(started=0.0).anonymize_report(AnonymizeStats(), PoolTiming(0.5, 10**9), now=0.01)
        assert [s.name for s in report.steps] == ["queue_wait"]
        assert report.other_ms == pytest.approx(9.5)

    def test_the_clock_starts_at_the_authentication_when_it_ran(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_timing=(50.0, 2_000_000_000, 3.0)))
        clock = RequestClock.begin(request)
        report = clock.anonymize_report(make_stats(), PoolTiming(0.0, 2_100_000_000), now=50.5)
        assert report.steps[0] == Step("auth", 3.0, 2_000_000_000)
        assert report.total_ms == pytest.approx(500.0)
        no_auth = RequestClock.begin(SimpleNamespace(state=SimpleNamespace()))
        assert "auth" not in [s.name for s in no_auth.deanonymize_report().steps]

    def test_the_step_context_manager_times_the_block(self):
        clock = RequestClock()
        before = time.time_ns()
        with clock.step("store_get"):
            time.sleep(0.02)
        report = clock.deanonymize_report()
        (step,) = report.steps
        assert step.name == "store_get" and 20 <= step.ms < 200 and step.start_ns >= before
        assert report.total_ms >= step.ms
