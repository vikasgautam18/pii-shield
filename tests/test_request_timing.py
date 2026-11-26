"""The per-request log line: ``<METHOD> <path> -> <status> in <ms> ms`` for every request except ``/health``."""

import logging
import re
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from app import audit_store, main
from app.request_timing import RequestTimingMiddleware

LINE = re.compile(r"^(?P<method>[A-Z]+) (?P<path>\S+) -> (?P<status>\d{3}) in (?P<ms>\d+\.\d) ms$")


def request_lines(caplog) -> list[re.Match]:
    return [m for m in (LINE.match(r.getMessage()) for r in caplog.records if r.name == "pii-shield") if m]


@pytest.fixture(autouse=True)
def _temp_audit_db(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_store, "AUDIT_DB_PATH", str(tmp_path / "audit.db"))


@pytest.fixture()
def tiny():
    """A small app with only this middleware, so nothing else logs or adds headers."""
    app = FastAPI()
    app.add_middleware(RequestTimingMiddleware)

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/boom")
    def boom():
        raise RuntimeError("exploded")

    @app.get("/slow-stream")
    def slow_stream():
        def body():
            yield b"first"
            time.sleep(0.15)  # the response has started, the handler is done: the last byte is still to come
            yield b"last"

        return StreamingResponse(body())

    return app


class TestTheLine:
    def test_every_request_is_logged_with_method_path_status_and_milliseconds(self, tiny, caplog):
        caplog.set_level(logging.INFO, logger="pii-shield")
        client = TestClient(tiny)
        assert client.get("/ok").status_code == 200
        assert client.get("/nowhere").status_code == 404
        assert client.post("/ok").status_code == 405
        found = [(m["method"], m["path"], m["status"]) for m in request_lines(caplog)]
        assert found == [("GET", "/ok", "200"), ("GET", "/nowhere", "404"), ("POST", "/ok", "405")]

    def test_health_is_not_logged(self, tiny, caplog):
        caplog.set_level(logging.INFO, logger="pii-shield")
        assert TestClient(tiny).get("/health").status_code == 200
        assert request_lines(caplog) == []

    def test_a_request_that_raises_is_logged_as_the_500_the_client_gets(self, tiny, caplog):
        caplog.set_level(logging.INFO, logger="pii-shield")
        assert TestClient(tiny, raise_server_exceptions=False).get("/boom").status_code == 500
        assert [(m["path"], m["status"]) for m in request_lines(caplog)] == [("/boom", "500")]

    def test_the_time_runs_until_the_last_byte_was_sent(self, tiny, caplog):
        caplog.set_level(logging.INFO, logger="pii-shield")
        TestClient(tiny).get("/slow-stream")
        (line,) = request_lines(caplog)
        assert float(line["ms"]) >= 140

    def test_it_only_logs(self, tiny):
        response = TestClient(tiny).get("/ok")
        assert "server-timing" not in response.headers and response.json() == {"ok": True}

    def test_a_path_cannot_forge_a_log_line(self, tiny, caplog):
        caplog.set_level(logging.INFO, logger="pii-shield")
        TestClient(tiny).get("/x%0aGET /admin -> 200 in 0.0 ms")
        (line,) = request_lines(caplog)
        assert "\n" not in line.string and line["status"] == "404"
        assert "%0A" in line["path"]

    def test_requests_that_are_not_http_pass_through_untouched(self):
        seen = []

        async def downstream(scope, receive, send):
            seen.append(scope["type"])

        import asyncio

        loop = asyncio.new_event_loop()
        try:
            for scope_type in ("lifespan", "websocket"):
                loop.run_until_complete(RequestTimingMiddleware(downstream)({"type": scope_type}, None, None))
        finally:
            loop.close()
        assert seen == ["lifespan", "websocket"]


@pytest.fixture(scope="module")
def exporter():
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.shutdown()  # a span processor cannot be removed; this makes it inert


class TestInsideTheServerSpan:
    """In the real app the line is logged inside the request's server span, so it carries the trace id."""

    def test_the_line_carries_the_trace_id_of_the_server_span(self, exporter, caplog):
        exporter.clear()
        caplog.set_level(logging.INFO, logger="pii-shield")
        client = TestClient(main.app)
        client.get("/supported-entities")
        (server,) = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
        records = [r for r in caplog.records if LINE.match(r.getMessage())]
        assert [r.getMessage().split(" in ")[0] for r in records] == ["GET /supported-entities -> 200"]
        assert records[0].otelTraceID == format(server.context.trace_id, "032x")
        assert records[0].otelSpanID == format(server.context.span_id, "016x")

    def test_the_real_app_logs_errors_too_but_not_the_probe(self, caplog):
        caplog.set_level(logging.INFO, logger="pii-shield")
        client = TestClient(main.app)
        client.get("/health")
        assert client.post("/anonymize_unique", json={"text": "x"}, headers={"X-App-Id": "missing"}).status_code == 404
        assert client.post("/anonymize_unique", json={}).status_code == 422
        assert [(m["path"], m["status"]) for m in request_lines(caplog)] == [
            ("/anonymize_unique", "404"),
            ("/anonymize_unique", "422"),
        ]
