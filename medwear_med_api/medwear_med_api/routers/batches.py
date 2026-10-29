import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from minio.error import S3Error
from sqlalchemy import select, text as _text

from medwear_med_api.auth import require_read, require_admin, KEYCLOAK_CLIENT
from medwear_med_api.db import get_project_session
from medwear_med_api.errors import api_error
from medwear_med_api.rate_limit import limiter
from medwear_med_api.influx_reader import (
    read_signals_by_uuid,
    read_signals_by_filters,
    INLINE_SAMPLE_LIMIT,
)
from medwear_med_api.models import UserProject
from medwear_med_api.routers.utils import (
    find_batch_in_projects, _stream_csv, _row_to_batch_dict,
    get_minio_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Batches"])


async def _check_project_access(user: Dict[str, Any], project_id: str) -> bool:
    """Returns True if user is global_admin or belongs to the given project."""
    caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    if "global_admin" in caller_roles:
        return True
    username = user.get("preferred_username", "")
    try:
        async with get_project_session(project_id) as session:
            result = await session.execute(
                select(UserProject).where(UserProject.username == username)
            )
            return result.scalars().first() is not None
    except Exception as e:
        logger.warning(f"Failed to check project access for {username} in {project_id}: {e}")
        return False


@router.get("/buckets", status_code=200, summary="List available MinIO buckets")
@limiter.limit("60/minute")
async def list_buckets(request: Request, user: Dict[str, Any] = Depends(require_read)):
    try:
        client = get_minio_client()
        buckets = client.list_buckets()
        return {"buckets": [{"name": b.name, "created": b.creation_date.isoformat() if b.creation_date else None}
                            for b in buckets]}
    except S3Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=api_error("STORAGE_ERROR", str(e)),
        )


@router.get("/signal-types", status_code=200, summary="List all signal types in the database")
@limiter.limit("60/minute")
async def list_signal_types(request: Request, user: Dict[str, Any] = Depends(require_read)):
    from medwear_med_api.db import get_all_projects
    dynamic_projects = await get_all_projects()
    types = set()
    for proj in dynamic_projects:
        try:
            async with get_project_session(proj) as session:
                result = await session.execute(
                    _text("SELECT DISTINCT signal_type FROM measurement_batch "
                          "WHERE is_deleted != true AND ingest_status = 'complete'")
                )
                for row in result.fetchall():
                    types.add(row[0])
        except Exception as e:
            logger.warning(f"Failed to fetch signal types from {proj}: {e}")
            continue
    return {"signal_types": list(types), "count": len(types)}


@router.get("", status_code=200, summary="Query batches with filters")
@limiter.limit("200/minute")
async def list_batches(
    request: Request,
    patient_ids: Optional[str] = Query(None),
    signal_type: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    device_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500000),
    offset: int = Query(0, ge=0, le=100_000),
    metadata_only: bool = Query(False),
    summary: bool = Query(False),
    format: str = Query("json"),
    downsample: Optional[int] = Query(None),
    show_deleted: bool = Query(False),
    marked_for_deletion: bool = Query(False),
    user: Dict[str, Any] = Depends(require_read),
):
    from medwear_med_api.db import get_all_projects

    caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    is_global_admin = "global_admin" in caller_roles
    username = user.get("preferred_username")
    dynamic_projects = await get_all_projects()

    if is_global_admin:
        target_projects = [project_id] if project_id else dynamic_projects
    else:
        user_projects = []
        for proj in dynamic_projects:
            try:
                async with get_project_session(proj) as session:
                    result = await session.execute(
                        select(UserProject).where(UserProject.username == username)
                    )
                    rows = result.scalars().all()
                    if rows:
                        user_projects.extend([r.project_id for r in rows])
            except Exception as e:
                logger.warning(f"Failed to fetch user projects for {username} from {proj}: {e}")
                continue
        if not user_projects:
            return {"batches": [], "count": 0, "offset": offset, "limit": limit}
        target_projects = [project_id] if project_id and project_id in user_projects else user_projects

    where_clauses = ["ingest_status = 'complete'"]
    params = {"limit": limit}

    user_can_see_deleted = "global_admin" in caller_roles or "project_admin" in caller_roles
    if marked_for_deletion and user_can_see_deleted:
        where_clauses.append("is_deleted = true")
    elif show_deleted and user_can_see_deleted:
        pass
    else:
        where_clauses.append("is_deleted != true")

    if patient_ids is not None:
        id_list = [p.strip() for p in patient_ids.split(",") if p.strip()]
        if not id_list:
            raise HTTPException(
                status_code=400,
                detail=api_error("INVALID_PATIENT_ID", "patient_ids must be a non-empty comma-separated list"))
        where_clauses.append("patient_id = ANY(:patient_ids)")
        params["patient_ids"] = id_list
    if signal_type is not None:
        where_clauses.append("signal_type = :signal_type")
        params["signal_type"] = signal_type.upper()
    if start is not None:
        where_clauses.append("start_timestamp >= :start")
        params["start"] = datetime.fromisoformat(start.replace(
            "Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    if end is not None:
        where_clauses.append("end_timestamp <= :end")
        params["end"] = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    if device_id is not None:
        where_clauses.append("device_id = :device_id")
        params["device_id"] = device_id

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if summary:
        summary_sql = _text(
            f"SELECT COUNT(*) as total, "
            f"COALESCE(SUM(sample_count), 0) as total_samples, "
            f"COUNT(DISTINCT patient_id) as patient_count "
            f"FROM measurement_batch {where_sql}"
        )
        signal_sql = _text(
            f"SELECT signal_type, COUNT(*) as cnt "
            f"FROM measurement_batch {where_sql} "
            f"GROUP BY signal_type"
        )
        count_params = {k: v for k, v in params.items() if k != "limit"}
        total, total_samples, patient_count = 0, 0, 0
        signal_counts: dict = {}
        for proj in target_projects:
            try:
                async with get_project_session(proj) as session:
                    row = (await session.execute(summary_sql, count_params)).fetchone()
                    if row:
                        total += row[0] or 0
                        total_samples += row[1] or 0
                        patient_count += row[2] or 0
                    for srow in (await session.execute(signal_sql, count_params)).fetchall():
                        signal_counts[srow[0]] = signal_counts.get(srow[0], 0) + srow[1]
            except Exception as e:
                logger.warning(f"Failed to query summary from {proj}: {e}")
                continue
        return {
            "total": total,
            "signal_types": list(signal_counts.keys()),
            "signal_counts": signal_counts,
            "patient_count": patient_count,
            "total_samples": total_samples,
        }

    sql = _text(
        f"SELECT uuid, patient_id, device_id, signal_type, schema_id, "
        f"start_timestamp, end_timestamp, sampling_rate_hz, sample_count, units, "
        f"metadata_json, idempotency_key, created_at, is_deleted, deleted_at "
        f"FROM measurement_batch {where_sql} ORDER BY created_at DESC LIMIT :limit"
    )

    raw_batches = []
    for proj in target_projects:
        try:
            async with get_project_session(proj) as session:
                result = await session.execute(sql, params)
                for row in result.fetchall():
                    b = _row_to_batch_dict(row)
                    b["project_id"] = proj
                    raw_batches.append(b)
        except Exception as e:
            logger.warning(f"Failed to query {proj}: {e}")
            continue

    raw_batches.sort(key=lambda x: x["created_at"] or "", reverse=True)
    raw_batches = raw_batches[offset:offset + limit]

    if metadata_only:
        if format == "csv":
            buf = io.StringIO()
            if raw_batches:
                writer = csv.DictWriter(buf, fieldnames=raw_batches[0].keys())
                writer.writeheader()
                writer.writerows(raw_batches)
            return StreamingResponse(
                iter([buf.getvalue()]),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=batches.csv"},
            )
        return {"batches": raw_batches, "count": len(raw_batches), "offset": offset, "limit": limit}

    batch_uuids = [b["uuid"] for b in raw_batches]
    signals_map = read_signals_by_filters(
        patient_ids=[str(p) for p in {b["patient_id"] for b in raw_batches}] if patient_ids else None,
        signal_type=signal_type,
        batch_uuids=batch_uuids,
    ) if batch_uuids else {}

    results = []
    for b in raw_batches:
        samples = signals_map.get(b["uuid"], [])
        if downsample and downsample > 1:
            samples = samples[::downsample]
        entry = {**b, "signals": samples if len(samples) <= INLINE_SAMPLE_LIMIT else [], "warning": None}
        if len(signals_map.get(b["uuid"], [])) > INLINE_SAMPLE_LIMIT:
            entry["warning"] = (
                f"Signal data exceeds {INLINE_SAMPLE_LIMIT} samples. "
                f"Use GET /v1/data/{b['uuid']}/signals to download."
            )
        results.append(entry)

    if format == "csv":
        buf = io.StringIO()
        if results:
            writer = csv.DictWriter(buf, fieldnames=results[0].keys())
            writer.writeheader()
            for r in results:
                row = {**r, "signals": ";".join(str(s) for s in r["signals"])}
                writer.writerow(row)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=batches.csv"},
        )

    return {"batches": results, "count": len(results), "offset": offset, "limit": limit}


@router.get("/{uuid}/metadata", status_code=200, summary="Retrieve only metadata from PostgreSQL")
@limiter.limit("200/minute")
async def get_batch_metadata(
    request: Request,
    uuid: str,
    user: Dict[str, Any] = Depends(require_read),
):
    batch, proj = await find_batch_in_projects(uuid)
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error(
                "NOT_FOUND",
                "Batch not found",
                uuid=uuid))
    if not await _check_project_access(user, proj):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api_error(
                "FORBIDDEN",
                "You do not have access to this project"))
    return batch


@router.get("/{uuid}/signals", status_code=200, summary="Retrieve only raw signals from InfluxDB")
@limiter.limit("100/minute")
async def get_batch_signals(
    request: Request,
    uuid: str,
    format: str = Query("json"),
    downsample: Optional[int] = Query(None, ge=2, le=1000),
    user: Dict[str, Any] = Depends(require_read),
):
    batch, proj = await find_batch_in_projects(uuid)
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error(
                "NOT_FOUND",
                "Batch not found",
                uuid=uuid))
    if not await _check_project_access(user, proj):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api_error(
                "FORBIDDEN",
                "You do not have access to this project"))

    samples = read_signals_by_uuid(uuid)
    if not samples:
        logger.warning(f"Missing InfluxDB signals for batch_uuid={uuid}")
        return {"uuid": uuid, "signals": [], "sample_count": 0, "warning": "No signal data found in InfluxDB."}

    if downsample and downsample > 1:
        samples = samples[::downsample]

    if len(samples) > INLINE_SAMPLE_LIMIT or format == "csv":
        return _stream_csv(batch, samples)

    return {"uuid": uuid, "signals": samples, "sample_count": len(samples)}


@router.get("/{uuid}", status_code=200, summary="Retrieve a single batch (metadata + signals)")
@limiter.limit("100/minute")
async def get_batch(
    request: Request,
    uuid: str,
    format: str = Query("json"),
    user: Dict[str, Any] = Depends(require_read),
):
    batch, proj = await find_batch_in_projects(uuid)
    if not batch:
        caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
        can_see_deleted = "global_admin" in caller_roles or "project_admin" in caller_roles
        if can_see_deleted:
            batch, proj = await find_batch_in_projects(uuid, include_deleted=True)
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error(
                "NOT_FOUND",
                "Batch not found",
                uuid=uuid))
    if not await _check_project_access(user, proj):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api_error(
                "FORBIDDEN",
                "You do not have access to this project"))

    samples = read_signals_by_uuid(uuid)
    warning = None
    if not samples:
        warning = "Signal data not found in InfluxDB."
        logger.warning(f"Missing InfluxDB data for batch_uuid={uuid}")

    if len(samples) > INLINE_SAMPLE_LIMIT or format == "csv":
        return _stream_csv(batch, samples)

    return {"metadata": batch, "signals": samples, "warning": warning}


@router.post("/{uuid}/restore", status_code=200, summary="Restore a soft-deleted batch")
@limiter.limit("30/minute")
async def restore_batch(
    request: Request,
    uuid: str,
    user: Dict[str, Any] = Depends(require_admin),
):
    batch, proj = await find_batch_in_projects(uuid, include_deleted=True)
    if not batch or not batch["is_deleted"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error(
                "NOT_FOUND",
                "Deleted batch not found",
                uuid=uuid))
    if not await _check_project_access(user, proj):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api_error(
                "FORBIDDEN",
                "You do not have access to this project"))
    async with get_project_session(proj) as session:
        await session.execute(
            _text("UPDATE measurement_batch SET is_deleted = false, deleted_at = NULL WHERE uuid = :uuid"),
            {"uuid": uuid}
        )
        await session.commit()
    return {"uuid": uuid, "status": "restored"}


@router.delete("/{uuid}", status_code=200, summary="Soft delete a batch (purged after 24h)")
@limiter.limit("30/minute")
async def delete_batch(
    request: Request,
    uuid: str,
    user: Dict[str, Any] = Depends(require_admin),
):
    batch, proj = await find_batch_in_projects(uuid)
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error(
                "NOT_FOUND",
                "Batch not found",
                uuid=uuid))
    if not await _check_project_access(user, proj):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api_error(
                "FORBIDDEN",
                "You do not have access to this project"))

    async with get_project_session(proj) as session:
        await session.execute(
            _text("UPDATE measurement_batch SET is_deleted = true, deleted_at = NOW() WHERE uuid = :uuid"),
            {"uuid": uuid}
        )
        await session.commit()

    return {
        "uuid": uuid,
        "status": "soft_deleted",
        "message": "Batch marked as deleted. Will be permanently removed after 24 hours.",
    }
