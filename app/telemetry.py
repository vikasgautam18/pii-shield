"""
OpenTelemetry bootstrap for PII Shield.

Supports two backends selected by environment variables:
  - Azure Monitor: set APPLICATIONINSIGHTS_CONNECTION_STRING
  - OTLP gRPC (local dev): set OTEL_EXPORTER_OTLP_ENDPOINT (default http://otel-lgtm:4317)

Importing this module registers the tracer and logger providers and the logging bridge, but
starts no thread and creates no exporter. Call init_telemetry() once in every worker process
to attach the exporters; app.main does it from the FastAPI lifespan, which runs after gunicorn
has forked the worker.

Never do that in the process that forks (gunicorn --preload imports the app in the master
first). The metric reader keeps a thread and holds a collection lock while the registered-apps
gauge reads Redis. If the fork lands while that lock is held, the worker inherits it locked:
its reader never exports again, so every counter and histogram the worker records is lost,
while traces and logs (which the SDK re-initialises after a fork) keep flowing.

Two more things are settled here because they belong to the process, not to a request:

* The ``traceresponse`` header (the W3C response counterpart of ``traceparent``) is switched on by registering a
  response propagator, which is plain data and starts nothing, so it is also done at import. The ASGI middleware
  looks the propagator up on every response, so it does not matter whether this runs before or after
  ``FastAPIInstrumentor.instrument_app``.
* Every duration histogram uses ``BOUNDS_MS``. The SDK's default boundaries stop at 10,000 ms, so a request
  that took 12-25 s was counted in the overflow bucket and every quantile above 10 s read as exactly 10 s.
"""

import logging
import os
import socket

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.propagators import TraceResponsePropagator, set_global_response_propagator
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE = os.getenv("OTEL_SERVICE_NAME", "pii-shield")
APPINSIGHTS_CS = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-lgtm:4317")

# Bucket boundaries, in milliseconds, of every duration histogram: a request of a few milliseconds up to a
# two-minute timeout.
BOUNDS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 20000, 30000, 60000, 120000)

_resource = Resource.create(
    {
        SERVICE_NAME: SERVICE,
        SERVICE_VERSION: "0.1.0",
        "service.instance.id": os.getenv("OTEL_SERVICE_INSTANCE_ID", "pii-shield-1"),
    }
)


def metrics_instance_id() -> str:
    """The ``service.instance.id`` under which this process exports its metrics: ``<replica or host>-<pid>``.

    Metrics are cumulative per series, and gunicorn runs several workers that share one replica. With the same
    id their series collided in Prometheus, each export overwriting the others, so only a fraction of the
    requests showed up. Traces and logs are events, not running totals, so they keep the shared resource above.
    """
    replica = os.getenv("OTEL_SERVICE_INSTANCE_ID") or os.getenv("CONTAINER_APP_REPLICA_NAME")
    return f"{replica or socket.gethostname()}-{os.getpid()}"


def metrics_resource() -> Resource:
    """The shared resource with this process's own ``service.instance.id``; call it after the fork."""
    return _resource.merge(Resource({"service.instance.id": metrics_instance_id()}))


def _duration_views() -> list[View]:
    """Give the HTTP server duration of the FastAPI instrumentation the same boundaries as ours.

    The instrumentation creates ``http.server.duration`` (milliseconds) under the default semantic conventions,
    and ``http.server.request.duration`` (seconds, with boundaries that end at 10 s) when
    ``OTEL_SEMCONV_STABILITY_OPT_IN`` asks for the stable ones.
    """
    return [
        View(instrument_name="http.server.duration", aggregation=ExplicitBucketHistogramAggregation(BOUNDS_MS)),
        View(
            instrument_name="http.server.request.duration",
            aggregation=ExplicitBucketHistogramAggregation(tuple(bound / 1000 for bound in BOUNDS_MS)),
        ),
    ]


def build_meter_provider(metric_readers, resource: Resource | None = None, **kwargs) -> MeterProvider:
    """The meter provider of one worker process: per-process identity, long-request histogram boundaries."""
    if resource is None:
        resource = metrics_resource()
    return MeterProvider(resource=resource, metric_readers=metric_readers, views=_duration_views(), **kwargs)


def _create_exporters():
    """Return (trace_exporter, metric_exporter, log_exporter) for the active backend."""
    if APPINSIGHTS_CS:
        from azure.monitor.opentelemetry.exporter import (
            AzureMonitorLogExporter,
            AzureMonitorMetricExporter,
            AzureMonitorTraceExporter,
        )
        logging.getLogger(__name__).info("Using Azure Monitor exporters")
        return (
            AzureMonitorTraceExporter(connection_string=APPINSIGHTS_CS),
            AzureMonitorMetricExporter(connection_string=APPINSIGHTS_CS),
            AzureMonitorLogExporter(connection_string=APPINSIGHTS_CS),
        )

    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    logging.getLogger(__name__).info("Using OTLP gRPC exporters → %s", OTLP_ENDPOINT)
    return (
        OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True),
        OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
        OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    )


_initialized = False

# Registered at import so the logging bridge below finds them (and startup logs keep their format), but
# inert: nothing is exported until init_telemetry() adds the processors.
_tracer_provider = TracerProvider(resource=_resource)
trace.set_tracer_provider(_tracer_provider)
_logger_provider = LoggerProvider(resource=_resource)
set_logger_provider(_logger_provider)
set_global_response_propagator(TraceResponsePropagator())

# Use the instrumentation-based logging bridge (avoids deprecation of sdk LoggingHandler)
LoggingInstrumentor().instrument(set_logging_format=True, log_level=logging.INFO)


def init_telemetry() -> None:
    """Attach the exporters for this process. Idempotent.

    The meter provider is created here, not at import, because a provider's metric readers are fixed when
    it is built. Instruments created earlier through the global API (app.main creates its counters at
    import) are proxies that bind to it once it is registered. Building it here, after the fork, is also what
    gives every worker its own ``service.instance.id`` (see ``metrics_instance_id``).
    """
    global _initialized
    if _initialized:
        return

    trace_exporter, metric_exporter, log_exporter = _create_exporters()

    _tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
    _logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

    # 5 s keeps the dashboards fresh; OTEL_METRIC_EXPORT_INTERVAL (milliseconds) overrides it.
    export_interval_millis = None if os.getenv("OTEL_METRIC_EXPORT_INTERVAL") else 5000
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=export_interval_millis,
    )
    metrics.set_meter_provider(build_meter_provider([metric_reader]))

    _initialized = True
