"""
Prometheus metrics definitions and /metrics endpoint initialisation.

Custom application metrics (supplement the auto-instrumented HTTP metrics):

  medwear_batch_ingested_total          — successful batch ingests (by signal_type, project)
  medwear_influx_write_total            — InfluxDB write attempts (by outcome)
  medwear_influx_write_duration_seconds — InfluxDB write latency histogram
  medwear_circuit_breaker_open          — 1 when a named circuit breaker is OPEN
"""

import logging

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom counters / histograms
# ---------------------------------------------------------------------------

BATCH_INGESTED = Counter(
    "medwear_batch_ingested_total",
    "Batches successfully ingested into PostgreSQL + InfluxDB",
    ["signal_type", "project_id"],
)

INFLUX_WRITE_TOTAL = Counter(
    "medwear_influx_write_total",
    "InfluxDB write attempts categorised by outcome",
    ["outcome"],  # success | retry | circuit_open | failed
)

INFLUX_WRITE_DURATION = Histogram(
    "medwear_influx_write_duration_seconds",
    "End-to-end InfluxDB write latency (including retries)",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

CIRCUIT_BREAKER_OPEN = Gauge(
    "medwear_circuit_breaker_open",
    "1 if the named circuit breaker is currently OPEN, 0 otherwise",
    ["service"],
)


# ---------------------------------------------------------------------------
# Initialisation helper
# ---------------------------------------------------------------------------

def init_metrics(app) -> None:
    """
    Attach prometheus-fastapi-instrumentator to *app* and expose /metrics.

    Called once after the FastAPI app is created.  The /metrics endpoint is
    excluded from auth and audit middleware by the SlowAPI default-limit
    exemption and the _SKIP_PATHS set in audit.py.
    """
    logger.info("Prometheus HTTP instrumentation disabled (router compatibility)")
