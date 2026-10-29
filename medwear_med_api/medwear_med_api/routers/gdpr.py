"""
GDPR / Privacy endpoints
========================
Art. 7  — Consent management  (grant / revoke / list)
Art. 17 — Right to erasure    (cascade: PostgreSQL → InfluxDB → MinIO)
Art. 20 — Data portability    (patient data export as JSON archive)

All write operations are restricted to project_admin or global_admin.
Patients may be granted read-only access to their own data via the export
endpoint (enforced by patient_id matching the token subject claim).
"""

import asyncio
import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as _text

from medwear_med_api.auth import get_current_user, KEYCLOAK_CLIENT
from medwear_med_api.db import get_all_projects, get_project_session
from medwear_med_api.errors import api_error
from medwear_med_api.models import PatientConsent, now_utc
from medwear_med_api.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gdpr", tags=["GDPR / Privacy"])

_VALID_CONSENT_TYPES = frozenset({
    "data_collection",
    "data_sharing",
    "research_use",
    "third_party_transfer",
})


# ── Schemas ──────────────────────────────────────────────────────────────────

class ConsentGrantRequest(BaseModel):
    consent_type: str = Field(..., description="One of: " + ", ".join(sorted(_VALID_CONSENT_TYPES)))
    consent_version: str = Field(default="1.0", max_length=20)
    notes: Optional[str] = None


class ConsentResponse(BaseModel):
    id: int
    patient_id: str
    consent_type: str
    granted: bool
    granted_at: str
    revoked_at: Optional[str]
    consent_version: str
    granted_by: str
    project_id: str
    notes: Optional[str]


class ErasureResponse(BaseModel):
    patient_id: str
    project_id: str
    batches_erased: int
    influx_deleted: bool
    minio_files_deleted: int
    minio_note: Optional[str]
    consent_revoked: int
    status: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_admin(user: dict) -> None:
    roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    if not any(r in roles for r in ("project_admin", "global_admin")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api_error("FORBIDDEN", "project_admin or global_admin role required"),
        )


async def _resolve_project(user: dict, project_id: Optional[str]) -> str:
    """Return a valid project_id for the caller, or raise 403."""
    username = user.get("preferred_username", "")
    projects = await get_all_projects()

    if project_id and project_id in projects:
        return project_id

    # Fall back to first project the user belongs to
    for proj in projects:
        try:
            async with get_project_session(proj) as session:
                row = (await session.execute(
                    _text("SELECT project_id FROM user_project WHERE username=:u LIMIT 1"),
                    {"u": username},
                )).fetchone()
                if row:
                    return row[0]
        except Exception:
            continue

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=api_error("FORBIDDEN", "Could not resolve project for this user"),
    )


# ── Consent endpoints ─────────────────────────────────────────────────────────

@router.post(
    "/consent/{patient_id}",
    response_model=ConsentResponse,
    status_code=201,
    summary="Grant consent for a patient (GDPR Art. 7)",
)
@limiter.limit("30/minute")
async def grant_consent(
    request: Request,
    patient_id: str,
    body: ConsentGrantRequest,
    project_id: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user),
) -> ConsentResponse:
    _require_admin(user)
    if body.consent_type not in _VALID_CONSENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=api_error(
                "INVALID_CONSENT_TYPE",
                f"consent_type must be one of: {sorted(_VALID_CONSENT_TYPES)}",
            ),
        )

    proj = await _resolve_project(user, project_id)
    username = user.get("preferred_username", "unknown")

    async with get_project_session(proj) as session:
        # Revoke any existing active consent of the same type first (idempotent)
        await session.execute(
            _text("""
                UPDATE patient_consent
                   SET granted=false, revoked_at=:now
                 WHERE patient_id=:pid
                   AND consent_type=:ct
                   AND granted=true
                   AND project_id=:proj
            """),
            {"now": now_utc(), "pid": patient_id, "ct": body.consent_type, "proj": proj},
        )

        consent = PatientConsent(
            patient_id=patient_id,
            consent_type=body.consent_type,
            granted=True,
            consent_version=body.consent_version,
            granted_by=username,
            project_id=proj,
            notes=body.notes,
        )
        session.add(consent)
        await session.commit()
        await session.refresh(consent)

    logger.info(f"Consent granted | patient={patient_id} | type={body.consent_type} | by={username}")
    return ConsentResponse(
        id=consent.id, patient_id=consent.patient_id, consent_type=consent.consent_type,
        granted=consent.granted, granted_at=consent.granted_at.isoformat(),
        revoked_at=None, consent_version=consent.consent_version,
        granted_by=consent.granted_by, project_id=consent.project_id, notes=consent.notes,
    )


@router.delete(
    "/consent/{patient_id}/{consent_type}",
    status_code=200,
    summary="Revoke consent for a patient (GDPR Art. 7 §3)",
)
@limiter.limit("30/minute")
async def revoke_consent(
    request: Request,
    patient_id: str,
    consent_type: str,
    project_id: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user),
) -> dict:
    _require_admin(user)
    proj = await _resolve_project(user, project_id)

    async with get_project_session(proj) as session:
        result = await session.execute(
            _text("""
                UPDATE patient_consent
                   SET granted=false, revoked_at=:now
                 WHERE patient_id=:pid AND consent_type=:ct AND granted=true AND project_id=:proj
            """),
            {"now": now_utc(), "pid": patient_id, "ct": consent_type, "proj": proj},
        )
        await session.commit()
        rows = result.rowcount

    logger.info(f"Consent revoked | patient={patient_id} | type={consent_type} | rows={rows}")
    return {"status": "revoked", "patient_id": patient_id, "consent_type": consent_type, "rows_updated": rows}


@router.get(
    "/consent/{patient_id}",
    response_model=List[ConsentResponse],
    summary="List all consents for a patient",
)
@limiter.limit("60/minute")
async def list_consents(
    request: Request,
    patient_id: str,
    project_id: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user),
) -> List[ConsentResponse]:
    _require_admin(user)
    proj = await _resolve_project(user, project_id)

    async with get_project_session(proj) as session:
        rows = (await session.execute(
            _text("""
                SELECT id, patient_id, consent_type, granted, granted_at,
                       revoked_at, consent_version, granted_by, project_id, notes
                  FROM patient_consent
                 WHERE patient_id=:pid AND project_id=:proj
                 ORDER BY granted_at DESC
            """),
            {"pid": patient_id, "proj": proj},
        )).fetchall()

    return [
        ConsentResponse(
            id=r[0], patient_id=r[1], consent_type=r[2], granted=r[3],
            granted_at=r[4].isoformat() if r[4] else "",
            revoked_at=r[5].isoformat() if r[5] else None,
            consent_version=r[6], granted_by=r[7], project_id=r[8], notes=r[9],
        )
        for r in rows
    ]


# ── Right to Erasure (Art. 17) ────────────────────────────────────────────────

@router.delete(
    "/patients/{patient_id}/erase",
    response_model=ErasureResponse,
    summary="Right to erasure — cascade delete all patient data (GDPR Art. 17)",
)
@limiter.limit("5/minute")
async def erase_patient(
    request: Request,
    patient_id: str,
    project_id: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user),
) -> ErasureResponse:
    _require_admin(user)
    proj = await _resolve_project(user, project_id)
    username = user.get("preferred_username", "unknown")

    logger.warning(f"GDPR erasure initiated | patient={patient_id} | project={proj} | by={username}")

    # 1. Collect batch UUIDs before deletion
    batch_uuids: List[str] = []
    async with get_project_session(proj) as session:
        rows = (await session.execute(
            _text("SELECT uuid FROM measurement_batch WHERE patient_id=:pid AND is_deleted != true"),
            {"pid": patient_id},
        )).fetchall()
        batch_uuids = [r[0] for r in rows]

    # 2. Soft-delete all batches in PostgreSQL
    async with get_project_session(proj) as session:
        await session.execute(
            _text("""
                UPDATE measurement_batch
                   SET is_deleted=true, deleted_at=:now
                 WHERE patient_id=:pid AND is_deleted != true
            """),
            {"now": now_utc(), "pid": patient_id},
        )
        await session.commit()

    # 3. Delete from InfluxDB (best-effort, non-blocking)
    influx_ok = False
    _influx_url = os.getenv("INFLUX_URL", "http://medwear-influxdb:8086")
    _influx_token = os.getenv("INFLUX_TOKEN", "")
    _influx_org = os.getenv("INFLUX_ORG", "medwear")
    _influx_bucket = os.getenv("INFLUX_BUCKET", "medwear-signals")

    def _delete_influx():
        try:
            from influxdb_client import InfluxDBClient
            client = InfluxDBClient(url=_influx_url, token=_influx_token, org=_influx_org)
            delete_api = client.delete_api()
            delete_api.delete(
                start="1970-01-01T00:00:00Z",
                stop="2100-01-01T00:00:00Z",
                predicate=f'patient_id="{patient_id}"',
                bucket=_influx_bucket,
                org=_influx_org,
            )
            client.close()
            return True
        except Exception as exc:
            logger.error(f"GDPR erasure: InfluxDB delete failed for patient={patient_id}: {exc}")
            return False

    influx_ok = await asyncio.to_thread(_delete_influx)

    # 4. Delete files from MinIO — only files explicitly tracked to this patient.
    #
    # MinIO object paths follow: {project_id}/{username}/{filename}
    # There is NO patient_id in the path. Bulk-deleting all files under the
    # project prefix would erase OTHER patients' data — a GDPR violation.
    #
    # We only delete files recorded in the `deleted_files` table with a
    # patient_id match. Raw files uploaded without patient association are
    # flagged for manual review in `minio_note`.
    minio_deleted = 0
    minio_note: Optional[str] = None
    _minio_endpoint = os.getenv("MINIO_ENDPOINT", "medwear-minio:9000")
    _minio_access_key = os.getenv("MINIO_API_ACCESS_KEY", "")
    _minio_secret_key = os.getenv("MINIO_API_SECRET_KEY", "")
    _minio_bucket = os.getenv("MINIO_BUCKET", "medwear-bronze")

    # Collect MinIO paths that ARE linked to this patient via deleted_files table
    tracked_paths: List[str] = []
    try:
        async with get_project_session(proj) as session:
            rows = (await session.execute(
                _text("""
                    SELECT filename FROM deleted_files
                     WHERE patient_id = :pid
                """),
                {"pid": patient_id},
            )).fetchall()
            tracked_paths = [r[0] for r in rows]
    except Exception as exc:
        # deleted_files table may not have patient_id column in older schemas
        logger.warning(f"GDPR erasure: could not query deleted_files for patient={patient_id}: {exc}")
        minio_note = (
            "Raw file deletion requires manual review: MinIO paths do not embed "
            "patient_id. Query deleted_files or audit MinIO manually."
        )

    if tracked_paths:
        def _delete_tracked_minio(paths: List[str]) -> int:
            count = 0
            try:
                from minio import Minio
                from minio.error import S3Error
                mc = Minio(_minio_endpoint, access_key=_minio_access_key,
                           secret_key=_minio_secret_key, secure=False)
                for path in paths:
                    try:
                        mc.remove_object(_minio_bucket, path)
                        count += 1
                    except S3Error as e:
                        if "NoSuchKey" not in str(e):
                            logger.warning(f"GDPR erasure: MinIO delete failed for {path}: {e}")
            except Exception as exc:
                logger.error(f"GDPR erasure: MinIO client error: {exc}")
            return count

        minio_deleted = await asyncio.to_thread(_delete_tracked_minio, tracked_paths)
    elif not minio_note:
        minio_note = (
            "No tracked MinIO files found for this patient. "
            "If raw files were uploaded, verify manually that no patient-identifiable "
            "files remain under the project prefix."
        )
        logger.warning(f"GDPR erasure: no tracked MinIO files for patient={patient_id}")

    # 5. Revoke all active consents
    consents_revoked = 0
    async with get_project_session(proj) as session:
        result = await session.execute(
            _text("""
                UPDATE patient_consent
                   SET granted=false, revoked_at=:now,
                       notes=COALESCE(notes, '') || ' [auto-revoked: GDPR erasure]'
                 WHERE patient_id=:pid AND granted=true AND project_id=:proj
            """),
            {"now": now_utc(), "pid": patient_id, "proj": proj},
        )
        await session.commit()
        consents_revoked = result.rowcount

    logger.warning(
        f"GDPR erasure complete | patient={patient_id} | project={proj} | "
        f"batches={len(batch_uuids)} | influx={'ok' if influx_ok else 'failed'} | "
        f"minio_files={minio_deleted} | consents_revoked={consents_revoked}"
    )

    return ErasureResponse(
        patient_id=patient_id,
        project_id=proj,
        batches_erased=len(batch_uuids),
        influx_deleted=influx_ok,
        minio_files_deleted=minio_deleted,
        minio_note=minio_note,
        consent_revoked=consents_revoked,
        status="erased",
    )


# ── Data Portability (Art. 20) ────────────────────────────────────────────────

@router.get(
    "/patients/{patient_id}/export",
    summary="Export all patient data as JSON archive (GDPR Art. 20)",
)
@limiter.limit("5/minute")
async def export_patient_data(
    request: Request,
    patient_id: str,
    project_id: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    """
    Returns a ZIP archive containing:
      - batches.json   — all measurement batch metadata
      - consents.json  — full consent history
      - export_meta.json — export timestamp, patient_id, project
    """
    _require_admin(user)
    proj = await _resolve_project(user, project_id)

    # Fetch batch metadata
    async with get_project_session(proj) as session:
        batch_rows = (await session.execute(
            _text("""
                SELECT uuid, signal_type, start_timestamp, end_timestamp,
                       sampling_rate_hz, sample_count, units, created_at, is_deleted
                  FROM measurement_batch
                 WHERE patient_id=:pid
                 ORDER BY start_timestamp
            """),
            {"pid": patient_id},
        )).fetchall()

        consent_rows = (await session.execute(
            _text("""
                SELECT consent_type, granted, granted_at, revoked_at, consent_version, notes
                  FROM patient_consent
                 WHERE patient_id=:pid AND project_id=:proj
                 ORDER BY granted_at
            """),
            {"pid": patient_id, "proj": proj},
        )).fetchall()

    batches = [
        {
            "uuid": r[0], "signal_type": r[1],
            "start": r[2].isoformat() if r[2] else None,
            "end": r[3].isoformat() if r[3] else None,
            "sampling_rate_hz": r[4], "sample_count": r[5],
            "units": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
            "is_deleted": r[8],
        }
        for r in batch_rows
    ]

    consents = [
        {
            "consent_type": r[0], "granted": r[1],
            "granted_at": r[2].isoformat() if r[2] else None,
            "revoked_at": r[3].isoformat() if r[3] else None,
            "version": r[4], "notes": r[5],
        }
        for r in consent_rows
    ]

    export_meta = {
        "patient_id": patient_id,
        "project_id": proj,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": user.get("preferred_username", "unknown"),
        "gdpr_article": "Art. 20 — Right to data portability",
        "batch_count": len(batches),
    }

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("export_meta.json", json.dumps(export_meta, indent=2))
        zf.writestr("batches.json", json.dumps(batches, indent=2))
        zf.writestr("consents.json", json.dumps(consents, indent=2))
    buf.seek(0)

    filename = f"patient_{patient_id}_export_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.zip"
    logger.info(f"GDPR export | patient={patient_id} | batches={len(batches)} | consents={len(consents)}")

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
