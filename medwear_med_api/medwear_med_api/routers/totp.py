import base64
import io
import logging
from typing import Any, Dict

import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as _text

from medwear_med_api.auth import require_read
from medwear_med_api.db import get_all_projects, get_project_session
from medwear_med_api.errors import api_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/2fa", tags=["2FA"])

APP_NAME = "MedWear"


async def _get_totp_project(username: str) -> str | None:
    projects = await get_all_projects()
    for proj in projects:
        try:
            async with get_project_session(proj) as session:
                row = await session.execute(
                    _text("SELECT project_id FROM user_project WHERE username=:u LIMIT 1"),
                    {"u": username},
                )
                r = row.fetchone()
                if r:
                    return proj
        except Exception as e:
            logger.warning(f"Failed to query user_project in {proj} for user {username}: {e}")
            continue
    return projects[0] if projects else None


async def _ensure_totp_table(project_id: str):
    async with get_project_session(project_id) as session:
        await session.execute(_text("""
            CREATE TABLE IF NOT EXISTS user_totp (
                username    TEXT PRIMARY KEY,
                secret      TEXT NOT NULL,
                enabled     BOOLEAN NOT NULL DEFAULT FALSE,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """))
        await session.commit()


class VerifyRequest(BaseModel):
    code: str


@router.get("/status", summary="Check if 2FA is enabled for the current user")
async def get_status(user: Dict[str, Any] = Depends(require_read)):
    username = user.get("preferred_username", "")
    proj = await _get_totp_project(username)
    if not proj:
        return {"enabled": False}
    await _ensure_totp_table(proj)
    async with get_project_session(proj) as session:
        row = await session.execute(
            _text("SELECT enabled FROM user_totp WHERE username=:u"), {"u": username}
        )
        r = row.fetchone()
    return {"enabled": bool(r and r[0])}


@router.post("/setup", summary="Generate a new TOTP secret and return QR code")
async def setup_2fa(user: Dict[str, Any] = Depends(require_read)):
    username = user.get("preferred_username", "")
    proj = await _get_totp_project(username)
    if not proj:
        raise HTTPException(status_code=403, detail=api_error("FORBIDDEN", "No project found for user"))
    await _ensure_totp_table(proj)

    secret = pyotp.random_base32()
    totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=APP_NAME)

    img = qrcode.make(totp_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    async with get_project_session(proj) as session:
        await session.execute(_text("""
            INSERT INTO user_totp (username, secret, enabled)
            VALUES (:u, :s, FALSE)
            ON CONFLICT (username) DO UPDATE SET secret = :s, enabled = FALSE
        """), {"u": username, "s": secret})
        await session.commit()

    return {
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_b64}",
        "message": "Scan the QR code with Google Authenticator, then call /verify to activate.",
    }


@router.post("/enable", summary="Verify TOTP code and enable 2FA")
async def enable_2fa(body: VerifyRequest, user: Dict[str, Any] = Depends(require_read)):
    username = user.get("preferred_username", "")
    proj = await _get_totp_project(username)
    if not proj:
        raise HTTPException(status_code=403, detail=api_error("FORBIDDEN", "No project found for user"))

    async with get_project_session(proj) as session:
        row = await session.execute(
            _text("SELECT secret FROM user_totp WHERE username=:u"), {"u": username}
        )
        r = row.fetchone()

    if not r:
        raise HTTPException(status_code=400, detail=api_error("NOT_SETUP", "Call /setup first"))

    totp = pyotp.TOTP(r[0])
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail=api_error("INVALID_CODE", "Incorrect TOTP code"))

    async with get_project_session(proj) as session:
        await session.execute(
            _text("UPDATE user_totp SET enabled=TRUE WHERE username=:u"), {"u": username}
        )
        await session.commit()

    return {"enabled": True, "message": "2FA enabled successfully"}


@router.post("/disable", summary="Disable 2FA for the current user")
async def disable_2fa(body: VerifyRequest, user: Dict[str, Any] = Depends(require_read)):
    username = user.get("preferred_username", "")
    proj = await _get_totp_project(username)
    if not proj:
        raise HTTPException(status_code=403, detail=api_error("FORBIDDEN", "No project found for user"))

    async with get_project_session(proj) as session:
        row = await session.execute(
            _text("SELECT secret FROM user_totp WHERE username=:u AND enabled=TRUE"), {"u": username}
        )
        r = row.fetchone()

    if not r:
        raise HTTPException(status_code=400, detail=api_error("NOT_ENABLED", "2FA is not enabled"))

    totp = pyotp.TOTP(r[0])
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail=api_error("INVALID_CODE", "Incorrect TOTP code"))

    async with get_project_session(proj) as session:
        await session.execute(
            _text("UPDATE user_totp SET enabled=FALSE WHERE username=:u"), {"u": username}
        )
        await session.commit()

    return {"enabled": False, "message": "2FA disabled"}


@router.post("/verify", summary="Verify a TOTP code (called after login if 2FA is enabled)")
async def verify_2fa(body: VerifyRequest, user: Dict[str, Any] = Depends(require_read)):
    username = user.get("preferred_username", "")
    proj = await _get_totp_project(username)
    if not proj:
        raise HTTPException(status_code=403, detail=api_error("FORBIDDEN", "No project found for user"))

    async with get_project_session(proj) as session:
        row = await session.execute(
            _text("SELECT secret, enabled FROM user_totp WHERE username=:u"), {"u": username}
        )
        r = row.fetchone()

    if not r or not r[1]:
        return {"verified": True, "message": "2FA not enabled for this user"}

    totp = pyotp.TOTP(r[0])
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail=api_error("INVALID_CODE", "Incorrect TOTP code"))

    return {"verified": True}
