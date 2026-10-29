import re
import time
import logging
from fastapi import Request
from jose import jwt, JWTError
from typing import Optional
from medwear_med_api.models import AuditLog
from medwear_med_api.db import get_all_projects, get_project_session
from sqlalchemy import text as _text

logger = logging.getLogger(__name__)

_SKIP_PATHS = {"/health", "/", "/docs", "/openapi.json", "/redoc"}

# UUID pattern used to detect batch/file UUIDs in request paths
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


async def get_user_projects(username: str) -> list[str]:
    try:
        projects = await get_all_projects()
    except Exception as e:
        logger.error(f"Failed to fetch projects for audit user lookup: {e}")
        raise

    matched: list[str] = []
    query_errors = []
    for proj in projects:
        try:
            async with get_project_session(proj) as session:
                row = await session.execute(
                    _text("SELECT project_id FROM user_project WHERE username=:u"),
                    {"u": username}
                )
                if row.fetchone():
                    matched.append(proj)
        except Exception as e:
            logger.warning(f"Failed to query user_project in {proj} for user {username}: {e}")
            query_errors.append(e)

    if query_errors:
        raise RuntimeError(
            f"User project lookup failed for '{username}': {len(query_errors)} project(s) could not be queried"
        )
    return matched


async def _project_for_batch_uuid(batch_uuid: str, projects: list) -> Optional[str]:
    """Look up which project a batch UUID belongs to."""
    for proj in projects:
        try:
            async with get_project_session(proj) as session:
                row = await session.execute(
                    _text("SELECT uuid FROM measurement_batch WHERE uuid=:u LIMIT 1"),
                    {"u": batch_uuid}
                )
                if row.fetchone():
                    return proj
        except Exception as e:
            logger.warning(f"Failed to look up batch uuid {batch_uuid} in project {proj}: {e}")
            continue
    return None


async def audit_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - start_time) * 1000

    if request.url.path in _SKIP_PATHS or request.url.path.startswith("/static"):
        return response

    request_id = getattr(request.state, "request_id", None)

    username = "anonymous"
    try:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth.split(" ")[1]
            payload = jwt.get_unverified_claims(token)
            username = payload.get("preferred_username", "anonymous")
    except JWTError:
        pass

    if username == "anonymous":
        return response

    try:
        # Detect global_admin from token
        try:
            _payload = jwt.get_unverified_claims(request.headers.get("Authorization", "").split(" ")[-1])
            _roles = _payload.get("resource_access", {})
            is_global_admin = any(
                "global_admin" in v.get("roles", [])
                for v in _roles.values() if isinstance(v, dict)
            )
        except Exception:
            is_global_admin = False

        projects = await get_all_projects()
        project_id: str | None = None
        user_project_ids: list[str] = []

        if is_global_admin:
            # 1. Explicit project_id query param (e.g. upload, batch endpoints)
            req_project = request.query_params.get("project_id")
            if req_project and req_project in projects:
                project_id = req_project
            else:
                # 2. UUID in path → look up which project that batch belongs to
                path_str = str(request.url.path)
                uuid_match = _UUID_RE.search(path_str)
                if uuid_match:
                    project_id = await _project_for_batch_uuid(uuid_match.group(), projects)
                else:
                    project_id = None
                # 3. Fallback: log to first project (covers /admin, /auth, etc.)
                if not project_id:
                    project_id = projects[0] if projects else None
        else:
            user_project_ids = await get_user_projects(username)
            if not user_project_ids:
                logger.warning(f"No projects found for user '{username}', skipping audit log",
                               extra={"request_id": request_id})
                return response
            if len(user_project_ids) == 1:
                project_id = user_project_ids[0]
            else:
                # Multi-project user: pick from context, fall back to logging all
                req_project = request.query_params.get("project_id")
                if req_project and req_project in user_project_ids:
                    project_id = req_project
                else:
                    path_str = str(request.url.path)
                    uuid_match = _UUID_RE.search(path_str)
                    project_id = (
                        await _project_for_batch_uuid(uuid_match.group(), user_project_ids)
                        if uuid_match else None
                    )

        path = str(request.url.path)
        if path.endswith("/upload"):
            xfilename = request.headers.get("x-filename", "")
            if xfilename:
                path = f"{path}?filename={xfilename}"

        # Determine which projects to write the audit log to
        if project_id:
            target_ids = [project_id]
        elif not is_global_admin and len(user_project_ids) > 1:
            # Context ambiguous — write to all user's projects so nothing is lost
            logger.debug(f"Audit: ambiguous project for '{username}', logging to all {len(user_project_ids)} projects",
                         extra={"request_id": request_id})
            target_ids = user_project_ids
        else:
            logger.warning(f"Could not determine project for user '{username}', skipping audit log",
                           extra={"request_id": request_id})
            return response

        for tid in target_ids:
            async with get_project_session(tid) as session:
                log = AuditLog(
                    username=username,
                    method=request.method,
                    path=path,
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 2)
                )
                session.add(log)
                await session.commit()
    except Exception as e:
        logger.error(f"Audit logging failed: {e}", extra={"request_id": request_id})

    return response
