"""
OpenTelemetry distributed tracing initialisation.

Controlled entirely via environment variables — no code changes needed to
switch backends:

  OTEL_SERVICE_NAME              (default: medwear-api)
  OTEL_TRACES_EXPORTER           none | console | otlp   (default: none)
  OTEL_EXPORTER_OTLP_ENDPOINT    e.g. http://jaeger:4317  (used when OTLP)

When OTEL_TRACES_EXPORTER=none (default) the function is a no-op, so there
is zero overhead in development and CI.

When enabled, FastAPI requests, SQLAlchemy queries, and asyncpg calls are
automatically traced.  The OTel trace-id is picked up by TraceIdFilter and
appears in every structured log line, linking logs to traces without any
manual propagation.
"""

import logging
import os

logger = logging.getLogger(__name__)


def init_tracing(app) -> bool:
    """
    Initialise OTel tracing and instrument *app*.

    Returns True if tracing was enabled, False otherwise.
    """
    exporter_type = os.getenv("OTEL_TRACES_EXPORTER", "none").lower()

    if exporter_type == "none":
        logger.info("OpenTelemetry tracing disabled (set OTEL_TRACES_EXPORTER=otlp or console to enable)")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        service_name = os.getenv("OTEL_SERVICE_NAME", "medwear-api")
        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)

        if exporter_type == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
            exporter = OTLPSpanExporter(endpoint=endpoint)
            logger.info(f"OpenTelemetry → OTLP gRPC  endpoint={endpoint}  service={service_name}")

        elif exporter_type == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            exporter = ConsoleSpanExporter()
            logger.info(f"OpenTelemetry → console  service={service_name}")

        else:
            logger.warning(f"Unknown OTEL_TRACES_EXPORTER={exporter_type!r} — tracing disabled")
            return False

        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Instrument FastAPI (adds traceparent / tracestate headers automatically)
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="health,metrics",
        )

        # Instrument SQLAlchemy if the async engine is available
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument()
            logger.info("OpenTelemetry SQLAlchemy instrumentation enabled")
        except ImportError:
            pass

        logger.info("OpenTelemetry initialised")
        return True

    except ImportError as exc:
        logger.warning(f"OpenTelemetry packages missing — tracing disabled: {exc}")
        return False
    except Exception as exc:
        logger.error(f"OpenTelemetry initialisation failed — tracing disabled: {exc}")
        return False


def get_tracer(name: str = "medwear-api"):
    """Return a tracer for creating manual spans."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return _NoopTracer()


class _NoopSpan:
    """No-op span that satisfies the context manager protocol."""
    def __enter__(self): return self
    def __exit__(self, *_): pass


class _NoopTracer:
    """Returned when OTel is not installed — all methods are no-ops."""

    def start_as_current_span(self, name, **_):
        return _NoopSpan()
