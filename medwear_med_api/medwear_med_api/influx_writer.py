import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from medwear_med_api.circuit_breaker import CircuitBreaker, CircuitOpenError

# Prometheus metrics — imported lazily so tests without prometheus_client still work
try:
    from medwear_med_api.metrics import INFLUX_WRITE_TOTAL, INFLUX_WRITE_DURATION
    _HAS_METRICS = True
except Exception:
    _HAS_METRICS = False

logger = logging.getLogger(__name__)

INFLUX_URL = os.getenv("INFLUX_URL", "http://medwear-influxdb:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = os.getenv("INFLUX_ORG", "medwear")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "medwear")

if not INFLUX_TOKEN:
    raise RuntimeError("INFLUX_TOKEN env variable is not set. Cannot start without it.")

# Circuit breaker: open after 5 consecutive failures, probe again after 60 s
_influx_cb = CircuitBreaker(
    name="influxdb-writer",
    failure_threshold=5,
    recovery_timeout=60.0,
)

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt (1 → 2 → 4)


def _build_points(
    batch_uuid: str,
    patient_id: str,
    device_id: str,
    signal_type: str,
    samples: List[float],
    start_timestamp: datetime,
    sampling_rate_hz: float,
    signal_info: Optional[Dict[str, Any]],
    project_id: Optional[str],
) -> List[Point]:
    points = []
    start_ns = int(start_timestamp.timestamp() * 1_000_000_000)
    ns_per_sample = int(1_000_000_000 / sampling_rate_hz)

    for i, value in enumerate(samples):
        point = (
            Point(signal_type.lower())
            .tag("batch_uuid", batch_uuid)
            .tag("patient_id", str(patient_id))
            .tag("device_id", device_id)
            .tag("signal_type", signal_type.upper())
            .tag("project_id", project_id or "unknown")
            .field("value", float(value))
            .time(start_ns + i * ns_per_sample, WritePrecision.NS)
        )
        if signal_info:
            for key, val in signal_info.items():
                if isinstance(val, (str, int, float, bool)):
                    point = point.tag(f"info_{key}", str(val))
                elif isinstance(val, list) and val:
                    point = point.tag(f"info_{key}", ",".join(str(v) for v in val))
        points.append(point)

    return points


_INFLUX_CHUNK_SIZE = int(os.getenv("INFLUX_CHUNK_SIZE", "10000"))


def _do_write(points: List[Point]) -> None:
    """Single write attempt — raises on any failure. Chunks large payloads."""
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=300_000)
    try:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        for i in range(0, len(points), _INFLUX_CHUNK_SIZE):
            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points[i:i + _INFLUX_CHUNK_SIZE])
    finally:
        client.close()


def write_signal_batch(
    batch_uuid: str,
    patient_id: str,
    device_id: str,
    signal_type: str,
    samples: List[float],
    start_timestamp: datetime,
    sampling_rate_hz: float,
    signal_info: Optional[Dict[str, Any]] = None,
    project_id: Optional[str] = None,
) -> int:
    """
    Write signal samples to InfluxDB.

    Retries up to _RETRY_ATTEMPTS times with exponential backoff.
    Guarded by a circuit breaker: if InfluxDB repeatedly fails, subsequent
    calls raise CircuitOpenError immediately without blocking the write thread.
    """
    points = _build_points(
        batch_uuid, patient_id, device_id, signal_type,
        samples, start_timestamp, sampling_rate_hz, signal_info, project_id,
    )

    last_exc: Exception | None = None
    _t_start = time.monotonic()

    for attempt in range(_RETRY_ATTEMPTS):
        try:
            _influx_cb.call(_do_write, points)
            elapsed = time.monotonic() - _t_start
            if _HAS_METRICS:
                INFLUX_WRITE_TOTAL.labels(outcome="success").inc()
                INFLUX_WRITE_DURATION.observe(elapsed)
            logger.info(
                f"InfluxDB write OK | batch_uuid={batch_uuid} "
                f"samples={len(points)} attempt={attempt + 1} duration_s={elapsed:.3f}"
            )
            return len(points)
        except CircuitOpenError as e:
            if _HAS_METRICS:
                INFLUX_WRITE_TOTAL.labels(outcome="circuit_open").inc()
            logger.error(f"InfluxDB circuit OPEN | batch_uuid={batch_uuid} | {e}")
            raise
        except Exception as e:
            last_exc = e
            if attempt < _RETRY_ATTEMPTS - 1:
                if _HAS_METRICS:
                    INFLUX_WRITE_TOTAL.labels(outcome="retry").inc()
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"InfluxDB write attempt {attempt + 1}/{_RETRY_ATTEMPTS} failed "
                    f"— retrying in {delay:.1f}s | batch_uuid={batch_uuid} | {e}"
                )
                time.sleep(delay)
            else:
                if _HAS_METRICS:
                    INFLUX_WRITE_TOTAL.labels(outcome="failed").inc()
                    INFLUX_WRITE_DURATION.observe(time.monotonic() - _t_start)
                logger.error(
                    f"InfluxDB write FAILED after {_RETRY_ATTEMPTS} attempts "
                    f"| batch_uuid={batch_uuid} | {e}"
                )

    raise last_exc  # type: ignore[misc]
