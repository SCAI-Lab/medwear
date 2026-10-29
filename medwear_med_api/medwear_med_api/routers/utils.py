import csv
import gzip
import io
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import StreamingResponse
from minio import Minio
from sqlalchemy import text as _text

from medwear_med_api.db import get_all_projects, get_project_session

logger = logging.getLogger(__name__)


MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "medwear-minio:9000")
MINIO_USER = os.getenv("MINIO_API_ACCESS_KEY", os.getenv("MINIO_ROOT_USER", ""))
MINIO_PASSWORD = os.getenv("MINIO_API_SECRET_KEY", os.getenv("MINIO_ROOT_PASSWORD", ""))
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "medwear-bronze")

if not MINIO_USER or not MINIO_PASSWORD:
    raise RuntimeError(
        "MinIO credentials (MINIO_API_ACCESS_KEY / MINIO_API_SECRET_KEY) are not set. Cannot start without them.")


def get_minio_client():
    return Minio(MINIO_ENDPOINT, access_key=MINIO_USER,
                 secret_key=MINIO_PASSWORD, secure=False)


async def find_batch_in_projects(
        uuid: str, include_deleted: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    dynamic_projects = await get_all_projects()
    where = ("WHERE uuid = :uuid" if include_deleted
             else "WHERE uuid = :uuid AND is_deleted != true AND ingest_status = 'complete'")
    sql = _text(
        f"SELECT uuid, patient_id, device_id, signal_type, schema_id, "
        f"start_timestamp, end_timestamp, sampling_rate_hz, sample_count, units, "
        f"metadata_json, idempotency_key, created_at, is_deleted, deleted_at "
        f"FROM measurement_batch {where}"
    )
    for proj in dynamic_projects:
        try:
            async with get_project_session(proj) as session:
                result = await session.execute(sql, {"uuid": uuid})
                row = result.fetchone()
                if row:
                    return _row_to_batch_dict(row), proj
        except Exception as e:
            logger.warning(f"Failed to find batch {uuid} in project {proj}: {e}")
            continue
    return None, None


def _row_to_batch_dict(row) -> Dict[str, Any]:
    return {
        "uuid": row[0],
        "patient_id": row[1],
        "device_id": row[2],
        "signal_type": row[3],
        "schema_id": row[4],
        "start_timestamp": (row[5].isoformat() + "Z") if row[5] else None,
        "end_timestamp": (row[6].isoformat() + "Z") if row[6] else None,
        "sampling_rate_hz": row[7],
        "sample_count": row[8],
        "units": row[9],
        "metadata_json": row[10],
        "idempotency_key": row[11],
        "created_at": (row[12].isoformat() + "Z") if row[12] else None,
        "is_deleted": row[13] or False,
        "deleted_at": (row[14].isoformat() + "Z") if row[14] else None,
    }


def _stream_csv(metadata: dict, samples: List[float]) -> StreamingResponse:
    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "uuid", "patient_id", "device_id", "signal_type",
            "start_timestamp", "end_timestamp", "sampling_rate_hz",
            "sample_count", "units", "samples"
        ])
        writer.writerow([
            metadata["uuid"],
            metadata["patient_id"],
            metadata["device_id"],
            metadata["signal_type"],
            metadata["start_timestamp"],
            metadata["end_timestamp"],
            metadata["sampling_rate_hz"],
            metadata["sample_count"],
            metadata["units"],
            ";".join(str(s) for s in samples),
        ])
        compressed = gzip.compress(buf.getvalue().encode())
        yield compressed

    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{metadata["uuid"]}.csv.gz"'},
    )
