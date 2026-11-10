"""Telemetry exporters must be created per worker, after gunicorn forks, never in the preloaded master.

gunicorn --preload imports the app in the master and then forks the workers. A metric reader started in
the master can be forked while its thread holds the collection lock (the registered-apps gauge reads Redis
from inside collect()); the worker then inherits that lock held and never exports a metric again, while
traces and logs keep flowing. On Azure that showed up as AppRequests in Application Insights but no
customMetrics, so the Grafana dashboards stayed empty.

The OpenTelemetry providers are process-global and can be set once, so these tests run small scripts in
child interpreters and read what they export from a local stand-in for the Application Insights endpoint.
"""

import ast
import gzip
import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

pytest.importorskip("azure.monitor.opentelemetry.exporter")

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")

# What gunicorn --preload does: the master imports the app (and registers instruments), then forks a worker
# whose lifespan calls init_telemetry(). If the master had a metric reader it would be inside the gauge
# callback (holding its collection lock) when the fork happens, so the script waits for exactly that moment.
FORK_SCRIPT = """
import os, threading, time
import app.telemetry as telemetry
from opentelemetry import metrics

meter = metrics.get_meter("fork-test")
counter = meter.create_counter("fork.test.requests")
master = os.getpid()
inside_gauge = threading.Event()


def gauge(options):
    if os.getpid() == master:
        inside_gauge.set()
        time.sleep(60)
    return [metrics.Observation(1)]


meter.create_observable_gauge("fork.test.registered", callbacks=[gauge])

if any(t.name == "OtelPeriodicExportingMetricReader" for t in threading.enumerate()):
    inside_gauge.wait(timeout=10)

if os.fork() == 0:
    telemetry.init_telemetry()
    counter.add(1)
    time.sleep(60)
    os._exit(0)
time.sleep(60)
"""

IMPORT_SCRIPT = """
import os, threading
import app.telemetry as telemetry
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider

assert [t.name for t in threading.enumerate()] == ["MainThread"], threading.enumerate()
assert not isinstance(metrics.get_meter_provider(), MeterProvider)

telemetry.init_telemetry()
threads = {t.ident for t in threading.enumerate()}
assert any(t.name == "OtelPeriodicExportingMetricReader" for t in threading.enumerate()), threading.enumerate()
assert isinstance(metrics.get_meter_provider(), MeterProvider)

telemetry.init_telemetry()
assert {t.ident for t in threading.enumerate()} == threads, threading.enumerate()
os._exit(0)
"""


class _IngestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        text = body.decode("utf-8", "ignore")
        try:
            items = json.loads(text)
            items = items if isinstance(items, list) else [items]
        except ValueError:
            items = [json.loads(line) for line in text.splitlines() if line.strip()]
        for item in items:
            data = item.get("data", {})
            if data.get("baseType") == "MetricData":
                self.server.metric_names.update(m["name"] for m in data["baseData"]["metrics"])
        reply = json.dumps({"itemsReceived": len(items), "itemsAccepted": len(items), "errors": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def ingest():
    """A local stand-in for the Application Insights ingestion endpoint that records metric names."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IngestHandler)
    server.daemon_threads = True
    server.metric_names = set()
    Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _child_env(ingest) -> dict:
    return {
        **os.environ,
        "APPLICATIONINSIGHTS_CONNECTION_STRING": (
            "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
            f"IngestionEndpoint=http://127.0.0.1:{ingest.server_address[1]}/"
        ),
        "OTEL_METRIC_EXPORT_INTERVAL": "200",
        "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL": "true",
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
    }


def test_importing_telemetry_starts_nothing_and_init_is_idempotent(ingest):
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_SCRIPT],
        cwd=REPO_ROOT,
        env=_child_env(ingest),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-1500:]


def test_forked_worker_exports_metrics_even_if_the_master_was_mid_collection(ingest, tmp_path):
    log = tmp_path / "child.log"
    with log.open("w") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-c", FORK_SCRIPT],
            cwd=REPO_ROOT,
            env=_child_env(ingest),
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "fork.test.requests" not in ingest.metric_names:
            time.sleep(0.1)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()

    assert "fork.test.requests" in ingest.metric_names, (
        f"the worker never exported its counter; exported: {sorted(ingest.metric_names)}\n" + log.read_text()[-1500:]
    )


def test_main_initialises_telemetry_only_from_the_lifespan():
    tree = ast.parse((REPO_ROOT / "app" / "main.py").read_text())
    lifespan = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_lifespan")
    inside_lifespan = {id(n) for n in ast.walk(lifespan)}
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "init_telemetry"
    ]
    assert calls, "app.main never calls init_telemetry()"
    assert all(id(call) in inside_lifespan for call in calls), (
        "init_telemetry() must only run from the lifespan (once per worker, after the fork), never at import"
    )
