import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text as _text

from medwear_med_api.auth import require_read, KEYCLOAK_CLIENT
from medwear_med_api.db import get_project_session
from medwear_med_api.influx_reader import read_signals_by_filters
from medwear_med_api.rate_limit import limiter
from medwear_med_api.routers.utils import _row_to_batch_dict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Patients"])


@router.get("/patients/{patient_id}", status_code=200, summary="Get all batches for a specific patient")
@limiter.limit("200/minute")
async def get_patient_data(
    request: Request,
    patient_id: str,
    signal_type: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=100_000),
    metadata_only: bool = Query(False),
    downsample: Optional[int] = Query(None),
    show_deleted: bool = Query(False),
    marked_for_deletion: bool = Query(False),
    user: Dict[str, Any] = Depends(require_read),
):
    from medwear_med_api.db import get_all_projects

    client_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    user_can_see_deleted = "global_admin" in client_roles or "project_admin" in client_roles
    is_global_admin = "global_admin" in client_roles
    username = user.get("preferred_username")
    dynamic_projects = await get_all_projects()

    # All non-admin users are restricted to their own projects
    if is_global_admin:
        target_projects = dynamic_projects
    else:
        allowed_projects = []
        for proj in dynamic_projects:
            try:
                async with get_project_session(proj) as session:
                    res = await session.execute(
                        _text("SELECT project_id FROM user_project WHERE username=:u"),
                        {"u": username}
                    )
                    allowed_projects.extend([r[0] for r in res.fetchall()])
            except Exception as e:
                logger.warning(f"Failed to fetch user-project mappings from {proj}: {e}")
                continue
        if not allowed_projects:
            return {"patient_id": patient_id, "batches": [], "count": 0}
        target_projects = allowed_projects

    where_clauses = ["patient_id = :pid", "ingest_status = 'complete'"]
    params: dict = {"pid": patient_id, "limit": limit, "offset": offset}

    if marked_for_deletion and user_can_see_deleted:
        where_clauses.append("is_deleted = true")
    elif show_deleted and user_can_see_deleted:
        pass
    else:
        where_clauses.append("is_deleted != true")

    if signal_type:
        where_clauses.append("signal_type = :signal_type")
        params["signal_type"] = signal_type.upper()
    if start:
        where_clauses.append("start_timestamp >= :start")
        params["start"] = datetime.fromisoformat(start.replace(
            "Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    if end:
        where_clauses.append("end_timestamp <= :end")
        params["end"] = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)

    where_sql = "WHERE " + " AND ".join(where_clauses)
    sql = _text(
        f"SELECT uuid, patient_id, device_id, signal_type, schema_id, "
        f"start_timestamp, end_timestamp, sampling_rate_hz, sample_count, units, "
        f"metadata_json, idempotency_key, created_at, is_deleted, deleted_at "
        f"FROM measurement_batch {where_sql} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    )

    batch_dicts = []
    for proj in target_projects:
        try:
            async with get_project_session(proj) as session:
                result = await session.execute(sql, params)
                for row in result.fetchall():
                    batch_dicts.append(_row_to_batch_dict(row))
        except Exception as e:
            logger.warning(f"Failed to query batches from {proj}: {e}")
            continue

    if metadata_only:
        return {"patient_id": patient_id, "batches": batch_dicts, "count": len(batch_dicts)}

    batch_uuids = [b["uuid"] for b in batch_dicts]
    signals_map = read_signals_by_filters(batch_uuids=batch_uuids) if batch_uuids else {}

    results = []
    for b in batch_dicts:
        samples = signals_map.get(b["uuid"], [])
        if downsample and downsample > 1:
            samples = samples[::downsample]
        results.append({**b, "signals": samples, "warning": None if samples else "No signals in InfluxDB."})

    return {"patient_id": patient_id, "batches": results, "count": len(results)}
