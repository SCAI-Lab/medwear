import logging
import logging.config
import uvicorn
from fastapi import FastAPI, Depends, Request
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from fastapi.middleware.gzip import GZipMiddleware
import asyncio
import os
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from medwear_med_api.models import MeasurementBatch
from sqlmodel import select
from influxdb_client import InfluxDBClient

from urllib.parse import quote_plus as _url_quote

# --- Credentials (fail fast if missing) ---
_PG_USER = os.getenv("POSTGRES_USER", "")
_PG_PASS = os.getenv("POSTGRES_PASSWORD", "")
if not _PG_USER or not _PG_PASS:
    raise RuntimeError("POSTGRES_USER / POSTGRES_PASSWORD env variables are not set.")
_PG_HOST = os.getenv("POSTGRES_HOST", "medwear-postgres")
_PG_PORT = os.getenv("POSTGRES_PORT", "5432")

_INFLUX_URL    = os.getenv("INFLUX_URL",    "http://medwear-influxdb:8086")
_INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "")
_INFLUX_ORG    = os.getenv("INFLUX_ORG",    "medwear")
_INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "medwear")
if not _INFLUX_TOKEN:
    raise RuntimeError("INFLUX_TOKEN env variable is not set.")

_MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT", "medwear-minio:9000")
_MINIO_ACCESS_KEY = os.getenv("MINIO_API_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER", "")
_MINIO_SECRET_KEY = os.getenv("MINIO_API_SECRET_KEY") or os.getenv("MINIO_ROOT_PASSWORD", "")
if not _MINIO_ACCESS_KEY or not _MINIO_SECRET_KEY:
    raise RuntimeError("MINIO credentials (MINIO_API_ACCESS_KEY / MINIO_API_SECRET_KEY) are not set.")

_KC_URL        = os.getenv("KEYCLOAK_URL",           "http://medwear-keycloak:8080")
_KC_REALM      = os.getenv("KEYCLOAK_REALM",         "medwear")
_KC_ADMIN_USER = os.getenv("KEYCLOAK_ADMIN",         "")
_KC_ADMIN_PASS = os.getenv("KEYCLOAK_ADMIN_PASSWORD","")

_engine = create_async_engine(os.getenv("DATABASE_URL", f"postgresql+asyncpg://{_PG_USER}:{_url_quote(_PG_PASS, safe='')}@{_PG_HOST}:{_PG_PORT}/medwear_project1"))
AsyncSessionLocal = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
from fastapi.middleware.cors import CORSMiddleware
from medwear_med_api.auth import get_current_user, require_read_write
from medwear_med_api.audit import audit_middleware
from medwear_med_api.batch_router import router as batch_router
from medwear_med_api.query_router import router as query_router
from medwear_med_api.routers.totp import router as totp_router

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

from medwear_med_api.logging_config import get_logging_config
logging.config.dictConfig(get_logging_config(_LOG_LEVEL))
logger = logging.getLogger(__name__)


async def _purge_expired_batches():
    from medwear_med_api.db import get_all_projects, get_project_session

    try:
        dynamic_projects = await get_all_projects()
    except Exception as e:
        logger.error(f"Purge: failed to get projects: {e}")
        return

    for proj in dynamic_projects:
        try:
            from sqlalchemy import text as _text

            def _influx_delete(uuid_val):
                try:
                    influx = InfluxDBClient(url=_INFLUX_URL, token=_INFLUX_TOKEN, org=_INFLUX_ORG)
                    influx.delete_api().delete(
                        start="1970-01-01T00:00:00Z",
                        stop="2100-01-01T00:00:00Z",
                        predicate=f'batch_uuid="{uuid_val}"',
                        bucket=_INFLUX_BUCKET,
                        org=_INFLUX_ORG,
                    )
                    influx.close()
                except Exception as e:
                    logger.warning(f"Purge: InfluxDB delete failed for {uuid_val}: {e}")

            async with get_project_session(proj) as session:
                # 1. Manual soft-deleted batches older than 24h
                result = await session.execute(
                    _text("SELECT uuid FROM measurement_batch WHERE is_deleted = true AND deleted_at IS NOT NULL AND deleted_at <= NOW() - INTERVAL '24 hours'")
                )
                uuids = [row[0] for row in result.fetchall()]
                for uuid_val in uuids:
                    _influx_delete(uuid_val)
                if uuids:
                    await session.execute(
                        _text("DELETE FROM measurement_batch WHERE is_deleted = true AND deleted_at IS NOT NULL AND deleted_at <= NOW() - INTERVAL '24 hours'")
                    )
                    await session.commit()
                    logger.info(f"Purge: hard-deleted {len(uuids)} manually-deleted batches from {proj}")

                # 2. Retention policy — soft-delete batches older than retention_days
                # The existing 24h hard-delete above will clean them up on the next run.
                ret_row = await session.execute(
                    _text("SELECT retention_days FROM project_settings WHERE project_id = :pid"),
                    {"pid": proj},
                )
                ret = ret_row.fetchone()
                if ret and ret[0]:
                    retention_days = ret[0]
                    result2 = await session.execute(
                        _text(f"SELECT uuid FROM measurement_batch WHERE is_deleted != true AND created_at <= NOW() - INTERVAL '{retention_days} days'")
                    )
                    expired = [row[0] for row in result2.fetchall()]
                    if expired:
                        await session.execute(
                            _text(f"UPDATE measurement_batch SET is_deleted = true, deleted_at = NOW() WHERE is_deleted != true AND created_at <= NOW() - INTERVAL '{retention_days} days'")
                        )
                        await session.commit()
                        logger.info(f"Purge: soft-deleted {len(expired)} retention-expired batches from {proj} (>{retention_days} days old)")
        except Exception as e:
            logger.error(f"Purge: failed for {proj}: {e}")


async def _purge_expired_files():
    from medwear_med_api.db import get_all_projects, get_project_session
    from minio import Minio
    from minio.error import S3Error

    MINIO_BUCKET = "medwear-bronze"
    minio_client = Minio(
        _MINIO_ENDPOINT,
        access_key=_MINIO_ACCESS_KEY,
        secret_key=_MINIO_SECRET_KEY,
        secure=False,
    )

    try:
        dynamic_projects = await get_all_projects()
    except Exception as e:
        logger.error(f"Purge files: failed to get projects: {e}")
        return

    for proj in dynamic_projects:
        try:
            from sqlalchemy import text as _text
            async with get_project_session(proj) as session:
                result = await session.execute(
                    _text("SELECT id, filename FROM deleted_files WHERE deleted_at <= NOW() - INTERVAL '24 hours'")
                )
                expired = result.fetchall()
                if not expired:
                    continue
                for row in expired:
                    try:
                        minio_client.remove_object(MINIO_BUCKET, row[1])
                    except S3Error as e:
                        if "NoSuchKey" not in str(e):
                            logger.warning(f"Purge files: MinIO delete failed for {row[1]}: {e}")
                ids = [r[0] for r in expired]
                await session.execute(
                    _text("DELETE FROM deleted_files WHERE id = ANY(:ids)"),
                    {"ids": ids},
                )
                await session.commit()
                logger.info(f"Purge files: hard-deleted {len(expired)} files from {proj}")
        except Exception as e:
            logger.error(f"Purge files: failed for {proj}: {e}")


async def _purge_expired_projects():
    import asyncpg
    from minio import Minio
    from minio.error import S3Error
    MINIO_BUCKET = "medwear-bronze"
    try:
        conn = await asyncpg.connect(
            host=_PG_HOST, port=int(_PG_PORT),
            user=_PG_USER, password=_PG_PASS, database="postgres"
        )
        rows = await conn.fetch(
            "SELECT project_name FROM deleted_projects WHERE deleted_at <= NOW() - INTERVAL '24 hours'"
        )
        minio_client = Minio(
            _MINIO_ENDPOINT,
            access_key=_MINIO_ACCESS_KEY,
            secret_key=_MINIO_SECRET_KEY,
            secure=False,
        )
        for row in rows:
            proj = row["project_name"]
            # Soft-delete all users still in this project before dropping the DB
            try:
                import httpx as _httpx
                kc_token_resp = _httpx.post(
                    f"{_KC_URL}/realms/master/protocol/openid-connect/token",
                    data={"grant_type": "password", "client_id": "admin-cli",
                          "username": _KC_ADMIN_USER,
                          "password": _KC_ADMIN_PASS}
                )
                kc_token = kc_token_resp.json().get("access_token")
                kc_headers = {"Authorization": f"Bearer {kc_token}"} if kc_token else {}

                proj_conn = await asyncpg.connect(
                    host=_PG_HOST, port=int(_PG_PORT),
                    user=_PG_USER, password=_PG_PASS, database=proj
                )
                proj_users = await proj_conn.fetch("SELECT username, role FROM user_project WHERE project_id=$1", proj)
                await proj_conn.close()
                for pu in proj_users:
                    uname, urole = pu["username"], pu["role"]
                    # Mark as soft-deleted so purge_users will clean up KC after 24h
                    await conn.execute(
                        "INSERT INTO deleted_users (username, project_id, role, deleted_by) VALUES ($1,$2,$3,$4) "
                        "ON CONFLICT (username, project_id) DO NOTHING",
                        uname, proj, urole, "system_project_purge"
                    )
                    logger.info(f"Purge projects: soft-deleted user {uname} from {proj}")
            except Exception as e:
                logger.warning(f"Purge projects: user soft-delete failed for {proj}: {e}")

            try:
                # Dispose SQLAlchemy engine pool for this project first
                try:
                    from medwear_med_api.db import dispose_project_engine
                    await dispose_project_engine(proj)
                except Exception as _e:
                    logger.warning(f"Purge projects: dispose engine failed for {proj}: {_e}")
                await conn.execute(f"""
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = '{proj}' AND pid <> pg_backend_pid()
                """)
                await conn.execute(f'DROP DATABASE IF EXISTS "{proj}"')
                await conn.execute("DELETE FROM deleted_projects WHERE project_name=$1", proj)
                logger.info(f"Purge projects: hard-deleted PostgreSQL DB {proj}")
            except Exception as e:
                logger.error(f"Purge projects: failed to drop {proj}: {e}")
            # Delete Grafana datasource for the project
            try:
                from medwear_med_api.grafana_utils import delete_datasource
                await delete_datasource(proj)
            except Exception as e:
                logger.warning(f"Purge projects: Grafana datasource delete failed for {proj}: {e}")
            # Delete MinIO folder for the project
            try:
                objects = minio_client.list_objects(MINIO_BUCKET, prefix=f"{proj}/", recursive=True)
                for obj in objects:
                    try:
                        minio_client.remove_object(MINIO_BUCKET, obj.object_name)
                    except S3Error as e:
                        if "NoSuchKey" not in str(e):
                            logger.warning(f"Purge projects: MinIO delete failed for {obj.object_name}: {e}")
                logger.info(f"Purge projects: deleted MinIO folder {proj}/")
            except Exception as e:
                logger.warning(f"Purge projects: MinIO cleanup failed for {proj}: {e}")
        await conn.close()
    except Exception as e:
        logger.error(f"Purge projects: {e}")


async def _purge_expired_users():
    import asyncpg
    try:
        conn = await asyncpg.connect(
            host=_PG_HOST, port=int(_PG_PORT),
            user=_PG_USER, password=_PG_PASS, database="postgres"
        )
        rows = await conn.fetch(
            "SELECT username, project_id FROM deleted_users WHERE deleted_at <= NOW() - INTERVAL '24 hours'"
        )
        if not rows:
            await conn.close()
            return

        import httpx as _httpx
        try:
            token_resp = _httpx.post(
                f"{_KC_URL}/realms/master/protocol/openid-connect/token",
                data={"grant_type": "password", "client_id": "admin-cli",
                      "username": _KC_ADMIN_USER,
                      "password": _KC_ADMIN_PASS}
            )
            admin_token = token_resp.json()["access_token"]
            headers_kc = {"Authorization": f"Bearer {admin_token}"}
        except Exception as e:
            logger.error(f"Purge users: failed to get Keycloak admin token: {e}")
            await conn.close()
            return

        for row in rows:
            username, project_id = row["username"], row["project_id"]
            try:
                # Check if user still has active mappings in other projects
                still_active = await conn.fetchval(
                    "SELECT COUNT(*) FROM deleted_users WHERE username=$1", username
                )
                # Only delete from Keycloak if no other active project_mappings exist
                # (active = in user_project tables, which we check across all projects)
                from medwear_med_api.db import get_all_projects
                active_projects = await get_all_projects()
                has_active_mapping = False
                for proj in active_projects:
                    try:
                        proj_conn = await asyncpg.connect(
                            host=_PG_HOST, port=int(_PG_PORT),
                            user=_PG_USER, password=_PG_PASS, database=proj
                        )
                        cnt = await proj_conn.fetchval("SELECT COUNT(*) FROM user_project WHERE username=$1", username)
                        await proj_conn.close()
                        if cnt and cnt > 0:
                            has_active_mapping = True
                            break
                    except Exception:
                        continue

                if not has_active_mapping:
                    try:
                        import httpx as _httpx
                        users_resp = _httpx.get(
                            f"{KC_URL}/admin/realms/{KC_REALM}/users?username={username}&exact=true",
                            headers=headers_kc
                        )
                        kc_users = users_resp.json()
                        if kc_users:
                            user_kc_id = kc_users[0]["id"]
                            del_resp = _httpx.delete(
                                f"{KC_URL}/admin/realms/{KC_REALM}/users/{user_kc_id}",
                                headers=headers_kc
                            )
                            logger.info(f"Purge users: Keycloak delete {username}: {del_resp.status_code}")
                        else:
                            logger.warning(f"Purge users: {username} not found in Keycloak")
                    except Exception as e:
                        logger.error(f"Purge users: Keycloak delete failed for {username}: {e}")

                await conn.execute(
                    "DELETE FROM deleted_users WHERE username=$1 AND project_id=$2", username, project_id
                )
                logger.info(f"Purge users: hard-deleted {username} from {project_id}")
            except Exception as e:
                logger.error(f"Purge users: failed for {username}/{project_id}: {e}")

        await conn.close()
    except Exception as e:
        logger.error(f"Purge users: {e}")


async def _purge_loop():
    await asyncio.sleep(60)
    while True:
        logger.info("Purge: starting scheduled purge run")
        try:
            await _purge_expired_batches()
        except Exception as e:
            logger.error(f"Purge: _purge_expired_batches crashed: {e}")
        try:
            await _purge_expired_files()
        except Exception as e:
            logger.error(f"Purge: _purge_expired_files crashed: {e}")
        try:
            await _purge_expired_projects()
        except Exception as e:
            logger.error(f"Purge: _purge_expired_projects crashed: {e}")
        try:
            await _purge_expired_users()
        except Exception as e:
            logger.error(f"Purge: _purge_expired_users crashed: {e}")
        logger.info("Purge: run complete")
        await asyncio.sleep(3600)


from medwear_med_api.rate_limit import limiter
from medwear_med_api.metrics import init_metrics
from medwear_med_api.tracing import init_tracing

app = FastAPI(
    title="MED-API",
    description="MEDWEAR standardized wearable sensor data API",
    version="0.1.0",
)

async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    from fastapi.responses import JSONResponse
    from medwear_med_api.errors import api_error
    return JSONResponse(
        status_code=429,
        content={"detail": api_error("RATE_LIMIT_EXCEEDED", f"Rate limit exceeded: {exc.detail}")},
    )

async def _reformat_429_middleware(request: Request, call_next):
    import json as _json
    from starlette.responses import Response as _Resp
    from medwear_med_api.errors import api_error
    response = await call_next(request)
    if response.status_code == 429:
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        try:
            existing = _json.loads(body)
            inner = existing.get("detail", existing)
            already_ok = (
                isinstance(inner, dict)
                and "error" in inner
                and "detail" in inner
                and "timestamp" in inner
            )
            if already_ok:
                return _Resp(content=body, status_code=429, media_type="application/json")
        except Exception:
            pass
        raw_msg = body.decode(errors="replace")
        try:
            raw_msg = _json.loads(body).get("error", raw_msg)
        except Exception:
            pass
        content = _json.dumps({"detail": api_error("RATE_LIMIT_EXCEEDED", str(raw_msg))})
        return _Resp(content=content.encode(), status_code=429, media_type="application/json")
    return response

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
_cors_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    if _cors_origins_env
    else ["https://localhost", "http://localhost:3002", "http://localhost:8080"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
async def _security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-API-Version"] = "1.0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


async def _request_id_middleware(request: Request, call_next):
    import uuid as _uuid
    from medwear_med_api.logging_config import set_trace_id
    request_id = request.headers.get("X-Request-ID") or str(_uuid.uuid4())
    request.state.request_id = request_id
    set_trace_id(request_id)          # inject into every log line for this request
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


app.middleware("http")(audit_middleware)
app.middleware("http")(_reformat_429_middleware)
app.middleware("http")(_security_headers_middleware)
app.middleware("http")(_request_id_middleware)

init_metrics(app)      # expose /metrics (Prometheus)
init_tracing(app)      # OTel tracing (no-op unless OTEL_TRACES_EXPORTER is set)

app.include_router(batch_router, prefix="/v1")
app.include_router(query_router)
app.include_router(totp_router, prefix="/v1")


async def _migrate_deleted_files_table():
    from medwear_med_api.db import get_all_projects, ensure_deleted_files_table
    try:
        projects = await get_all_projects()
        for proj in projects:
            await ensure_deleted_files_table(proj)
        logger.info(f"Migration: ensured deleted_files table in {len(projects)} projects")
    except Exception as e:
        logger.warning(f"Migration: deleted_files table migration failed (non-fatal): {e}")


async def _migrate_deleted_users_table():
    import asyncpg
    try:
        conn = await asyncpg.connect(
            host=_PG_HOST, port=int(_PG_PORT),
            user=_PG_USER, password=_PG_PASS, database="postgres"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS deleted_users (
                id SERIAL PRIMARY KEY,
                username VARCHAR NOT NULL,
                project_id VARCHAR NOT NULL,
                role VARCHAR NOT NULL DEFAULT 'collaborator',
                deleted_by VARCHAR NOT NULL DEFAULT 'system',
                deleted_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(username, project_id)
            )
        """)
        await conn.close()
        logger.info("Migration: ensured deleted_users table in postgres")
    except Exception as e:
        logger.warning(f"Migration: deleted_users table migration failed (non-fatal): {e}")


async def _migrate_ingest_schema():
    from medwear_med_api.db import get_all_projects, ensure_ingest_schema
    for attempt in range(30):   # PostgreSQL may still be starting
        try:
            projects = await get_all_projects()
            for proj in projects:
                await ensure_ingest_schema(proj)
            logger.info(f"Migration: ensured ingest_status/ingest_session in {len(projects)} projects")
            return
        except Exception as e:
            logger.warning(f"Migration: ingest schema attempt {attempt + 1}/30 failed: {e}")
            await asyncio.sleep(2)
    raise RuntimeError("ingest schema migration failed; refusing to start")


@app.on_event("startup")
async def startup_event():
    # Awaited, unlike the migrations below: ingest writes ingest_status.
    await _migrate_ingest_schema()
    asyncio.create_task(_migrate_deleted_files_table())
    asyncio.create_task(_migrate_deleted_users_table())
    asyncio.create_task(_purge_loop())
    asyncio.create_task(_startup_grafana_sync())
    asyncio.create_task(_grafana_sync_loop())


async def _grafana_sync_once():
    try:
        from medwear_med_api.db import get_all_projects
        from medwear_med_api.grafana_utils import sync_all_datasources
        projects = await get_all_projects()
        await sync_all_datasources(projects)
        logger.info(f"Grafana sync complete: {projects}")
    except Exception as e:
        logger.warning(f"Grafana sync failed (non-fatal): {e}")


async def _startup_grafana_sync():
    await asyncio.sleep(15)
    await _grafana_sync_once()


async def _grafana_sync_loop():
    await asyncio.sleep(60)
    while True:
        await _grafana_sync_once()
        await asyncio.sleep(3600)

@app.get("/health")
@app.get("/v1/health")
async def health():
    import time as _time
    import httpx
    from influxdb_client import InfluxDBClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from minio import Minio
    from urllib.parse import quote_plus as _q
    from fastapi.responses import JSONResponse
    from medwear_med_api.circuit_breaker import CircuitOpenError
    from medwear_med_api.influx_writer import _influx_cb, INFLUX_BUCKET

    results: dict = {"status": "ok", "services": {}}

    def _svc(label: str, ok: bool, latency_ms: float, detail: str = "") -> None:
        results["services"][label] = {
            "status":     "ok" if ok else "error",
            "latency_ms": round(latency_ms, 1),
            **({"detail": detail} if detail else {}),
        }
        if not ok:
            results["status"] = "degraded"

    # --- PostgreSQL: connect to real project DB, not just system DB ---
    t0 = _time.monotonic()
    try:
        from medwear_med_api.db import get_all_projects
        projects = await get_all_projects()
        target_db = projects[0] if projects else "postgres"
        engine = create_async_engine(
            f"postgresql+asyncpg://{_PG_USER}:{_q(_PG_PASS, safe='')}@{_PG_HOST}:{_PG_PORT}/{target_db}",
            echo=False,
        )
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        _svc("postgresql", True, (_time.monotonic() - t0) * 1000)
    except Exception as e:
        _svc("postgresql", False, (_time.monotonic() - t0) * 1000, str(e)[:80])

    # --- InfluxDB: ping + verify bucket exists ---
    t0 = _time.monotonic()
    try:
        client = InfluxDBClient(url=_INFLUX_URL, token=_INFLUX_TOKEN, org=_INFLUX_ORG, timeout=5_000)
        ok = client.ping()
        client.close()
        _svc("influxdb", ok, (_time.monotonic() - t0) * 1000)
    except Exception as e:
        _svc("influxdb", False, (_time.monotonic() - t0) * 1000, str(e)[:80])

    # --- InfluxDB circuit breaker state ---
    cb_state = _influx_cb.state
    results["circuit_breaker"] = {
        "influxdb": cb_state,
        **({"warning": "InfluxDB circuit is OPEN — writes are failing fast"} if cb_state == "open" else {}),
    }
    if cb_state == "open":
        results["status"] = "degraded"

    # --- MinIO ---
    t0 = _time.monotonic()
    try:
        minio_client = Minio(
            _MINIO_ENDPOINT,
            access_key=_MINIO_ACCESS_KEY,
            secret_key=_MINIO_SECRET_KEY,
            secure=False,
        )
        minio_client.list_buckets()
        _svc("minio", True, (_time.monotonic() - t0) * 1000)
    except Exception as e:
        _svc("minio", False, (_time.monotonic() - t0) * 1000, str(e)[:80])

    # --- Keycloak ---
    t0 = _time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as hx:
            r = await hx.get(f"{_KC_URL}/realms/{_KC_REALM}")
        ok = r.status_code == 200
        _svc("keycloak", ok, (_time.monotonic() - t0) * 1000,
             "" if ok else f"HTTP {r.status_code}")
    except Exception as e:
        _svc("keycloak", False, (_time.monotonic() - t0) * 1000, str(e)[:80])

    status_code = 200 if results["status"] == "ok" else 503
    return JSONResponse(content=results, status_code=status_code)

@app.get("/")
def root():
    return {"message": "MED-API is running"}

@app.get("/me", dependencies=[Depends(get_current_user)])
def me(user: dict = Depends(get_current_user)):
    return {
        "username": user.get("preferred_username"),
        "roles": user.get("resource_access", {}).get("med-api", {}).get("roles", []),
        "email": user.get("email"),
    }

@app.get("/protected", dependencies=[Depends(require_read_write)])
def protected(user: dict = Depends(require_read_write)):
    return {"message": f"Hello {user.get('preferred_username')}! You have read/write access."}

if __name__ == "__main__":
    uvicorn.run("run_med_api:app", host="0.0.0.0", port=8080, reload=False)
