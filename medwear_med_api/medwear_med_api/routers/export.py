"""
Research data export — WFDB and EDF/EDF+ formats
=================================================
Endpoint: GET /v1/data/signals/{uuid}/export?format=wfdb|edf|csv

These are the de-facto standards for sharing biosignal data:
  WFDB  — PhysioNet / MIT standard (ECG, PPG, BCG research databases)
  EDF+  — European Data Format (clinical polysomnography, EEG, multi-channel)
  CSV   — fallback, always available

Dependencies (added to requirements.txt):
  wfdb      — pip install wfdb
  pyEDFlib  — pip install pyEDFlib

Both are optional at import time — the endpoint returns 501 if the
library is not installed, keeping the rest of the API unaffected.
"""

import io
import logging
import os
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from influxdb_client import InfluxDBClient
from medwear_med_api.auth import get_current_user
from medwear_med_api.db import get_all_projects, get_project_session
from medwear_med_api.errors import api_error
from medwear_med_api.influx_reader import _require_uuid, _flux_str
from medwear_med_api.rate_limit import limiter
from sqlalchemy import text as _text

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Research Export"])


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Required environment variable '{name}' is not set. Check your .env file.")
    return val


_INFLUX_URL = _require_env("INFLUX_URL")
_INFLUX_TOKEN = _require_env("INFLUX_TOKEN")
_INFLUX_ORG = _require_env("INFLUX_ORG")
_INFLUX_BUCKET = _require_env("INFLUX_BUCKET")


# ── Signal fetch from InfluxDB ────────────────────────────────────────────────

def _fetch_samples(batch_uuid: str) -> list[float]:
    """Pull raw signal samples from InfluxDB for a given batch UUID."""
    _require_uuid(batch_uuid)
    client = InfluxDBClient(url=_INFLUX_URL, token=_INFLUX_TOKEN, org=_INFLUX_ORG)
    try:
        query = f"""
        from(bucket: "{_flux_str(_INFLUX_BUCKET)}")
          |> range(start: 1970-01-01T00:00:00Z, stop: 2100-01-01T00:00:00Z)
          |> filter(fn: (r) => r["batch_uuid"] == "{_flux_str(batch_uuid)}")
          |> filter(fn: (r) => r["_field"] == "value")
          |> sort(columns: ["_time"])
        """
        tables = client.query_api().query(query, org=_INFLUX_ORG)
        return [record.get_value() for table in tables for record in table.records]
    finally:
        client.close()


# ── Format builders ───────────────────────────────────────────────────────────

def _build_csv(samples: list[float], meta: dict) -> bytes:
    """Simple per-sample CSV with timestamp column."""
    import csv as _csv
    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["sample_index", "value", "unit", "signal_type"])
    unit = meta.get("units", "unknown")
    sig = meta.get("signal_type", "UNKNOWN")
    for i, v in enumerate(samples):
        writer.writerow([i, v, unit, sig])
    return buf.getvalue().encode()


def _build_wfdb_zip(samples: list[float], meta: dict) -> bytes:
    """
    WFDB record: one .hea header + one .dat binary data file, packaged as ZIP.
    Compatible with PhysioNet wfdb-python and MATLAB wfdb toolbox.
    """
    try:
        import wfdb
        import numpy as np
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=api_error("FORMAT_NOT_AVAILABLE",
                             "wfdb package not installed. Run: pip install wfdb"),
        )

    record_name = meta.get("uuid", "record")[:16].replace("-", "")
    fs = int(meta.get("sampling_rate_hz", 256))
    sig_name = meta.get("signal_type", "signal")
    units = meta.get("units", "mV")

    arr = np.array(samples, dtype=np.float32).reshape(-1, 1)

    # Write to a temp directory in memory via wfdb's io
    import tempfile
    import pathlib
    with tempfile.TemporaryDirectory() as tmpdir:
        wfdb.wrsamp(
            record_name,
            fs=fs,
            units=[units],
            sig_name=[sig_name],
            p_signal=arr,
            write_dir=tmpdir,
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in pathlib.Path(tmpdir).iterdir():
                zf.write(f, arcname=f.name)
        return buf.getvalue()


def _build_edf(samples: list[float], meta: dict) -> bytes:
    """
    EDF+ record — single channel, continuous recording.
    Compatible with EDFbrowser, MNE-Python, EEGLAB.
    """
    try:
        import pyedflib
        import numpy as np
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=api_error("FORMAT_NOT_AVAILABLE",
                             "pyEDFlib not installed. Run: pip install pyEDFlib"),
        )

    # pyedflib requires a real file path — use a named temp file
    import tempfile
    import pathlib
    with tempfile.NamedTemporaryFile(suffix=".edf", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        fs = int(meta.get("sampling_rate_hz", 256))
        sig_name = meta.get("signal_type", "signal")[:16]
        units = meta.get("units", "uV")
        arr = np.array(samples, dtype=np.float64)

        f = pyedflib.EdfWriter(tmp_path, 1, file_type=pyedflib.FILETYPE_EDFPLUS)
        ch = {
            "label": sig_name,
            "dimension": units,
            "sample_frequency": fs,
            "physical_max": float(max(arr)) if arr.size else 1.0,
            "physical_min": float(min(arr)) if arr.size else -1.0,
            "digital_max": 32767,
            "digital_min": -32768,
            "transducer": meta.get("device_id", ""),
            "prefilter": "",
        }
        f.setSignalHeader(0, ch)
        f.writeSamples([arr])
        f.close()

        return pathlib.Path(tmp_path).read_bytes()
    finally:
        pathlib.Path(tmp_path).unlink(missing_ok=True)


# ── Bulk patient export ───────────────────────────────────────────────────────

@router.get(
    "/data/patients/{patient_id}/export",
    summary="Bulk export all signal data for a patient (ZIP)",
)
@limiter.limit("5/minute")
async def export_patient(
    request: Request,
    patient_id: str,
    format: str = "csv",
    user: Dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    """Download all batches for a patient as a single ZIP archive."""
    fmt = format.lower()
    if fmt not in ("csv", "wfdb", "edf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_error("UNSUPPORTED_FORMAT", "format must be one of: csv, wfdb, edf"),
        )

    projects = await get_all_projects()
    all_batches: list[dict] = []
    for proj in projects:
        try:
            async with get_project_session(proj) as session:
                rows = (await session.execute(
                    _text("""
                        SELECT uuid, signal_type, sampling_rate_hz, units,
                               patient_id, device_id, start_timestamp
                          FROM measurement_batch
                         WHERE patient_id=:pid AND is_deleted != true
                           AND ingest_status = 'complete'
                    """),
                    {"pid": patient_id},
                )).fetchall()
                for row in rows:
                    all_batches.append({
                        "uuid": row[0], "signal_type": row[1],
                        "sampling_rate_hz": row[2], "units": row[3],
                        "patient_id": row[4], "device_id": row[5],
                        "start_timestamp": row[6].isoformat() if row[6] else "",
                        "project_id": proj,
                    })
        except Exception as exc:
            logger.warning(f"Bulk export: lookup failed for {proj}: {exc}")

    if not all_batches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("NO_DATA", f"No batches found for patient {patient_id}"),
        )

    import asyncio
    master_buf = io.BytesIO()
    now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    with zipfile.ZipFile(master_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for meta in all_batches:
            uuid = meta["uuid"]
            try:
                samples = await asyncio.to_thread(_fetch_samples, uuid)
            except Exception:
                samples = []
            if not samples:
                continue
            if fmt == "csv":
                content = _build_csv(samples, meta)
                zf.writestr(f"patient_{patient_id}/{uuid[:8]}_{meta['signal_type']}.csv", content)
            elif fmt == "wfdb":
                wfdb_zip = await asyncio.to_thread(_build_wfdb_zip, samples, meta)
                inner = zipfile.ZipFile(io.BytesIO(wfdb_zip))
                for name in inner.namelist():
                    zf.writestr(f"patient_{patient_id}/{uuid[:8]}_{name}", inner.read(name))
            else:
                edf_bytes = await asyncio.to_thread(_build_edf, samples, meta)
                zf.writestr(f"patient_{patient_id}/{uuid[:8]}_{meta['signal_type']}.edf", edf_bytes)

    master_buf.seek(0)
    filename = f"patient_{patient_id}_{fmt}_{now_str}.zip"
    logger.info(
        f"Bulk export | patient={patient_id} | format={fmt} | "
        f"batches={len(all_batches)} | user={user.get('preferred_username')}"
    )
    return StreamingResponse(
        master_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get(
    "/data/signals/{uuid}/export",
    summary="Export signal data in research format (WFDB, EDF+, CSV)",
)
@limiter.limit("20/minute")
async def export_signal(
    request: Request,
    uuid: str,
    format: str = "csv",    # "wfdb" | "edf" | "csv"
    user: Dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    """
    Export a batch's raw signal data in a standard research format.

    - **wfdb** — MIT/PhysioNet WFDB (returns ZIP with .hea + .dat)
    - **edf**  — European Data Format EDF+ (returns .edf file)
    - **csv**  — plain comma-separated values (always available)
    """
    fmt = format.lower()
    if fmt not in ("wfdb", "edf", "csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_error("UNSUPPORTED_FORMAT", "format must be one of: wfdb, edf, csv"),
        )

    # Resolve batch metadata from PostgreSQL
    projects = await get_all_projects()
    meta: Optional[dict] = None
    for proj in projects:
        try:
            async with get_project_session(proj) as session:
                row = (await session.execute(
                    _text("""
                        SELECT uuid, signal_type, sampling_rate_hz, units,
                               patient_id, device_id, start_timestamp
                          FROM measurement_batch
                         WHERE uuid=:uid AND is_deleted != true
                           AND ingest_status = 'complete'
                    """),
                    {"uid": uuid},
                )).fetchone()
                if row:
                    meta = {
                        "uuid": row[0], "signal_type": row[1],
                        "sampling_rate_hz": row[2], "units": row[3],
                        "patient_id": row[4], "device_id": row[5],
                        "start_timestamp": row[6].isoformat() if row[6] else "",
                        "project_id": proj,
                    }
                    break
        except Exception as exc:
            logger.warning(f"Export: metadata lookup failed for {proj}: {exc}")

    if not meta:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("BATCH_NOT_FOUND", f"Batch {uuid!r} not found or was deleted"),
        )

    # Fetch raw samples from InfluxDB
    import asyncio
    try:
        samples = await asyncio.to_thread(_fetch_samples, uuid)
    except Exception as exc:
        logger.error(f"Export: InfluxDB fetch failed for {uuid}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=api_error("INFLUX_FETCH_FAILED", f"Could not retrieve signal data: {exc}"),
        )

    if not samples:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("NO_SIGNAL_DATA", f"No signal samples found for batch {uuid!r}"),
        )

    # Build response by format
    now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    sig = meta["signal_type"]

    if fmt == "csv":
        content = _build_csv(samples, meta)
        filename = f"{sig}_{uuid[:8]}_{now_str}.csv"
        media = "text/csv"
    elif fmt == "wfdb":
        content = await asyncio.to_thread(_build_wfdb_zip, samples, meta)
        filename = f"{sig}_{uuid[:8]}_{now_str}_wfdb.zip"
        media = "application/zip"
    else:  # edf
        content = await asyncio.to_thread(_build_edf, samples, meta)
        filename = f"{sig}_{uuid[:8]}_{now_str}.edf"
        media = "application/octet-stream"

    logger.info(
        f"Export | uuid={uuid} | format={fmt} | samples={len(samples)} | "
        f"user={user.get('preferred_username')}"
    )
    return StreamingResponse(
        io.BytesIO(content),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
