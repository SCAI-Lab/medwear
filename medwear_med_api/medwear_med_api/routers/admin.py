import io
import logging
import os
import re
from enum import Enum
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select, text as _text

from medwear_med_api.auth import (
    require_read, require_admin,
    require_global_admin, KEYCLOAK_CLIENT, Role, ROLES_OWN_MAPPINGS_ONLY,
)
from medwear_med_api.db import (
    get_all_projects, get_deleted_projects,
    create_project_db, soft_delete_project, restore_deleted_project,
    get_deleted_users, soft_delete_user, restore_deleted_user,
    get_project_session,
)
from medwear_med_api.errors import api_error
from medwear_med_api.grafana_utils import ensure_datasource
from medwear_med_api.rate_limit import limiter
from medwear_med_api.models import UserProject
from medwear_med_api.routers.utils import get_minio_client, MINIO_BUCKET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Admin"])

# Keycloak configuration — read once at module load time
KC_URL = os.getenv("KEYCLOAK_URL")
KC_REALM = os.getenv("KEYCLOAK_REALM")
KC_CLIENT = os.getenv("KEYCLOAK_CLIENT_ID")
KC_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET")
KC_ADMIN_USER = os.getenv("KEYCLOAK_ADMIN")
KC_ADMIN_PASS = os.getenv("KEYCLOAK_ADMIN_PASSWORD")

_missing = [name for name, val in [
    ("KEYCLOAK_URL", KC_URL),
    ("KEYCLOAK_REALM", KC_REALM),
    ("KEYCLOAK_CLIENT_ID", KC_CLIENT),
    ("KEYCLOAK_CLIENT_SECRET", KC_SECRET),
    ("KEYCLOAK_ADMIN", KC_ADMIN_USER),
    ("KEYCLOAK_ADMIN_PASSWORD", KC_ADMIN_PASS),
] if not val]
if _missing:
    raise RuntimeError(f"Missing required env variables: {', '.join(_missing)}. Check your .env file.")


class OperationStatus(str, Enum):
    ASSIGNED = "assigned"
    REMOVED = "removed"
    CREATED = "created"
    SOFT_DELETED = "soft_deleted"
    RESTORED = "restored"
    PASSWORD_CHANGED = "password_changed"


class UserProjectAssign(BaseModel):
    username: str
    project_id: str
    role: Role = Role.COLLABORATOR


class CreateUserRequest(BaseModel):
    username: str
    password: str
    project_id: str
    role: Role


class CreateProjectRequest(BaseModel):
    project_name: str


class ChangePasswordRequest(BaseModel):
    username: str
    old_password: str
    new_password: str


@router.post("/admin/add-user-to-project", status_code=201, summary="Assign a user to a project")
async def assign_user_project(
    body: UserProjectAssign,
    user: Dict[str, Any] = Depends(require_admin),
):
    async with get_project_session(body.project_id) as session:
        existing = await session.execute(
            select(UserProject).where(
                UserProject.username == body.username,
                UserProject.project_id == body.project_id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=api_error(
                "DUPLICATE_ASSIGNMENT", f"User '{body.username}' is already assigned to project '{body.project_id}'"), )
        mapping = UserProject(username=body.username, project_id=body.project_id, role=body.role.value)
        session.add(mapping)
        await session.commit()
    return {"username": body.username, "project_id": body.project_id, "status": OperationStatus.ASSIGNED}


@router.delete("/admin/remove-user-from-project", status_code=200, summary="Remove a user from a project")
async def remove_user_project(
    username: str = Query(...),
    project_id: str = Query(...),
    user: Dict[str, Any] = Depends(require_admin),
):
    async with get_project_session(project_id) as session:
        result = await session.execute(
            select(UserProject).where(
                UserProject.username == username,
                UserProject.project_id == project_id,
            )
        )
        mapping = result.scalar_one_or_none()
        if mapping is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=api_error("NOT_FOUND", f"Mapping for user '{username}' in project '{project_id}' not found"),
            )
        await session.delete(mapping)
        await session.commit()

    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                f"{KC_URL}/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": KC_ADMIN_USER,
                    "password": KC_ADMIN_PASS})
            admin_token = token_resp.json()["access_token"]
            headers_kc = {"Authorization": f"Bearer {admin_token}"}
            users_resp = await client.get(
                f"{KC_URL}/admin/realms/{KC_REALM}/users?username={username}&exact=true",
                headers=headers_kc
            )
            kc_users = users_resp.json()
            if kc_users:
                user_id = kc_users[0]["id"]
                del_resp = await client.delete(f"{KC_URL}/admin/realms/{KC_REALM}/users/{user_id}", headers=headers_kc)
                logger.info(f"Keycloak delete {username}: {del_resp.status_code}")
            else:
                logger.warning(f"User {username} not found in Keycloak")
    except Exception as e:
        logger.error(f"Keycloak delete failed for {username}: {e}")

    return {"username": username, "project_id": project_id, "status": OperationStatus.REMOVED}


@router.get("/admin/user-project-mappings", status_code=200, summary="List all user-project mappings")
async def list_user_projects(
    username: Optional[str] = Query(None),
    user: Dict[str, Any] = Depends(require_read),
):
    client_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    user_roles = {r for r in Role if r.value in client_roles}
    can_see_own_only = bool(user_roles & ROLES_OWN_MAPPINGS_ONLY) and not (user_roles - ROLES_OWN_MAPPINGS_ONLY)

    effective_username = user.get("preferred_username") if can_see_own_only else username

    dynamic_projects = await get_all_projects()
    all_mappings = []
    for proj in dynamic_projects:
        try:
            async with get_project_session(proj) as session:
                q = select(UserProject)
                if effective_username:
                    q = q.where(UserProject.username == effective_username)
                result = await session.execute(q)
                for m in result.scalars().all():
                    all_mappings.append({
                        "username": m.username,
                        "project_id": m.project_id,
                        "created_at": m.created_at.isoformat(),
                    })
        except Exception as e:
            logger.warning(f"Failed to fetch user-project mappings from {proj}: {e}")
            continue
    return {"mappings": all_mappings}


@router.post("/admin/users", status_code=201, summary="Create a new user in Keycloak and assign to project")
async def create_user(
    body: CreateUserRequest,
    user: Dict[str, Any] = Depends(require_admin),
):
    caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    is_global_admin = "global_admin" in caller_roles
    is_project_admin = "project_admin" in caller_roles

    if not is_global_admin and not is_project_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api_error("FORBIDDEN", "project_admin or global_admin role required"),
        )

    if is_project_admin and not is_global_admin:
        caller_username = user.get("preferred_username")
        dyn = await get_all_projects()
        caller_projects = []
        for proj in dyn:
            try:
                async with get_project_session(proj) as session:
                    res = await session.execute(
                        _text("SELECT project_id FROM user_project WHERE username=:u"),
                        {"u": caller_username}
                    )
                    caller_projects.extend([r[0] for r in res.fetchall()])
            except Exception as e:
                logger.warning(f"Failed to fetch caller projects from {proj}: {e}")
                continue
        if not caller_projects:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=api_error("FORBIDDEN", "You have no assigned project"),
            )
        body.project_id = caller_projects[0]
        if body.role == Role.GLOBAL_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=api_error("FORBIDDEN", "project_admin cannot create global_admin users"),
            )
        if body.role in [Role.PROJECT_ADMIN]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=api_error("FORBIDDEN", "Only global_admin can create admin or project_admin users"),
            )

    all_projects = await get_all_projects()
    for proj in all_projects:
        try:
            async with get_project_session(proj) as session:
                result = await session.execute(
                    select(UserProject).where(UserProject.username == body.username)
                )
                if result.scalar_one_or_none():
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=api_error(
                            "DUPLICATE_USER",
                            f"Username '{body.username}' already exists in project '{proj}'. "
                            "Usernames must be unique across all projects.",
                        ),
                    )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Failed to check duplicate user in {proj}: {e}")
            continue

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"{KC_URL}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli",
                  "username": KC_ADMIN_USER, "password": KC_ADMIN_PASS}
        )
        if token_resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=api_error("INTERNAL_SERVER_ERROR", "Failed to obtain Keycloak admin token"),
            )

        admin_token = token_resp.json()["access_token"]
        headers_kc = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}

        user_resp = await client.post(
            f"{KC_URL}/admin/realms/{KC_REALM}/users",
            headers=headers_kc,
            json={
                "username": body.username,
                "enabled": True,
                "credentials": [{"type": "password", "value": body.password, "temporary": False}],
                "requiredActions": []
            }
        )
        if user_resp.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=api_error("DUPLICATE_USER", f"Username '{body.username}' already exists in Keycloak"),
            )
        if user_resp.status_code not in [201, 200]:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=api_error("INTERNAL_SERVER_ERROR", f"Keycloak user creation failed: {user_resp.text}"),
            )

        users_resp = await client.get(
            f"{KC_URL}/admin/realms/{KC_REALM}/users?username={body.username}&exact=true",
            headers=headers_kc
        )
        users = users_resp.json()
        if not users:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=api_error("INTERNAL_SERVER_ERROR", "User created in Keycloak but could not be retrieved"),
            )
        user_id = users[0]["id"]

        clients_resp = await client.get(f"{KC_URL}/admin/realms/{KC_REALM}/clients", headers=headers_kc)
        client_obj = next((c for c in clients_resp.json() if c.get("clientId") == KC_CLIENT), None)
        if not client_obj:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=api_error("INTERNAL_SERVER_ERROR", f"Keycloak client '{KC_CLIENT}' not found"),
            )
        client_id = client_obj["id"]

        roles_resp = await client.get(f"{KC_URL}/admin/realms/{KC_REALM}/clients/{client_id}/roles", headers=headers_kc)
        role_obj = next((r for r in roles_resp.json() if r["name"] == body.role.value), None)
        if not role_obj:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=api_error(
                    "INVALID_ROLE",
                    f"Role '{body.role.value}' not found in Keycloak",
                    available_roles=[
                        r["name"] for r in roles_resp.json()]),
            )

        await client.post(
            f"{KC_URL}/admin/realms/{KC_REALM}/users/{user_id}/role-mappings/clients/{client_id}",
            headers=headers_kc,
            json=[role_obj]
        )

    # Global admins are not assigned to a specific project — store in project1 as reference only if needed
    # For global_admin: skip per-project user_project insert; central deleted_users table not needed
    # We use project_id='global' as a sentinel — just skip the project DB insert
    if body.role != Role.GLOBAL_ADMIN and body.project_id != "global":
        async with get_project_session(body.project_id) as session:
            existing = await session.execute(
                select(UserProject).where(
                    UserProject.username == body.username,
                    UserProject.project_id == body.project_id
                )
            )
            if not existing.scalar_one_or_none():
                session.add(UserProject(username=body.username, project_id=body.project_id, role=body.role.value))
                await session.commit()

    return {
        "status": OperationStatus.CREATED,
        "username": body.username,
        "project_id": body.project_id if body.role != Role.GLOBAL_ADMIN else None,
        "role": body.role.value,
        "message": "User created. Temporary password must be changed on first login."
    }


@router.get("/admin/users", status_code=200, summary="List users in a project")
async def list_project_users(
    project_id: Optional[str] = Query(None),
    show_deleted: bool = Query(False),
    user: Dict[str, Any] = Depends(require_admin),
):
    caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    is_global_admin = "global_admin" in caller_roles
    caller_username = user.get("preferred_username")

    dynamic_projects = await get_all_projects()

    if is_global_admin:
        target_projects = [project_id] if project_id else dynamic_projects
    else:
        caller_projects = []
        for proj in dynamic_projects:
            try:
                async with get_project_session(proj) as session:
                    result = await session.execute(
                        select(UserProject).where(
                            UserProject.username == caller_username,
                            UserProject.project_id == proj,
                        )
                    )
                    if result.scalar_one_or_none():
                        caller_projects.append(proj)
            except Exception as e:
                logger.warning(f"Failed to resolve caller projects from {proj}: {e}")
                continue
        target_projects = [project_id] if project_id and project_id in caller_projects else caller_projects

    if show_deleted:
        deleted = await get_deleted_users(project_id=project_id)
        if not is_global_admin:
            # project_admin: only see their own project's deleted users, never global admins
            caller_projects_set = set(target_projects)
            deleted = [d for d in deleted
                       if d["project_id"] in caller_projects_set
                       and d["role"] not in ("global_admin",)]
        return {"users": deleted, "count": len(deleted), "show_deleted": True}

    if not is_global_admin and not target_projects:
        return {"users": [], "count": 0}

    all_users = []

    # Fetch global_admin users from Keycloak (only for global_admin callers)
    if is_global_admin and not project_id:
        try:
            soft_deleted_usernames = {d["username"] for d in await get_deleted_users()}
            async with httpx.AsyncClient() as kc_client:
                token_resp = await kc_client.post(
                    f"{KC_URL}/realms/master/protocol/openid-connect/token",
                    data={"grant_type": "password", "client_id": "admin-cli",
                          "username": KC_ADMIN_USER,
                          "password": KC_ADMIN_PASS}
                )
                admin_token = token_resp.json()["access_token"]
                headers_kc = {"Authorization": f"Bearer {admin_token}"}
                clients_resp = await kc_client.get(f"{KC_URL}/admin/realms/{KC_REALM}/clients", headers=headers_kc)
                client_obj = next((c for c in clients_resp.json() if c.get("clientId") == KC_CLIENT), None)
                if client_obj:
                    client_kc_id = client_obj["id"]
                    ga_users_resp = await kc_client.get(
                        f"{KC_URL}/admin/realms/{KC_REALM}/clients/{client_kc_id}/roles/global_admin/users",
                        headers=headers_kc
                    )
                for u in (ga_users_resp.json() if client_obj else []):
                    uname = u.get("username")
                    if uname not in soft_deleted_usernames:
                        all_users.append({
                            "username": uname,
                            "project_id": "global",
                            "role": "global_admin",
                            "created_at": None,
                            "is_deleted": False,
                        })
        except Exception as e:
            logger.warning(f"Failed to fetch global_admin users from Keycloak: {e}")

    for proj in target_projects:
        try:
            async with get_project_session(proj) as session:
                result = await session.execute(
                    select(UserProject).where(UserProject.project_id == proj)
                )
                all_users.extend([{
                    "username": u.username,
                    "project_id": u.project_id or proj,
                    "role": u.role,
                    "created_at": u.created_at.isoformat(),
                    "is_deleted": False,
                } for u in result.scalars().all()])
        except Exception as e:
            logger.warning(f"Failed to list users from {proj}: {e}")
            continue

    return {"users": all_users, "count": len(all_users)}


@router.delete("/admin/user-project", status_code=200, summary="Soft-delete a user from a project (purged after 24h)")
async def delete_user_from_project(
    username: str = Query(...),
    project_id: str = Query(...),
    user: Dict[str, Any] = Depends(require_admin),
):
    caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    is_global_admin = "global_admin" in caller_roles
    caller_username = user.get("preferred_username")

    if not is_global_admin:
        async with get_project_session(project_id) as session:
            result = await session.execute(
                select(UserProject).where(UserProject.username == caller_username)
            )
            caller_projects = [r.project_id for r in result.scalars().all()]
            if project_id not in caller_projects:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=api_error("FORBIDDEN", "You can only remove users from your own project"),
                )

    # Global admins have no user_project row — handle separately
    if project_id == "global":
        if not is_global_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=api_error("FORBIDDEN", "Only global_admin can remove global_admin users"),
            )
        if username == caller_username:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=api_error("FORBIDDEN", "You cannot remove yourself"),
            )
        await soft_delete_user(username, "global", "global_admin", caller_username)
        return {"username": username, "project_id": "global", "status": OperationStatus.SOFT_DELETED,
                "message": "Global admin will be permanently deleted from Keycloak after 24 hours."}

    async with get_project_session(project_id) as session:
        result = await session.execute(
            select(UserProject).where(
                UserProject.username == username,
                UserProject.project_id == project_id
            )
        )
        mapping = result.scalar_one_or_none()
        if mapping is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=api_error("NOT_FOUND", f"User '{username}' not found in project '{project_id}'"),
            )
        target_role = mapping.role

        # project_admin cannot delete other project_admins — only global_admin can
        if not is_global_admin and target_role in ("project_admin", "global_admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=api_error("FORBIDDEN", "Only global_admin can remove project_admin or global_admin users"),
            )

        await session.delete(mapping)
        await session.commit()

    await soft_delete_user(username, project_id, target_role, caller_username)
    return {"username": username, "project_id": project_id, "status": OperationStatus.SOFT_DELETED,
            "message": "User will be permanently deleted from Keycloak after 24 hours."}


@router.post("/admin/users/{username}/restore", status_code=200, summary="Restore a soft-deleted user")
async def restore_user(
    username: str,
    project_id: str = Query(...),
    user: Dict[str, Any] = Depends(require_admin),
):
    caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    is_global_admin = "global_admin" in caller_roles
    caller_username = user.get("preferred_username")

    if not is_global_admin:
        async with get_project_session(project_id) as session:
            result = await session.execute(
                select(UserProject).where(UserProject.username == caller_username)
            )
            caller_projects = [r.project_id for r in result.scalars().all()]
            if project_id not in caller_projects:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=api_error("FORBIDDEN", "You can only restore users in your own project"),
                )

    restored = await restore_deleted_user(username, project_id)
    if restored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("NOT_FOUND", f"User '{username}' not found in deleted users for project '{project_id}'"),
        )

    # Global admins have no user_project row — just removing from deleted_users is enough
    if project_id != "global":
        async with get_project_session(project_id) as session:
            existing = await session.execute(
                select(UserProject).where(
                    UserProject.username == username,
                    UserProject.project_id == project_id
                )
            )
            if not existing.scalar_one_or_none():
                session.add(UserProject(username=username, project_id=project_id, role=restored["role"]))
                await session.commit()

    return {"username": username, "project_id": project_id, "status": OperationStatus.RESTORED}


@router.post("/admin/projects", status_code=201, summary="Create a new project database")
async def create_project(
    body: CreateProjectRequest,
    user: Dict[str, Any] = Depends(require_global_admin),
):
    name = body.project_name.strip().lower().replace(" ", "_")
    if not re.match(r'^[a-z0-9_]+$', name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=api_error(
            "INVALID_PROJECT_NAME", "Project name can only contain lowercase letters, numbers and underscores"), )

    if not name.startswith("medwear_"):
        name = f"medwear_{name}"

    existing = await get_all_projects()
    if name in existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=api_error("DUPLICATE_PROJECT", f"Project '{name}' already exists"),
        )

    try:
        await create_project_db(name)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=api_error("INTERNAL_SERVER_ERROR", str(e)),
        )

    grafana_ok = False
    try:
        grafana_ok = await ensure_datasource(name)
    except Exception as e:
        logger.warning(f"Grafana datasource creation failed for {name} (non-fatal): {e}")

    minio_ok = False
    try:
        minio_client = get_minio_client()
        placeholder = f"{name}/.keep"
        minio_client.put_object(MINIO_BUCKET, placeholder, io.BytesIO(b""), 0, content_type="application/octet-stream")
        minio_ok = True
    except Exception as e:
        logger.warning(f"MinIO folder init failed for {name} (non-fatal): {e}")

    return {
        "status": OperationStatus.CREATED,
        "project_id": name,
        "message": f"Project {name} created with all required tables.",
        "grafana_datasource": "created" if grafana_ok else "failed (check logs)",
        "minio_folder": "created" if minio_ok else "failed (check logs)",
    }


@router.get("/admin/projects", status_code=200, summary="List all projects")
async def list_projects(
    show_deleted: bool = Query(False),
    user: Dict[str, Any] = Depends(require_admin),
):
    if show_deleted:
        deleted = await get_deleted_projects()
        return {"projects": deleted, "count": len(deleted), "show_deleted": True}
    projects = await get_all_projects()
    return {"projects": projects, "count": len(projects), "show_deleted": False}


@router.delete("/admin/projects/{project_name}", status_code=200, summary="Soft delete a project (purged after 24h)")
async def delete_project(
    project_name: str,
    user: Dict[str, Any] = Depends(require_global_admin),
):
    existing = await get_all_projects()
    if project_name not in existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("NOT_FOUND", f"Project '{project_name}' not found"),
        )
    username = user.get("preferred_username", "unknown")
    await soft_delete_project(project_name, username)
    return {"project_name": project_name, "status": OperationStatus.SOFT_DELETED,
            "message": "Project will be permanently deleted after 24 hours."}


@router.post("/admin/projects/{project_name}/restore", status_code=200, summary="Restore a soft-deleted project")
async def restore_project(
    project_name: str,
    user: Dict[str, Any] = Depends(require_global_admin),
):
    deleted = await get_deleted_projects()
    if not any(d["project_name"] == project_name for d in deleted):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("NOT_FOUND", f"Project '{project_name}' not found in deleted projects"),
        )
    await restore_deleted_project(project_name)
    return {"project_name": project_name, "status": OperationStatus.RESTORED}


@router.get("/admin/activities", status_code=200, summary="Get audit log activities")
async def get_activities(
    username: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    path_filter: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: Dict[str, Any] = Depends(require_admin),
):
    dynamic_projects = await get_all_projects()
    caller_roles = user.get("resource_access", {}).get(KEYCLOAK_CLIENT, {}).get("roles", [])
    is_global_admin = "global_admin" in caller_roles
    caller_username = user.get("preferred_username")

    if is_global_admin:
        target_projects = [project_id] if project_id else dynamic_projects
    else:
        caller_projects = []
        for proj in dynamic_projects:
            try:
                async with get_project_session(proj) as session:
                    result = await session.execute(
                        select(UserProject).where(UserProject.username == caller_username)
                    )
                    rows = result.scalars().all()
                    if rows:
                        caller_projects.extend([r.project_id for r in rows])
            except Exception as e:
                logger.warning(f"Failed to resolve caller projects for audit log from {proj}: {e}")
                continue
        target_projects = [project_id] if project_id and project_id in caller_projects else caller_projects

    all_logs = []
    for proj in target_projects:
        try:
            async with get_project_session(proj) as session:
                where_parts = []
                q_params = {"lim": limit}
                if username:
                    where_parts.append("username = :u")
                    q_params["u"] = username
                if path_filter == "files":
                    where_parts.append("(path LIKE '%/files/%' OR path LIKE '%/upload%') AND method != 'GET'")
                where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
                result = await session.execute(
                    _text(
                        f"SELECT id, username, method, path, status_code, duration_ms, timestamp "
                        f"FROM audit_log {where_sql} ORDER BY timestamp DESC LIMIT :lim"
                    ),
                    q_params
                )
                for row in result.fetchall():
                    ts = row[6]
                    all_logs.append({
                        "id": row[0],
                        "username": row[1],
                        "method": row[2],
                        "path": row[3],
                        "status_code": row[4],
                        "duration_ms": row[5],
                        "timestamp": (ts.isoformat() + "Z") if ts else None,
                        "project_id": proj,
                    })
        except Exception as e:
            logger.warning(f"Failed to get audit log from {proj}: {e}")
            continue

    all_logs.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"activities": all_logs[:limit], "count": len(all_logs[:limit])}


@router.post("/auth/change-password", status_code=200, summary="Change temporary password")
@limiter.limit("5/minute")
async def change_password(request: Request, body: ChangePasswordRequest):
    async with httpx.AsyncClient() as client:
        verify_resp = await client.post(
            f"{KC_URL}/realms/{KC_REALM}/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": KC_CLIENT,
                  "client_secret": KC_SECRET, "username": body.username,
                  "password": body.old_password}
        )
        if verify_resp.status_code not in [200, 400]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=api_error("UNAUTHORIZED", "Invalid credentials"),
            )

        admin_resp = await client.post(
            f"{KC_URL}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli",
                  "username": KC_ADMIN_USER, "password": KC_ADMIN_PASS}
        )
        admin_token = admin_resp.json()["access_token"]
        headers_kc = {"Authorization": f"Bearer {admin_token}"}

        users_resp = await client.get(
            f"{KC_URL}/admin/realms/{KC_REALM}/users?username={body.username}&exact=true",
            headers=headers_kc
        )
        users = users_resp.json()
        if not users:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=api_error("NOT_FOUND", f"User '{body.username}' not found"),
            )

        user_id = users[0]["id"]

        reset_resp = await client.put(
            f"{KC_URL}/admin/realms/{KC_REALM}/users/{user_id}/reset-password",
            headers=headers_kc,
            json={"type": "password", "value": body.new_password, "temporary": False}
        )
        if reset_resp.status_code not in [200, 204]:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=api_error("INTERNAL_SERVER_ERROR", "Failed to update password in Keycloak"),
            )

        await client.put(
            f"{KC_URL}/admin/realms/{KC_REALM}/users/{user_id}",
            headers=headers_kc,
            json={"requiredActions": []}
        )

    return {"status": OperationStatus.PASSWORD_CHANGED,
            "message": "Password updated successfully. Please login with your new password."}


class TokenRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/auth/token", status_code=200,
             summary="Exchange username/password for a Keycloak token (client_secret stays server-side)")
@limiter.limit("10/minute")
async def token_proxy(request: Request, body: TokenRequest):
    """Frontend calls this instead of Keycloak directly so the client_secret is never exposed in the browser."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{KC_URL}/realms/{KC_REALM}/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": KC_CLIENT,
                    "client_secret": KC_SECRET,
                    "username": body.username,
                    "password": body.password,
                },
            )
    except httpx.RequestError as exc:
        logger.error(f"Keycloak unreachable during token exchange: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=api_error("KEYCLOAK_UNAVAILABLE", "Authentication service is unreachable"),
        )

    if resp.status_code in (401, 400):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_error("UNAUTHORIZED", resp.json().get("error_description", "Invalid credentials")),
        )
    if resp.status_code != 200:
        logger.error(f"Unexpected Keycloak response during token exchange: {resp.status_code} {resp.text}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=api_error("KEYCLOAK_ERROR", "Unexpected error from authentication service"),
        )
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
        "token_type": data.get("token_type", "Bearer"),
    }


# ---------------------------------------------------------------------------
# Retention policy — project-level data auto-expiry
# ---------------------------------------------------------------------------

RETENTION_OPTIONS = {
    30: "1 month",
    90: "3 months",
    180: "6 months",
    365: "1 year",
    1095: "3 years",
    2190: "6 years",
    3650: "10 years",
}


class RetentionPolicyRequest(BaseModel):
    project_id: str
    retention_days: Optional[int] = None  # None = never expire


@router.get("/admin/retention", status_code=200, summary="Get retention policy for a project")
async def get_retention_policy(
    project_id: str = Query(...),
    user: Dict[str, Any] = Depends(require_admin),
):
    dynamic_projects = await get_all_projects()
    if project_id not in dynamic_projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("NOT_FOUND", f"Project '{project_id}' not found"),
        )
    async with get_project_session(project_id) as session:
        result = await session.execute(
            _text("SELECT retention_days, updated_at, updated_by FROM project_settings WHERE project_id = :pid"),
            {"pid": project_id},
        )
        row = result.fetchone()
    if row is None:
        return {"project_id": project_id, "retention_days": None, "label": "Never (data kept indefinitely)"}
    label = RETENTION_OPTIONS.get(row[0], f"{row[0]} days") if row[0] else "Never (data kept indefinitely)"
    return {
        "project_id": project_id,
        "retention_days": row[0],
        "label": label,
        "updated_at": row[1].isoformat() if row[1] else None,
        "updated_by": row[2],
    }


@router.put("/admin/retention", status_code=200, summary="Set or clear retention policy for a project")
async def set_retention_policy(
    body: RetentionPolicyRequest,
    user: Dict[str, Any] = Depends(require_admin),
):
    dynamic_projects = await get_all_projects()
    if body.project_id not in dynamic_projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_error("NOT_FOUND", f"Project '{body.project_id}' not found"),
        )
    if body.retention_days is not None and body.retention_days not in RETENTION_OPTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_error(
                "INVALID_RETENTION",
                f"retention_days must be one of {sorted(RETENTION_OPTIONS.keys())} or null (never expire)",
            ),
        )
    username = user.get("preferred_username", "system")
    async with get_project_session(body.project_id) as session:
        await session.execute(
            _text("""
                INSERT INTO project_settings (project_id, retention_days, updated_at, updated_by)
                VALUES (:pid, :days, NOW(), :by)
                ON CONFLICT (project_id) DO UPDATE
                SET retention_days = EXCLUDED.retention_days,
                    updated_at     = EXCLUDED.updated_at,
                    updated_by     = EXCLUDED.updated_by
            """),
            {"pid": body.project_id, "days": body.retention_days, "by": username},
        )
        await session.commit()
    label = RETENTION_OPTIONS.get(
        body.retention_days,
        "Never (data kept indefinitely)") if body.retention_days else "Never (data kept indefinitely)"
    return {
        "project_id": body.project_id,
        "retention_days": body.retention_days,
        "label": label,
        "status": "updated",
    }


@router.post("/auth/refresh", status_code=200, summary="Refresh access token using refresh token")
@limiter.limit("20/minute")
async def token_refresh(request: Request, body: RefreshRequest):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{KC_URL}/realms/{KC_REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "refresh_token",
                "client_id": KC_CLIENT,
                "client_secret": KC_SECRET,
                "refresh_token": body.refresh_token,
            },
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_error("UNAUTHORIZED", "Refresh token expired or invalid"),
        )
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
        "token_type": data.get("token_type", "Bearer"),
    }
