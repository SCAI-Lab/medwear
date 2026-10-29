import hashlib
import os
import time
import httpx
from enum import Enum
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from medwear_med_api.errors import api_error

# Keycloak settings
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://medwear-keycloak:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "medwear")
KEYCLOAK_CLIENT = os.getenv("KEYCLOAK_CLIENT_ID", "med-api")

JWKS_URL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"

bearer_scheme = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# API key auth (for device clients that cannot obtain a Keycloak JWT)
#
# Provision: set DEVICE_API_KEYS to a comma-separated list of
# SHA-256 hex digests of the raw keys, e.g.:
#   DEVICE_API_KEYS=sha256(key1),sha256(key2)
# The device sends:  X-API-Key: <raw_key>
# ---------------------------------------------------------------------------
_RAW_API_KEY_HASHES = os.getenv("DEVICE_API_KEYS", "")
_VALID_API_KEY_HASHES: frozenset[str] = frozenset(
    h.strip() for h in _RAW_API_KEY_HASHES.split(",") if h.strip()
)


_API_KEY_ROLE = os.getenv("DEVICE_API_KEY_ROLE", "device")


def _user_from_api_key(raw_key: str) -> dict:
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    if key_hash not in _VALID_API_KEY_HASHES:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_error("UNAUTHORIZED", "Invalid API key"),
        )
    return {
        "preferred_username": f"device_{key_hash[:12]}",
        "resource_access": {KEYCLOAK_CLIENT: {"roles": [_API_KEY_ROLE]}},
        "sub": f"apikey:{key_hash[:16]}",
    }


_JWKS_TTL = 3600  # seconds
_jwks_cache: dict = {"keys": None, "expires_at": 0.0}


class Role(Enum):
    DEVICE = "device"        # Medical device sending data; no UI access
    COLLABORATOR = "collaborator"  # Clinical staff; read-only, sees only their own project
    RESEARCHER = "researcher"    # Read + write access; can upload and query data across their project
    PROJECT_ADMIN = "project_admin"  # Manages users and data within their assigned project(s)
    GLOBAL_ADMIN = "global_admin"  # Full access; manages projects, all users, and system configuration


# Roles that can only view their own project mappings (not all users in a project).
# Add a role here if it should be restricted to seeing only its own data.
ROLES_OWN_MAPPINGS_ONLY = {Role.COLLABORATOR, Role.DEVICE}


def get_jwks() -> dict:
    """Fetch public keys from Keycloak, cached for _JWKS_TTL seconds."""
    now = time.monotonic()
    if _jwks_cache["keys"] is None or now > _jwks_cache["expires_at"]:
        response = httpx.get(JWKS_URL, timeout=5.0)
        response.raise_for_status()
        _jwks_cache["keys"] = response.json()
        _jwks_cache["expires_at"] = now + _JWKS_TTL
    return _jwks_cache["keys"]


def decode_token(token: str) -> dict:
    """Decode and verify a JWT token using Keycloak's public keys."""
    try:
        jwks = get_jwks()
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience="account",
            options={"verify_exp": True, "verify_iss": True}
        )
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_error("UNAUTHORIZED", f"Invalid token: {str(e)}"),
        )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """FastAPI dependency — accepts Bearer JWT or X-API-Key header."""
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return _user_from_api_key(api_key)
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_error("UNAUTHORIZED", "Authorization header or X-API-Key required"),
        )
    return decode_token(credentials.credentials)


# Role hierarchy — each role includes the permissions of the roles listed in its set.
#
# DEVICE       → POST /v1/data/batch only (medical device sending raw data, no UI access)
# COLLABORATOR → GET only (clinical staff, read-only access to their own project)
# RESEARCHER   → GET + POST (includes COLLABORATOR and DEVICE permissions)
# PROJECT_ADMIN → GET + POST + DELETE within their assigned project(s) only
#                 (manage users, batches, and files — scoped to their project)
# GLOBAL_ADMIN  → full access across all projects (create/delete projects,
#                 manage all users, no project scope restriction)
ROLE_HIERARCHY = {
    Role.DEVICE: {Role.DEVICE},
    Role.COLLABORATOR: {Role.COLLABORATOR},
    Role.RESEARCHER: {Role.RESEARCHER, Role.COLLABORATOR, Role.DEVICE},
    Role.PROJECT_ADMIN: {Role.PROJECT_ADMIN, Role.RESEARCHER, Role.COLLABORATOR, Role.DEVICE},
    Role.GLOBAL_ADMIN: {Role.GLOBAL_ADMIN, Role.PROJECT_ADMIN, Role.RESEARCHER, Role.COLLABORATOR, Role.DEVICE},
}


def require_role(required_role: Role):
    """FastAPI dependency factory — checks if the user has a role that includes the required role."""
    def role_checker(user: dict = Depends(get_current_user)) -> dict:
        client_roles = set(
            user.get("resource_access", {})
                .get(KEYCLOAK_CLIENT, {})
                .get("roles", [])
        )
        valid_role_values = {r.value for r in Role}
        has_access = any(
            required_role in ROLE_HIERARCHY.get(Role(r), set())
            for r in client_roles
            if r in valid_role_values
        )
        if not has_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=api_error("FORBIDDEN", f"Role '{required_role.value}' or higher required"),
            )
        return user
    return role_checker


# Convenience dependencies
require_collaborator = require_role(Role.COLLABORATOR)
require_researcher = require_role(Role.RESEARCHER)
require_project_admin = require_role(Role.PROJECT_ADMIN)
require_admin = require_role(Role.PROJECT_ADMIN)
require_global_admin = require_role(Role.GLOBAL_ADMIN)
require_device = require_role(Role.DEVICE)
require_read = require_collaborator
require_read_write = require_researcher

# Backwards compatibility aliases
require_read = require_collaborator
require_read_write = require_researcher


KEYCLOAK_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
TOKEN_URL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"


def refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token. Internal use only."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": KEYCLOAK_CLIENT,
            "client_secret": KEYCLOAK_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
    )
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_error("UNAUTHORIZED", "Refresh token is invalid or expired"),
        )
    return response.json()
