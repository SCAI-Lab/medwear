import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from minio.error import S3Error
from sqlalchemy import select

from medwear_med_api.auth import require_read, require_admin, KEYCLOAK_CLIENT
from medwear_med_api.db import get_project_session
from medwear_med_api.errors import api_error
from medwear_med_api.models import UserProject
from medwear_med_api.routers.utils import get_minio_client, MINIO_BUCKET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Files"])


@router.get("/files", status_code=200, summary="List uploaded files in MinIO")
async def list_files(
    prefix: Optional[str] = Query(None),
    my_files: bool = Query(False),
    show_deleted: bool = Query(False),
    user: Dict[str, Any] = Depends(require_read),
):
    try:
        from medwear_med_api.db import get_all_projects
        from sqlalchemy import text as _text

        client = get_minio_client()
        username = user.get("preferred_username", "")
        caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
        is_global_admin = "global_admin" in caller_roles
        dynamic_projects = await get_all_projects()

        if is_global_admin:
            allowed_project_ids = dynamic_projects
        else:
            allowed_project_ids = []
            for proj in dynamic_projects:
                try:
                    async with get_project_session(proj) as session:
                        result = await session.execute(
                            select(UserProject).where(UserProject.username == username)
                        )
                        rows = result.scalars().all()
                        if rows:
                            allowed_project_ids.extend([r.project_id for r in rows])
                except Exception as e:
                    logger.warning(f"Failed to fetch user-project mappings from {proj}: {e}")
                    continue

        if show_deleted:
            all_deleted = []
            for proj in allowed_project_ids:
                try:
                    async with get_project_session(proj) as session:
                        result = await session.execute(
                            _text("SELECT filename, username, deleted_at FROM deleted_files ORDER BY deleted_at DESC")
                        )
                        for row in result.fetchall():
                            if row[0].endswith("/.keep"):
                                continue
                            all_deleted.append({
                                "filename": row[0],
                                "size_bytes": 0,
                                "last_modified": (row[2].isoformat() + "Z") if row[2] else None,
                                "is_deleted": True,
                                "deleted_by": row[1],
                            })
                except Exception as e:
                    logger.warning(f"Failed to fetch deleted files from {proj}: {e}")
                    continue
            return {"files": all_deleted, "count": len(all_deleted)}

        deleted_names = set()
        for proj in allowed_project_ids:
            try:
                async with get_project_session(proj) as session:
                    res = await session.execute(_text("SELECT filename FROM deleted_files"))
                    for row in res.fetchall():
                        deleted_names.add(row[0])
            except Exception as e:
                logger.warning(f"Failed to fetch deleted file names from {proj}: {e}")
                continue

        files = []
        for project_id in allowed_project_ids:
            if my_files:
                effective_prefix = f"{project_id}/{username}/"
            else:
                effective_prefix = f"{project_id}/"
            if prefix:
                effective_prefix = f"{project_id}/{prefix}"
            try:
                objects = client.list_objects(MINIO_BUCKET, prefix=effective_prefix, recursive=True)
                for obj in objects:
                    if obj.object_name.endswith("/.keep"):
                        continue
                    if obj.object_name in deleted_names:
                        continue
                    files.append({
                        "filename": obj.object_name,
                        "size_bytes": obj.size,
                        "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                        "is_deleted": False,
                    })
            except S3Error as e:
                logger.warning(f"Failed to list objects from MinIO for project {project_id}: {e}")
                continue

        return {"files": files, "count": len(files)}
    except S3Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=api_error("STORAGE_ERROR", str(e)),
        )


@router.get("/files/{filename:path}", status_code=200, summary="Download a file from MinIO")
async def download_file(
    filename: str,
    user: Dict[str, Any] = Depends(require_read),
):
    try:
        client = get_minio_client()
        response = client.get_object(MINIO_BUCKET, filename)
        return StreamingResponse(
            response.stream(32 * 1024),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename.split("/")[-1]}"'},
        )
    except S3Error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("NOT_FOUND", "File not found", filename=filename),
        )


@router.delete("/files/{filename:path}", status_code=200, summary="Soft delete a file (purged after 24h)")
async def delete_file(
    filename: str,
    user: Dict[str, Any] = Depends(require_admin),
):
    from medwear_med_api.db import get_all_projects, ensure_deleted_files_table
    from sqlalchemy import text as _text

    if filename.endswith("/.keep") or filename == ".keep":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_error("FORBIDDEN_OPERATION", "System files cannot be deleted"),
        )

    username = user.get("preferred_username", "")
    parts = filename.split("/")
    project_id = parts[0] if len(parts) > 1 else None

    dynamic_projects = await get_all_projects()
    if not project_id or project_id not in dynamic_projects:
        project_id = dynamic_projects[0] if dynamic_projects else None
    if not project_id:
        raise HTTPException(status_code=400, detail=api_error("NO_PROJECT", "No project found"))

    await ensure_deleted_files_table(project_id)

    try:
        async with get_project_session(project_id) as session:
            await session.execute(
                _text("INSERT INTO deleted_files (filename, username, deleted_at) VALUES (:f, :u, NOW())"),
                {"f": filename, "u": username}
            )
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to log deleted file: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=api_error("INTERNAL_SERVER_ERROR", f"Failed to soft-delete file: {e}"),
        )

    return {"filename": filename, "status": "soft_deleted",
            "message": "File will be permanently deleted after 24 hours."}


@router.post("/files/{filename:path}/restore", status_code=200, summary="Restore a soft-deleted file")
async def restore_file(
    filename: str,
    user: Dict[str, Any] = Depends(require_admin),
):
    from medwear_med_api.db import get_all_projects
    from sqlalchemy import text as _text

    dynamic_projects = await get_all_projects()
    for proj in dynamic_projects:
        try:
            async with get_project_session(proj) as session:
                result = await session.execute(
                    _text("DELETE FROM deleted_files WHERE filename=:f RETURNING id"),
                    {"f": filename}
                )
                if result.rowcount > 0:
                    await session.commit()
                    return {"filename": filename, "status": "restored"}
        except Exception as e:
            logger.warning(f"Failed to restore deleted file '{filename}' from {proj}: {e}")
            continue
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=api_error("NOT_FOUND", "Deleted file record not found", filename=filename),
    )
