"""
Structured logging configuration.

Every log record automatically receives a `trace_id` field injected by
TraceIdFilter.  The value is resolved in priority order:
  1. Current OpenTelemetry span trace-id (if OTel is initialised and a span is active)
  2. The request-scoped ContextVar set by the HTTP middleware

This means all log lines within a single HTTP request share the same trace_id
whether or not OTel distributed tracing is enabled.
"""

import contextvars
import logging

# Set by _request_id_middleware for every inbound request
_TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


def set_trace_id(trace_id: str) -> None:
    _TRACE_ID.set(trace_id)


def get_trace_id() -> str:
    return _TRACE_ID.get()


class TraceIdFilter(logging.Filter):
    """Inject trace_id into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Prefer the active OTel span's trace-id (more precise for sub-spans)
        try:
            from opentelemetry import trace as _otel_trace
            ctx = _otel_trace.get_current_span().get_span_context()
            if ctx.is_valid:
                record.trace_id = format(ctx.trace_id, "032x")
                return True
        except ImportError:
            pass  # OTel not installed, fall through to context-var trace_id

        record.trace_id = _TRACE_ID.get()
        return True


def get_logging_config(log_level: str) -> dict:
    """Return a logging.config.dictConfig-compatible dict."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "trace_id": {
                "()": "medwear_med_api.logging_config.TraceIdFilter",
            },
        },
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                # trace_id is injected by the filter → serialised automatically
                "format": "%(asctime)s %(levelname)s %(name)s %(trace_id)s %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "filters": ["trace_id"],
            },
        },
        "root": {
            "handlers": ["console"],
            "level": log_level,
        },
    }
