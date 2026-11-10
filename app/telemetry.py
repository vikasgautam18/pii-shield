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
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE = os.getenv("OTEL_SERVICE_NAME", "pii-shield")
APPINSIGHTS_CS = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-lgtm:4317")

_resource = Resource.create(
    {
        SERVICE_NAME: SERVICE,
        SERVICE_VERSION: "0.1.0",
        "service.instance.id": os.getenv("OTEL_SERVICE_INSTANCE_ID", "pii-shield-1"),
    }
)


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

# Use the instrumentation-based logging bridge (avoids deprecation of sdk LoggingHandler)
LoggingInstrumentor().instrument(set_logging_format=True, log_level=logging.INFO)


def init_telemetry() -> None:
    """Attach the exporters for this process. Idempotent.

    The meter provider is created here, not at import, because a provider's metric readers are fixed when
    it is built. Instruments created earlier through the global API (app.main creates its counters at
    import) are proxies that bind to it once it is registered.
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
    metrics.set_meter_provider(MeterProvider(resource=_resource, metric_readers=[metric_reader]))

    _initialized = True
