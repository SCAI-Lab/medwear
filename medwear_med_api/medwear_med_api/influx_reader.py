import os
import re
from influxdb_client import InfluxDBClient
from datetime import datetime
from typing import List, Optional, Dict

INFLUX_URL = os.getenv("INFLUX_URL", "http://medwear-influxdb:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = os.getenv("INFLUX_ORG", "medwear")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "wearables")

if not INFLUX_TOKEN:
    raise RuntimeError("INFLUX_TOKEN env variable is not set. Cannot start without it.")

INLINE_SAMPLE_LIMIT = 10_000

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_SAFE_STR_RE = re.compile(r'^[\w\-\.]+$')  # alphanumeric, hyphen, dot, underscore


def _flux_str(value: str) -> str:
    """Escape a string for safe embedding inside a Flux double-quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _require_uuid(value: str) -> str:
    if not _UUID_RE.match(value):
        raise ValueError(f"Invalid UUID format: {value!r}")
    return value


def _require_safe(value: str) -> str:
    if not _SAFE_STR_RE.match(value):
        raise ValueError(f"Unsafe value for Flux query: {value!r}")
    return value


def read_signals_by_uuid(batch_uuid: str) -> List[float]:
    safe_uuid = _require_uuid(batch_uuid)
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    try:
        query_api = client.query_api()
        flux = f"""
from(bucket: "{_flux_str(INFLUX_BUCKET)}")
  |> range(start: 0)
  |> filter(fn: (r) => r["batch_uuid"] == "{_flux_str(safe_uuid)}")
  |> sort(columns: ["_time"])
"""
        tables = query_api.query(flux)
    finally:
        client.close()

    samples = []
    for table in tables:
        for record in table.records:
            samples.append(record.get_value())
    return samples


def read_signals_by_filters(
    patient_ids: Optional[List[str]] = None,
    signal_type: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    device_id: Optional[str] = None,
    batch_uuids: Optional[List[str]] = None,
) -> Dict[str, List[float]]:
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    try:
        query_api = client.query_api()

        range_start = start.isoformat() + "Z" if start else "0"
        range_stop = end.isoformat() + "Z" if end else "now()"

        flux = f"""
from(bucket: "{_flux_str(INFLUX_BUCKET)}")
  |> range(start: {range_start}, stop: {range_stop})
"""
        if signal_type:
            flux += f'  |> filter(fn: (r) => r["_measurement"] == "{_flux_str(_require_safe(signal_type.lower()))}")\n'
        if patient_ids:
            ids = " or ".join([f'r["patient_id"] == "{_flux_str(_require_safe(pid))}"' for pid in patient_ids])
            flux += f'  |> filter(fn: (r) => {ids})\n'
        if device_id:
            flux += f'  |> filter(fn: (r) => r["device_id"] == "{_flux_str(_require_safe(device_id))}")\n'
        if batch_uuids:
            safe_uuids = [_require_uuid(u) for u in batch_uuids]
            uuids = " or ".join([f'r["batch_uuid"] == "{_flux_str(u)}"' for u in safe_uuids])
            flux += f'  |> filter(fn: (r) => {uuids})\n'

        flux += '  |> sort(columns: ["_time"])'

        tables = query_api.query(flux)
    finally:
        client.close()

    result: Dict[str, List[float]] = {}
    for table in tables:
        for record in table.records:
            uuid = record.values.get("batch_uuid", "unknown")
            if uuid not in result:
                result[uuid] = []
            result[uuid].append(record.get_value())
    return result
