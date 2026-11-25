"""Telemetry exporters must be created per worker, after gunicorn forks, never in the preloaded master.

gunicorn --preload imports the app in the master and then forks the workers. A metric reader started in
the master can be forked while its thread holds the collection lock (the registered-apps gauge reads Redis
from inside collect()); the worker then inherits that lock held and never exports a metric again, while
traces and logs keep flowing. On Azure that showed up as AppRequests in Application Insights but no
customMetrics, so the Grafana dashboards stayed empty.

The OpenTelemetry providers are process-global and can be set once, so these tests run small scripts in
child interpreters and read what they export from a local stand-in for the Application Insights endpoint.

Every worker also exports its metrics under its own ``service.instance.id``: with one shared id the cumulative
series of the workers of a replica collided in Prometheus, each export overwriting the others.
"""

import ast
import gzip
import json
import os
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

pytest.importorskip("azure.monitor.opentelemetry.exporter")

import app.telemetry as telemetry

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

# Two workers forked from one master, as gunicorn does: each exports a counter and a span under the identity it
# was given. The master prints their pids so the test can tell which instance id belongs to which worker.
TWO_WORKERS_SCRIPT = """
import os, time
import app.telemetry as telemetry
from opentelemetry import metrics, trace

counter = metrics.get_meter("fork-test").create_counter("fork.test.requests")
tracer = trace.get_tracer("fork-test")
pids = []
for _ in range(2):
    pid = os.fork()
    if pid == 0:
        telemetry.init_telemetry()
        counter.add(1)
        tracer.start_span("fork.test.span").end()
        time.sleep(60)
        os._exit(0)
    pids.append(pid)
print("WORKERS", *pids, flush=True)
time.sleep(60)
"""

IMPORT_SCRIPT = """
import os, threading
import app.telemetry as telemetry
from opentelemetry import metrics
from opentelemetry.instrumentation.propagators import TraceResponsePropagator, get_global_response_propagator
from opentelemetry.sdk.metrics import MeterProvider

assert [t.name for t in threading.enumerate()] == ["MainThread"], threading.enumerate()
assert not isinstance(metrics.get_meter_provider(), MeterProvider)
assert isinstance(get_global_response_propagator(), TraceResponsePropagator)

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
            instance = item.get("tags", {}).get("ai.cloud.roleInstance")
            if data.get("baseType") == "MetricData":
                for metric in data["baseData"]["metrics"]:
                    self.server.metric_names.add(metric["name"])
                    self.server.metric_instances.setdefault(metric["name"], set()).add(instance)
            elif data.get("baseType") == "RemoteDependencyData":
                self.server.span_instances.add(instance)
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
    server.metric_instances = {}  # metric name -> the service.instance.id values it was exported under
    server.span_instances = set()
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
        "OTEL_BSP_SCHEDULE_DELAY": "200",
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


def test_every_forked_worker_exports_its_metrics_under_its_own_instance_id(ingest, tmp_path):
    env = _child_env(ingest)
    env.pop("OTEL_SERVICE_INSTANCE_ID", None)
    env["CONTAINER_APP_REPLICA_NAME"] = "replica-a"
    log = tmp_path / "workers.log"
    with log.open("w") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-c", TWO_WORKERS_SCRIPT],
            cwd=REPO_ROOT,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (
            len(ingest.metric_instances.get("fork.test.requests", ())) < 2 or not ingest.span_instances
        ):
            time.sleep(0.1)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()

    pids = next(line for line in log.read_text().splitlines() if line.startswith("WORKERS ")).split()[1:]
    assert len(pids) == 2
    assert ingest.metric_instances.get("fork.test.requests") == {f"replica-a-{pid}" for pid in pids}, (
        ingest.metric_instances, log.read_text()[-1500:]
    )
    # Traces keep the resource that is shared by the workers.
    assert ingest.span_instances == {"pii-shield-1"}, ingest.span_instances


class TestMetricsInstanceId:
    def test_it_is_the_replica_and_the_process(self, monkeypatch):
        monkeypatch.delenv("OTEL_SERVICE_INSTANCE_ID", raising=False)
        monkeypatch.setenv("CONTAINER_APP_REPLICA_NAME", "pii-shield--abc123")
        assert telemetry.metrics_instance_id() == f"pii-shield--abc123-{os.getpid()}"

    def test_an_explicit_instance_id_wins_over_the_replica_name(self, monkeypatch):
        monkeypatch.setenv("OTEL_SERVICE_INSTANCE_ID", "explicit")
        monkeypatch.setenv("CONTAINER_APP_REPLICA_NAME", "pii-shield--abc123")
        assert telemetry.metrics_instance_id() == f"explicit-{os.getpid()}"

    def test_it_falls_back_to_the_host_name(self, monkeypatch):
        monkeypatch.delenv("OTEL_SERVICE_INSTANCE_ID", raising=False)
        monkeypatch.delenv("CONTAINER_APP_REPLICA_NAME", raising=False)
        assert telemetry.metrics_instance_id() == f"{socket.gethostname()}-{os.getpid()}"

    def test_only_the_metrics_resource_carries_it(self):
        shared = telemetry._resource.attributes
        metrics_attributes = telemetry.metrics_resource().attributes
        assert metrics_attributes["service.instance.id"] == telemetry.metrics_instance_id()
        assert metrics_attributes["service.instance.id"] != shared["service.instance.id"]
        assert {k: v for k, v in metrics_attributes.items() if k != "service.instance.id"} == {
            k: v for k, v in shared.items() if k != "service.instance.id"
        }
        assert telemetry._tracer_provider.resource is telemetry._resource  # traces and logs keep the shared one
        assert telemetry._logger_provider.resource is telemetry._resource
