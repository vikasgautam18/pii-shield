"""
OpenTelemetry bootstrap for PII Shield.

Supports two backends selected by environment variables:
  - Azure Monitor: set APPLICATIONINSIGHTS_CONNECTION_STRING
  - OTLP gRPC (local dev): set OTEL_EXPORTER_OTLP_ENDPOINT (default http://otel-lgtm:4317)

Import this module early — before the FastAPI app is created — so the
providers are registered globally.
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


_trace_exporter, _metric_exporter, _log_exporter = _create_exporters()

# ── Tracing ────────────────────────────────────────────────────────────────
_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(_trace_exporter))
trace.set_tracer_provider(_tracer_provider)

# ── Metrics ────────────────────────────────────────────────────────────────
_metric_reader = PeriodicExportingMetricReader(
    _metric_exporter,
    export_interval_millis=5000,
)
_meter_provider = MeterProvider(resource=_resource, metric_readers=[_metric_reader])
metrics.set_meter_provider(_meter_provider)

# ── Logging ────────────────────────────────────────────────────────────────
_logger_provider = LoggerProvider(resource=_resource)
_logger_provider.add_log_record_processor(BatchLogRecordProcessor(_log_exporter))
set_logger_provider(_logger_provider)

# Use the instrumentation-based logging bridge (avoids deprecation of sdk LoggingHandler)
LoggingInstrumentor().instrument(set_logging_format=True, log_level=logging.INFO)
