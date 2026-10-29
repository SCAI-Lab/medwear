import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import time
import uuid as uuid_mod
from typing import Any, Dict, List

import asyncpg
from minio import Minio
from minio.commonconfig import Tags
from minio.error import S3Error

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError

from medwear_med_api.auth import require_read_write, require_device, KEYCLOAK_CLIENT
from medwear_med_api.db import ensure_ingest_schema, get_all_projects, get_project_session
from medwear_med_api.errors import api_error
from medwear_med_api.influx_writer import write_signal_batch
from medwear_med_api.models import MeasurementBatch
from medwear_med_api.rate_limit import limiter
from medwear_med_api.routers.utils import MINIO_BUCKET, MINIO_ENDPOINT, MINIO_PASSWORD, MINIO_USER

try:
    from medwear_med_api.metrics import BATCH_INGESTED as _BATCH_INGESTED
    _HAS_METRICS = True
except Exception:
    _HAS_METRICS = False
from medwear_med_api.schemas import (
    BatchUploadRequest,
    BatchUploadListRequest,
    BatchUploadListResponse,
    BatchResult,
    SessionCreateRequest,
    SessionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Data Ingestion"])

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024


_schema_ensured: set = set()


async def _resolve_project(project_id, username: str) -> str:
    """The requested project if it exists, otherwise the user's project;
    makes sure that project has the two-phase ingest schema."""
    dynamic_projects = await get_all_projects()
    if not project_id or project_id not in dynamic_projects:
        project_id = None
        for proj in dynamic_projects:
            try:
                conn = await asyncpg.connect(
                    host=os.getenv("POSTGRES_HOST", ""), port=int(os.getenv("POSTGRES_PORT", "5432")),
                    user=os.getenv("POSTGRES_USER", ""), password=os.getenv("POSTGRES_PASSWORD", ""), database=proj
                )
                row = await conn.fetchrow(
                    "SELECT project_id FROM user_project WHERE username=$1 LIMIT 1", username
                )
                await conn.close()
                if row:
                    project_id = row["project_id"]
                    break
            except Exception as e:
                logger.warning(f"Failed to resolve project for user {username} from {proj}: {e}")
                continue
    if not project_id or project_id not in dynamic_projects:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=api_error(
            "FORBIDDEN", "Could not determine project for this user. Ensure the user is assigned to a project."), )
    if project_id not in _schema_ensured:
        await ensure_ingest_schema(project_id)
        _schema_ensured.add(project_id)
    return project_id


# ── Sessions: stream metadata sent once ─────────────────────────────────────────

_SESSION_IDENTITY = ("patient_id", "device_id", "signal_type", "schema_id",
                     "sampling_rate_hz", "units", "project_id")
_SESSION_CACHE_MAX = 10_000
_session_cache: Dict[str, dict] = {}


async def _load_session(session_id: str, project_id) -> dict:
    cached = _session_cache.get(session_id)
    if cached:
        return cached
    projects = await get_all_projects()
    candidates = [project_id] if project_id in projects else projects
    for proj in candidates:
        try:
            async with get_project_session(proj) as session:
                row = (await session.execute(
                    _text("SELECT session_id, patient_id, device_id, signal_type, schema_id, "
                          "sampling_rate_hz, units, header_json, project_id, created_by "
                          "FROM ingest_session WHERE session_id = :sid"),
                    {"sid": session_id},
                )).mappings().fetchone()
        except Exception as e:   # project without the table yet
            logger.debug(f"Session lookup in {proj} failed: {e}")
            continue
        if row:
            if len(_session_cache) >= _SESSION_CACHE_MAX:
                _session_cache.clear()
            _session_cache[session_id] = dict(row)
            return _session_cache[session_id]
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=api_error("UNKNOWN_SESSION", f"Session {session_id} is not registered; "
                         "POST /v1/data/session first", session_id=session_id),
    )


async def _expand_session_batch(item: Any, username: str) -> Any:
    """A batch that references a session carries only its samples and time
    frame; fill in the session's metadata to get a full BatchUploadRequest."""
    if not isinstance(item, dict) or not item.get("session_id"):
        return item
    s = await _load_session(str(item["session_id"]), item.get("project_id"))
    if s["created_by"] != username:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=api_error(
            "FORBIDDEN", "Session belongs to another uploader", session_id=s["session_id"]))
    body = dict(item.get("body") or {})
    body.setdefault("unit", s["units"])
    interval = (body.get("effective_time_frame") or {}).get("time_interval") or {}
    idem = item.get("idempotency_key")
    if not idem and not item.get("uuid"):
        # deterministic, so a retry of the same batch maps to the same key
        idem = f"{s['session_id']}::{interval.get('start_date_time')}::{interval.get('end_date_time')}"
    return {
        "header": {**(s["header_json"] or {}),
                   "uuid": item.get("uuid") or str(uuid_mod.uuid4()),
                   "source_creation_date_time": (item.get("source_creation_date_time")
                                                 or interval.get("start_date_time", ""))},
        "body": body,
        "signal_type": s["signal_type"],
        "patient_id": s["patient_id"],
        "device_id": s["device_id"],
        "project_id": s["project_id"],
        "idempotency_key": idem,
        "checksum": item.get("checksum"),
        "session_id": s["session_id"],
    }


def _make_idempotency_key(request: BatchUploadRequest) -> str:
    user_key = request.idempotency_key
    if user_key:
        return hashlib.sha256(user_key.encode()).hexdigest()
    raw = f"{request.device_id}::{request.header.uuid}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_checksum(samples: List[float]) -> str:
    raw = json.dumps(samples, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _semantic_validate(request: BatchUploadRequest):
    try:
        start_dt = request.get_start_datetime()
        end_dt = request.get_end_datetime()
        if end_dt <= start_dt:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=api_error(
                    "INVALID_TIMESTAMP_ORDER",
                    f"end_date_time ({end_dt}) must be after start_date_time ({start_dt})",
                ),
            )
    except HTTPException:
        raise
    except (ValueError, KeyError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=api_error("INVALID_TIMESTAMP_FORMAT", str(e)),
        )

    sampling_rate = request.get_sampling_rate()
    if sampling_rate <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=api_error(
                "MISSING_SAMPLING_RATE",
                "sampling_rate_hz must be present and > 0 in acquisition_rate or derivable from timestamps",
            ),
        )


SIGNAL_FIELD_MAP = {
    "ECG": "ecg_lead_data",
    "BCG": "bcg_sensor_readings",
    "PPG": "signal_data",
    "IMU": "signal_data",
    "EEG": "signal_data",
    "EDA": "signal_data",
    "HR": "signal_data",
    "HRV": "signal_data",
}


def _csv_body(
        signal_type: str,
        samples: list,
        unit: str,
        start_dt: str,
        end_dt: str,
        signal_quality: list = None) -> dict:
    time_frame = {"effective_time_frame": {"time_interval": {
        "start_date_time": start_dt,
        "end_date_time": end_dt,
    }}}
    base = {"unit": unit, **time_frame}
    field_name = SIGNAL_FIELD_MAP.get(signal_type, "signal_data")
    body = {**base, field_name: samples}
    if signal_type == "ECG":
        if not signal_quality:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=api_error(
                    "MISSING_SIGNAL_QUALITY",
                    "ECG CSV rows must include a signal_quality column with semicolon-separated values (0.0-1.0)",
                ),
            )
        body["signal_quality"] = signal_quality
    return body


def _parse_csv(content: bytes) -> BatchUploadListRequest:
    try:
        text = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        batches = []
        for row in reader:
            samples = [float(x) for x in row["samples"].split(";") if x.strip()]
            signal_quality = None
            if row.get("signal_quality"):
                signal_quality = [float(x) for x in row["signal_quality"].split(";") if x.strip()]
            batch = {
                "header": {
                    "uuid": row["uuid"],
                    "schema_id": row["schema_id"],
                    "source_creation_date_time": row["source_creation_date_time"],
                    "acquisition_rate": {
                        "value": float(row["sampling_rate_hz"]),
                        "unit": "Hz",
                        "number_of_times": 1,
                    },
                },
                "body": _csv_body(
                    row["signal_type"].upper(), samples, row["unit"],
                    row["start_date_time"], row["end_date_time"], signal_quality
                ),
                "signal_type": row["signal_type"].upper(),
                "patient_id": str(row["patient_id"]),
                "device_id": row["device_id"],
                "idempotency_key": row.get("idempotency_key") or None,
            }
            batches.append(batch)
        return BatchUploadListRequest(batches=batches)
    except (csv.Error, ValueError, KeyError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_error("CSV_PARSE_ERROR", str(e)),
        )


async def _ingest_one(batch_req: BatchUploadRequest, username: str) -> BatchResult:
    try:
        batch_req.get_validated_body()
    except ValidationError as e:
        errors = e.errors()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_error(
                "SCHEMA_VALIDATION_FAILURE",
                f"{batch_req.signal_type.value} body validation failed: " +
                "; ".join(
                    f"{' -> '.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in errors),
                issues=[{"field": " -> ".join(str(loc) for loc in err["loc"]),
                         "message": err["msg"]} for err in errors],
            ),
        )

    _semantic_validate(batch_req)

    samples = batch_req.get_samples()
    units = batch_req.get_units()
    start_dt = batch_req.get_start_datetime()
    end_dt = batch_req.get_end_datetime()
    sampling_rate = batch_req.get_sampling_rate()
    idempotency_key = _make_idempotency_key(batch_req)
    computed_checksum = _make_checksum(samples)

    if batch_req.checksum and batch_req.checksum != computed_checksum:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_error(
                "CHECKSUM_MISMATCH",
                "The provided checksum does not match the computed checksum of the signal data",
                provided_checksum=batch_req.checksum,
                computed_checksum=computed_checksum,
            ),
        )
    checksum = computed_checksum

    logger.info(
        f"Ingesting {batch_req.signal_type.value} | "
        f"patient={batch_req.patient_id} | device={batch_req.device_id} | "
        f"samples={len(samples)} | rate={sampling_rate} Hz"
    )

    batch = MeasurementBatch(
        patient_id=batch_req.patient_id,
        device_id=batch_req.device_id,
        signal_type=batch_req.signal_type.value,
        schema_id=batch_req.header.schema_id,
        start_timestamp=start_dt,
        end_timestamp=end_dt,
        sampling_rate_hz=sampling_rate,
        sample_count=len(samples),
        units=units,
        metadata_json={
            "modality": batch_req.header.modality,
            "device_identity": (
                batch_req.header.device_identity.model_dump()
                if batch_req.header.device_identity else None
            ),
            "uploaded_by": username,
            "signal_info": batch_req.get_signal_info(),
        },
        idempotency_key=idempotency_key,
        checksum=checksum,
        project_id=batch_req.project_id if hasattr(batch_req, 'project_id') and batch_req.project_id else None,
        session_id=batch_req.session_id,
    )

    project_id = await _resolve_project(batch.project_id, username)
    batch.project_id = project_id

    # I-02: Overlapping batch detection
    overlap_warning = None
    async with get_project_session(project_id) as session:
        overlap_row = await session.execute(
            _text("""
                SELECT uuid FROM measurement_batch
                WHERE device_id = :device_id
                  AND signal_type = :signal_type
                  AND is_deleted != true
                  AND ingest_status = 'complete'
                  AND start_timestamp < :end_ts
                  AND end_timestamp > :start_ts
                LIMIT 1
            """),
            {
                "device_id": batch.device_id,
                "signal_type": batch.signal_type,
                "start_ts": start_dt,
                "end_ts": end_dt,
            },
        )
        existing = overlap_row.fetchone()

    if existing:
        overlap_warning = (
            f"Overlapping batch detected: an existing batch (uuid={existing[0]}) covers "
            f"part of the time range {start_dt.isoformat()} → {end_dt.isoformat()}. "
            "Data accepted — verify for duplicates."
        )
        logger.warning(f"Overlap detected | device={batch.device_id} | existing={existing[0]}")

    # Phase 1: metadata row as "pending". It becomes "complete" (and the batch
    # acknowledged) only after the InfluxDB write, so a crash, timeout or
    # InfluxDB failure in between leaves a pending row that a resubmission
    # resumes instead of reporting as a duplicate.
    batch.ingest_status = "pending"
    resumed = False
    async with get_project_session(project_id) as session:
        try:
            session.add(batch)
            await session.commit()
            await session.refresh(batch)
        except IntegrityError:
            await session.rollback()
            existing = None
            async with get_project_session(project_id) as s2:
                row = await s2.execute(
                    _text("SELECT uuid, ingest_status, checksum FROM measurement_batch "
                          "WHERE idempotency_key = :ikey LIMIT 1"),
                    {"ikey": idempotency_key},
                )
                existing = row.fetchone()
            if existing is None or existing[1] == "complete":
                logger.warning(f"Duplicate | idempotency_key={idempotency_key}")
                return BatchResult(
                    batch_uuid=str(existing[0]) if existing else idempotency_key,
                    status="duplicate",
                    sample_count=0,
                    message="Duplicate batch — already ingested",
                )
            if existing[2] != checksum:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=api_error(
                        "IDEMPOTENCY_CONFLICT",
                        "A pending batch with this idempotency key holds different samples",
                        batch_uuid=str(existing[0]),
                    ),
                )
            # Same samples, InfluxDB write not confirmed: rewrite them under the
            # existing batch_uuid. InfluxDB overwrites points with identical
            # series and timestamp, so a partial earlier write is not doubled.
            batch.uuid = str(existing[0])
            resumed = True
            logger.warning(f"Resuming pending batch | batch_uuid={batch.uuid}")

    batch_uuid = batch.uuid
    if not resumed:
        logger.info(f"PostgreSQL OK | batch_uuid={batch_uuid}")

    # Re-verify data integrity: samples in memory must still match the committed checksum
    _recomputed = _make_checksum(samples)
    if _recomputed != checksum:
        logger.error(f"Integrity check FAILED before InfluxDB write | batch_uuid={batch_uuid}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=api_error(
                "DATA_INTEGRITY_ERROR",
                "Sample data checksum mismatch detected before write — data may have been corrupted in memory",
                batch_uuid=batch_uuid,
            ),
        )

    try:
        await asyncio.to_thread(
            write_signal_batch,
            batch_uuid=batch_uuid,
            patient_id=batch_req.patient_id,
            device_id=batch_req.device_id,
            signal_type=batch_req.signal_type.value,
            samples=samples,
            start_timestamp=start_dt,
            sampling_rate_hz=sampling_rate,
            signal_info=batch_req.get_signal_info(),
            project_id=batch_req.project_id,
        )
        logger.info(f"InfluxDB OK | batch_uuid={batch_uuid}")
    except Exception as e:
        logger.error(f"InfluxDB FAILED | batch_uuid={batch_uuid} | {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=api_error(
                "PARTIAL_WRITE_FAILURE",
                "InfluxDB write failed; the batch is pending and not stored. "
                "Resubmit the same batch to resume it.",
                batch_uuid=batch_uuid,
            ),
        )

    # Phase 2: both stores written
    async with get_project_session(project_id) as session:
        await session.execute(
            _text("UPDATE measurement_batch SET ingest_status = 'complete' WHERE uuid = :uuid"),
            {"uuid": batch_uuid},
        )
        await session.commit()
    logger.info(f"COMMIT OK | batch_uuid={batch_uuid}{' | resumed' if resumed else ''}")

    if _HAS_METRICS:
        _BATCH_INGESTED.labels(
            signal_type=batch_req.signal_type.value,
            project_id=project_id or "unknown",
        ).inc()

    return BatchResult(
        batch_uuid=batch_uuid,
        status="ingested",
        sample_count=len(samples),
        message=(f"{batch_req.signal_type.value} batch ingested @ {sampling_rate} Hz"
                 + (" (resumed a pending write)" if resumed else "")),
        overlap_warning=overlap_warning,
    )


@router.patch(
    "/batch/{batch_uuid}/device_id",
    status_code=200,
    summary="Update device_id for an existing batch",
)
async def update_batch_device_id(
    batch_uuid: str,
    project_id: str,
    device_id: str,
    user: Dict[str, Any] = Depends(require_read_write),
):
    from sqlalchemy import text as _text
    new_ikey = hashlib.sha256(f"{device_id}::{batch_uuid}".encode()).hexdigest()
    async with get_project_session(project_id) as session:
        result = await session.execute(
            _text("""
                UPDATE measurement_batch
                SET device_id = :device_id, idempotency_key = :ikey
                WHERE uuid = :uuid AND is_deleted != true
                RETURNING uuid
            """),
            {"device_id": device_id, "ikey": new_ikey, "uuid": batch_uuid},
        )
        row = result.fetchone()
        await session.commit()
    if not row:
        raise HTTPException(status_code=404, detail=api_error("NOT_FOUND", f"Batch {batch_uuid} not found"))
    return {"batch_uuid": batch_uuid, "device_id": device_id, "status": "updated"}


@router.post(
    "/batch",
    response_model=BatchUploadListResponse,
    status_code=200,
    summary="Upload one or more wearable sensor data batches (JSON or CSV)",
)
@limiter.limit(lambda: os.getenv("BATCH_RATE_LIMIT", "300/minute"))
async def upload_batch(
    request: Request,
    response: Response,
    user: Dict[str, Any] = Depends(require_device),
) -> BatchUploadListResponse:
    t_received = time.time_ns()
    response.headers["X-Received-At"] = f"{t_received / 1e9:.6f}"

    content_type = request.headers.get("content-type", "")
    body = await request.body()

    if len(body) > MAX_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=api_error(
                "PAYLOAD_TOO_LARGE",
                f"Maximum allowed payload is {MAX_PAYLOAD_BYTES // 1024 // 1024} MB",
                max_bytes=MAX_PAYLOAD_BYTES,
                received_bytes=len(body),
            ),
        )

    if "text/csv" in content_type:
        list_request = _parse_csv(body)
    else:
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=api_error("INVALID_JSON", str(e)),
            )
        username = user.get("preferred_username", "unknown")
        if isinstance(data, dict) and isinstance(data.get("batches"), list):
            data["batches"] = [await _expand_session_batch(b, username) for b in data["batches"]]
        else:
            data = await _expand_session_batch(data, username)
        try:
            if "batches" in data:
                list_request = BatchUploadListRequest(**data)
            else:
                single = BatchUploadRequest(**data)
                list_request = BatchUploadListRequest(batches=[single])
        except ValidationError as e:
            errors = e.errors()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=api_error(
                    "SCHEMA_VALIDATION_FAILURE",
                    "Request body validation failed: " +
                    "; ".join(
                        f"{' -> '.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in errors),
                    issues=[{"field": " -> ".join(str(loc) for loc in err["loc"]),
                             "message": err["msg"]} for err in errors],
                ),
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=api_error("INVALID_JSON", str(e)),
            )

    t_validated = time.time_ns()
    response.headers["X-Validated-At"] = f"{t_validated / 1e9:.6f}"

    username = user.get("preferred_username", "unknown")
    results = []
    for batch_req in list_request.batches:
        result = await _ingest_one(batch_req, username)
        results.append(result)

    t_stored = time.time_ns()
    response.headers["X-Stored-At"] = f"{t_stored / 1e9:.6f}"

    return BatchUploadListResponse(results=results)


@router.post(
    "/session",
    response_model=SessionResponse,
    status_code=200,
    summary="Register stream metadata once; batches then reference session_id",
)
@limiter.limit(lambda: os.getenv("SESSION_RATE_LIMIT", "600/minute"))
async def create_session(
    request: Request,
    req: SessionCreateRequest,
    user: Dict[str, Any] = Depends(require_device),
) -> SessionResponse:
    username = user.get("preferred_username", "unknown")
    project_id = await _resolve_project(req.project_id, username)
    header = req.header.model_dump(exclude_none=True, mode="json")
    values = {
        "session_id": req.session_id or str(uuid_mod.uuid4()),
        "patient_id": req.patient_id,
        "device_id": req.device_id,
        "signal_type": req.signal_type.value,
        "schema_id": req.header.schema_id,
        "sampling_rate_hz": req.header.acquisition_rate.value,
        "units": req.unit,
        "header_json": json.dumps(header),
        "project_id": project_id,
        "created_by": username,
    }
    async with get_project_session(project_id) as session:
        inserted = (await session.execute(
            _text("""
                INSERT INTO ingest_session (session_id, patient_id, device_id, signal_type, schema_id,
                                            sampling_rate_hz, units, header_json, project_id, created_by)
                VALUES (:session_id, :patient_id, :device_id, :signal_type, :schema_id,
                        :sampling_rate_hz, :units, CAST(:header_json AS JSONB), :project_id, :created_by)
                ON CONFLICT (session_id) DO NOTHING
                RETURNING session_id
            """),
            values,
        )).fetchone()
        await session.commit()
        if not inserted:   # re-registration (e.g. a retried request): must match
            row = (await session.execute(
                _text("SELECT patient_id, device_id, signal_type, schema_id, sampling_rate_hz, units, "
                      "project_id, created_by FROM ingest_session WHERE session_id = :sid"),
                {"sid": values["session_id"]},
            )).mappings().fetchone()
            if row is None or any(row[k] != values[k] for k in _SESSION_IDENTITY) \
                    or row["created_by"] != username:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=api_error(
                    "SESSION_CONFLICT", "session_id is already registered with different metadata",
                    session_id=values["session_id"]))
    logger.info(f"Session {'created' if inserted else 'confirmed'} | session_id={values['session_id']} | "
                f"{values['signal_type']} | device={req.device_id} | project={project_id}")
    return SessionResponse(session_id=values["session_id"], project_id=project_id,
                           status="created" if inserted else "existing")


MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024 * 1024


@router.post(
    "/upload",
    status_code=200,
    summary="Upload a raw sensor file (.cwa or .json, max 1 GB)",
)
@limiter.limit("120/minute")
async def upload_file(
    request: Request,
    user: Dict[str, Any] = Depends(require_read_write),
):
    content_type = request.headers.get("content-type", "")
    filename = request.headers.get("x-filename", "unknown")
    content = await request.body()
    file_size = len(content)

    if file_size > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=api_error(
                "FILE_TOO_LARGE",
                f"Maximum allowed size is 1 GB. Received: {file_size / 1e9:.3f} GB",
                max_bytes=MAX_FILE_SIZE_BYTES,
                received_bytes=file_size,
            ),
        )

    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    allowed = {".cwa", ".json", ".csv", ".wiff"}
    if ext not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_error(
                "UNSUPPORTED_FILE_TYPE",
                f"Only .cwa, .json, .csv, and .wiff files are accepted. Got: '{ext or 'no extension'}'",
                allowed=sorted(allowed),
            ),
        )

    if ext == ".json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=api_error("INVALID_JSON", str(e)),
            )
        if "header" not in parsed or "body" not in parsed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=api_error(
                    "INVALID_ORD_SCHEMA",
                    "JSON file must contain 'header' and 'body' fields. "
                    "Use POST /v1/data/batch for structured uploads.",
                ),
            )

    logger.info(
        f"Raw file received | filename={filename} | size={file_size} bytes | "
        f"user={user.get('preferred_username')}"
    )

    try:
        minio_client = Minio(MINIO_ENDPOINT, access_key=MINIO_USER, secret_key=MINIO_PASSWORD, secure=False)
        username = user.get("preferred_username", "unknown")
        caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
        is_global_admin = "global_admin" in caller_roles

        dynamic_projects = await get_all_projects()

        project_id = request.query_params.get("project_id")
        if project_id and project_id not in dynamic_projects:
            project_id = None

        if not project_id:
            user_projects = []
            for proj in dynamic_projects:
                try:
                    conn = await asyncpg.connect(
                        host=os.environ["POSTGRES_HOST"], port=int(os.environ["POSTGRES_PORT"]),
                        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"], database=proj
                    )
                    row = await conn.fetchrow(
                        "SELECT project_id FROM user_project WHERE username=$1", username
                    )
                    await conn.close()
                    if row:
                        user_projects.append(row["project_id"])
                except Exception as e:
                    logger.warning(f"Failed to resolve project for user {username} from {proj}: {e}")
                    continue

            if len(user_projects) == 1:
                project_id = user_projects[0]
            elif len(user_projects) > 1:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "PROJECT_ID_REQUIRED",
                            "detail": f"User belongs to multiple projects {user_projects}. Specify ?project_id="}
                )

        if not project_id and is_global_admin and dynamic_projects:
            project_id = dynamic_projects[0]

        if not project_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=api_error(
                "FORBIDDEN", "Could not determine project for this user. Ensure the user is assigned to a project."), )

        object_name = f"{project_id}/{username}/{filename}"

        try:
            minio_client.stat_object(MINIO_BUCKET, object_name)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=api_error(
                    "DUPLICATE_FILE",
                    f"A file named '{filename}' already exists for user '{username}'. "
                    "Delete it first or use a different filename.",
                    minio_path=f"{MINIO_BUCKET}/{object_name}",
                ),
            )
        except S3Error as e:
            if e.code != "NoSuchKey":
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=api_error("STORAGE_ERROR", str(e)),
                )

        tags = Tags.new_object_tags()
        tags["uploaded_by"] = username
        tags["filename"] = filename

        minio_client.put_object(
            MINIO_BUCKET, object_name, io.BytesIO(content), length=file_size,
            content_type=content_type or "application/octet-stream", tags=tags,
        )
        logger.info(f"MinIO OK | bucket={MINIO_BUCKET} | object={object_name}")

        return {
            "status": "accepted",
            "filename": filename,
            "size_bytes": file_size,
            "minio_path": f"{MINIO_BUCKET}/{object_name}",
            "message": f"File uploaded to MinIO: {MINIO_BUCKET}/{object_name}",
        }
    except HTTPException:
        raise
    except S3Error as e:
        logger.error(f"MinIO FAILED | {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=api_error("STORAGE_ERROR", str(e)),
        )
