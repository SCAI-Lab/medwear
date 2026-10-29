import logging
import os
import re
from contextlib import asynccontextmanager
from typing import List
from urllib.parse import quote_plus as _quote

import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

_PROJECT_NAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{0,62}$')
logger = logging.getLogger(__name__)


def _validate_project_name(name: str) -> None:
    if not _PROJECT_NAME_RE.match(name):
        raise ValueError(
            f"Invalid project name {name!r}: must start with a letter "
            "and contain only letters, digits, or underscores (max 63 chars)")


PG_USER = os.getenv("POSTGRES_USER", "")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "")
PG_HOST = os.getenv("POSTGRES_HOST", "medwear-postgres")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))

if not PG_USER or not PG_PASS:
    raise RuntimeError(
        "PostgreSQL credentials (POSTGRES_USER / POSTGRES_PASSWORD) are not set. Cannot start without them.")
BASE_URL = f"postgresql+asyncpg://{PG_USER}:{_quote(PG_PASS, safe='')}@{PG_HOST}:{PG_PORT}"

_engines = {}
_sessions = {}
_projects = ["medwear_project1", "medwear_project2", "medwear_project3"]


@asynccontextmanager
async def pg_connection(database: str = "postgres"):
    conn: asyncpg.Connection = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, database=database
    )
    try:
        yield conn
    finally:
        await conn.close()


async def get_all_projects() -> List:
    try:
        async with pg_connection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS deleted_projects (
                    id SERIAL PRIMARY KEY,
                    project_name VARCHAR NOT NULL UNIQUE,
                    deleted_by VARCHAR NOT NULL,
                    deleted_at TIMESTAMP DEFAULT NOW()
                )
            """)
            rows = await conn.fetch(
                "SELECT datname FROM pg_database WHERE datname LIKE 'medwear_%' "
                "AND datname != 'medwear_processed' "
                "AND datname NOT IN (SELECT project_name FROM deleted_projects) ORDER BY datname"
            )
        return [r["datname"] for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch projects from database: {e}")
        return []


async def get_deleted_projects() -> List:
    async with pg_connection() as conn:
        rows = await conn.fetch(
            "SELECT project_name, deleted_by, deleted_at FROM deleted_projects ORDER BY deleted_at DESC"
        )
    return [
        {"project_name": r["project_name"], "deleted_by": r["deleted_by"],
         "deleted_at": r["deleted_at"].isoformat() + "Z"}
        for r in rows
    ]


async def soft_delete_project(project_name: str, deleted_by: str) -> None:
    async with pg_connection() as conn:
        await conn.execute(
            "INSERT INTO deleted_projects (project_name, deleted_by) VALUES ($1, $2) "
            "ON CONFLICT (project_name) DO NOTHING",
            project_name, deleted_by
        )


async def restore_deleted_project(project_name: str) -> None:
    async with pg_connection() as conn:
        await conn.execute("DELETE FROM deleted_projects WHERE project_name=$1", project_name)


async def _ensure_deleted_users_table(conn) -> None:
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


async def get_deleted_users(project_id: str = None) -> List:
    async with pg_connection() as conn:
        await _ensure_deleted_users_table(conn)
        if project_id:
            rows = await conn.fetch(
                "SELECT username, project_id, role, deleted_by, deleted_at "
                "FROM deleted_users WHERE project_id=$1 ORDER BY deleted_at DESC",
                project_id
            )
        else:
            rows = await conn.fetch(
                "SELECT username, project_id, role, deleted_by, deleted_at FROM deleted_users ORDER BY deleted_at DESC"
            )
    return [{"username": r["username"], "project_id": r["project_id"], "role": r["role"],
             "deleted_by": r["deleted_by"], "deleted_at": r["deleted_at"].isoformat() + "Z"} for r in rows]


async def soft_delete_user(username: str, project_id: str, role: str, deleted_by: str) -> None:
    async with pg_connection() as conn:
        await _ensure_deleted_users_table(conn)
        await conn.execute(
            "INSERT INTO deleted_users (username, project_id, role, deleted_by) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (username, project_id) DO UPDATE SET deleted_at=NOW(), deleted_by=$4",
            username, project_id, role, deleted_by
        )


async def restore_deleted_user(username: str, project_id: str) -> dict:
    async with pg_connection() as conn:
        await _ensure_deleted_users_table(conn)
        row = await conn.fetchrow(
            "SELECT role FROM deleted_users WHERE username=$1 AND project_id=$2",
            username, project_id
        )
        if not row:
            return None
        role = row["role"]
        await conn.execute("DELETE FROM deleted_users WHERE username=$1 AND project_id=$2", username, project_id)
    return {"role": role}


async def hard_delete_project(project_name: str) -> None:
    _validate_project_name(project_name)
    async with pg_connection() as conn:
        await conn.execute("DELETE FROM deleted_projects WHERE project_name=$1", project_name)
        await conn.execute(f'DROP DATABASE IF EXISTS "{project_name}"')


async def create_project_db(project_name: str) -> bool:
    _validate_project_name(project_name)
    try:
        async with pg_connection() as conn:
            await conn.execute(f'CREATE DATABASE "{project_name}" OWNER {PG_USER}')

        async with pg_connection(database=project_name) as proj_conn:
            await proj_conn.execute("""
                CREATE TABLE IF NOT EXISTS measurement_batch (
                    uuid VARCHAR PRIMARY KEY,
                    patient_id VARCHAR(255) NOT NULL,
                    device_id VARCHAR NOT NULL,
                    signal_type VARCHAR NOT NULL,
                    schema_id VARCHAR,
                    start_timestamp TIMESTAMP,
                    end_timestamp TIMESTAMP,
                    sampling_rate_hz FLOAT,
                    sample_count INTEGER,
                    units VARCHAR,
                    metadata_json JSONB,
                    project_id VARCHAR(100),
                    idempotency_key VARCHAR UNIQUE,
                    checksum VARCHAR(64),
                    created_at TIMESTAMP DEFAULT NOW(),
                    is_deleted BOOLEAN DEFAULT FALSE,
                    deleted_at TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR,
                    method VARCHAR,
                    path VARCHAR,
                    status_code INTEGER,
                    duration_ms FLOAT,
                    timestamp TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS user_project (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(255) NOT NULL,
                    project_id VARCHAR(100) NOT NULL,
                    role VARCHAR(50) NOT NULL DEFAULT 'collaborator',
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS deleted_files (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR NOT NULL,
                    username VARCHAR NOT NULL,
                    project_id VARCHAR(100),
                    deleted_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS project_settings (
                    project_id     VARCHAR(100) PRIMARY KEY,
                    retention_days INTEGER,
                    updated_at     TIMESTAMPTZ DEFAULT NOW(),
                    updated_by     VARCHAR(255) DEFAULT 'system'
                );
                CREATE TABLE IF NOT EXISTS user_totp (
                    username   VARCHAR(255) PRIMARY KEY,
                    secret     VARCHAR(255) NOT NULL,
                    enabled    BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        await ensure_ingest_schema(project_name)

        _projects.append(project_name)
        return True
    except Exception as e:
        raise Exception(f"Failed to create project {project_name}: {e}")


async def ensure_deleted_files_table(project_name: str) -> None:
    try:
        async with pg_connection(database=project_name) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS deleted_files (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR NOT NULL,
                    username VARCHAR NOT NULL,
                    project_id VARCHAR(100),
                    deleted_at TIMESTAMP DEFAULT NOW()
                )
            """)
            await conn.execute("""
                ALTER TABLE deleted_files ADD COLUMN IF NOT EXISTS project_id VARCHAR(100)
            """)
    except Exception as e:
        logger.warning(f"Failed to ensure deleted_files table: {e}")


async def ensure_ingest_schema(project_name: str) -> None:
    """Idempotent schema for two-phase ingest and sessions (alembic 006).
    Existing batches were written to both stores, so they default to complete."""
    async with pg_connection(database=project_name) as conn:
        await conn.execute("""
            ALTER TABLE measurement_batch
                ADD COLUMN IF NOT EXISTS ingest_status VARCHAR(16) NOT NULL DEFAULT 'complete';
            ALTER TABLE measurement_batch
                ADD COLUMN IF NOT EXISTS session_id VARCHAR(64);
            CREATE INDEX IF NOT EXISTS ix_measurement_batch_pending
                ON measurement_batch (ingest_status) WHERE ingest_status = 'pending';
            CREATE TABLE IF NOT EXISTS ingest_session (
                session_id       VARCHAR(64) PRIMARY KEY,
                patient_id       VARCHAR(255) NOT NULL,
                device_id        VARCHAR(255) NOT NULL,
                signal_type      VARCHAR(50) NOT NULL,
                schema_id        VARCHAR(100) NOT NULL,
                sampling_rate_hz FLOAT NOT NULL,
                units            VARCHAR(20) NOT NULL,
                header_json      JSONB,
                project_id       VARCHAR(100) NOT NULL,
                created_by       VARCHAR(255) NOT NULL,
                created_at       TIMESTAMP DEFAULT NOW()
            );
        """)


def get_engine(project_id: str):
    if project_id not in _engines:
        url = f"{BASE_URL}/{project_id}"
        _engines[project_id] = create_async_engine(url, pool_pre_ping=True)
    return _engines[project_id]


async def dispose_project_engine(project_id: str) -> None:
    if project_id in _engines:
        await _engines[project_id].dispose()
        del _engines[project_id]
    if project_id in _sessions:
        del _sessions[project_id]


def get_session_factory(project_id: str):
    if project_id not in _sessions:
        engine = get_engine(project_id)
        _sessions[project_id] = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return _sessions[project_id]


def get_project_session(project_id: str):
    return get_session_factory(project_id)()


ALL_PROJECTS = _projects
