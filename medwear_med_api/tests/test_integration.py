"""
Integration tests — requires test containers running:
  docker compose -f docker-compose.test.yml up -d

Run: cd medwear_med_api && pytest tests/test_integration.py -v

These tests bypass Keycloak (auth is mocked) and test the core DB + API logic.
"""
import os
import uuid
import datetime
import pytest
import asyncpg
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

# --- Environment stubs (must precede project imports) -----------------------
os.environ["POSTGRES_USER"]            = os.getenv("TEST_PG_USER", "test_user")
os.environ["POSTGRES_PASSWORD"]        = os.getenv("TEST_PG_PASS", "test_pass")
os.environ["POSTGRES_HOST"]            = os.getenv("TEST_PG_HOST", "localhost")
os.environ["POSTGRES_PORT"]            = os.getenv("POSTGRES_PORT", os.getenv("TEST_PG_PORT", "5432"))
os.environ["INFLUX_TOKEN"]             = "test-influx-token"
os.environ["INFLUX_URL"]               = f"http://localhost:{os.getenv('TEST_INFLUX_PORT', os.getenv('INFLUX_PORT', '8086'))}"
os.environ["INFLUX_BUCKET"]            = "wearables"
os.environ["MINIO_API_ACCESS_KEY"]     = "test-access"
os.environ["MINIO_API_SECRET_KEY"]     = "test-secret"
os.environ["KEYCLOAK_URL"]             = "http://localhost:8080"
os.environ["KEYCLOAK_REALM"]           = "medwear"
os.environ["KEYCLOAK_CLIENT_ID"]       = "med-api"
os.environ["KEYCLOAK_CLIENT_SECRET"]   = "test-secret"
os.environ["KEYCLOAK_ADMIN"]           = "admin"
os.environ["KEYCLOAK_ADMIN_PASSWORD"]  = "admin-pass"

TEST_PG_HOST = os.environ["POSTGRES_HOST"]
TEST_PG_PORT = int(os.environ["POSTGRES_PORT"])
TEST_PG_USER = os.environ["POSTGRES_USER"]
TEST_PG_PASS = os.environ["POSTGRES_PASSWORD"]
TEST_PG_DB   = "medwear_project1"

# Fake decoded JWT payload — used to mock auth
FAKE_RESEARCHER = {
    "preferred_username": "test_researcher",
    "resource_access": {
        "med-api": {"roles": ["researcher"]}
    },
    "sub": "test-sub-uuid",
}
FAKE_GLOBAL_ADMIN = {
    "preferred_username": "test_admin",
    "resource_access": {
        "med-api": {"roles": ["global_admin"]}
    },
    "sub": "test-admin-sub",
}


# ---------------------------------------------------------------------------
# DB Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Create project DB and tables once for the whole test session."""
    # Connect to postgres system DB to create the project DB
    conn = await asyncpg.connect(
        host=TEST_PG_HOST, port=TEST_PG_PORT,
        user=TEST_PG_USER, password=TEST_PG_PASS,
        database="postgres",
    )
    await conn.execute(
        f'CREATE DATABASE "{TEST_PG_DB}" OWNER {TEST_PG_USER}'
    ) if not await conn.fetchval(
        "SELECT 1 FROM pg_database WHERE datname=$1", TEST_PG_DB
    ) else None
    await conn.close()

    # Create tables
    proj_conn = await asyncpg.connect(
        host=TEST_PG_HOST, port=TEST_PG_PORT,
        user=TEST_PG_USER, password=TEST_PG_PASS,
        database=TEST_PG_DB,
    )
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
        CREATE TABLE IF NOT EXISTS project_settings (
            project_id VARCHAR(100) PRIMARY KEY,
            retention_days INTEGER,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_by VARCHAR(255) NOT NULL DEFAULT 'system'
        );
    """)
    await proj_conn.close()
    yield


@pytest.fixture
async def db_conn():
    conn = await asyncpg.connect(
        host=TEST_PG_HOST, port=TEST_PG_PORT,
        user=TEST_PG_USER, password=TEST_PG_PASS,
        database=TEST_PG_DB,
    )
    yield conn
    await conn.execute("DELETE FROM measurement_batch")
    await conn.execute("DELETE FROM audit_log")
    await conn.execute("DELETE FROM user_project")
    await conn.execute("DELETE FROM project_settings")
    await conn.close()


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_app():
    """Build a minimal FastAPI app with real DB but mocked auth + InfluxDB."""
    from fastapi import FastAPI
    from medwear_med_api.batch_router import router as batch_router
    from medwear_med_api.routers.batches import router as batches_router

    app = FastAPI()
    app.include_router(batch_router, prefix="/v1")
    app.include_router(batches_router, prefix="/v1")
    return app


@pytest.fixture
def client(test_app):
    from starlette.testclient import TestClient

    with patch("medwear_med_api.auth.decode_token", return_value=FAKE_RESEARCHER), \
         patch("medwear_med_api.db.get_all_projects", AsyncMock(return_value=[TEST_PG_DB])), \
         patch("medwear_med_api.influx_writer.write_signal_batch", return_value=10), \
         patch("medwear_med_api.routers.batches.read_signals_by_filters", return_value={}), \
         patch("medwear_med_api.routers.batches.read_signals_by_uuid", return_value=[]):
        with TestClient(test_app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture
def admin_client(test_app):
    from starlette.testclient import TestClient

    with patch("medwear_med_api.auth.decode_token", return_value=FAKE_GLOBAL_ADMIN), \
         patch("medwear_med_api.db.get_all_projects", AsyncMock(return_value=[TEST_PG_DB])), \
         patch("medwear_med_api.influx_writer.write_signal_batch", return_value=10), \
         patch("medwear_med_api.routers.batches.read_signals_by_filters", return_value={}), \
         patch("medwear_med_api.routers.batches.read_signals_by_uuid", return_value=[]):
        with TestClient(test_app, raise_server_exceptions=True) as c:
            yield c


# ===========================================================================
# DB layer tests
# ===========================================================================

class TestDatabaseLayer:
    @pytest.mark.asyncio
    async def test_insert_and_query_batch(self, db_conn):
        uid = str(uuid.uuid4())
        now = datetime.datetime.utcnow()

        await db_conn.execute(
            """
            INSERT INTO measurement_batch
              (uuid, patient_id, device_id, signal_type, schema_id,
               start_timestamp, end_timestamp, sampling_rate_hz,
               sample_count, units, idempotency_key, created_at, is_deleted)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            uid, "42", "test-device", "ECG", "schema-v1",
            now, now, 128.0, 256, "mV", uid, now, False,
        )

        row = await db_conn.fetchrow(
            "SELECT patient_id, signal_type FROM measurement_batch WHERE uuid=$1", uid
        )
        assert row["patient_id"] == "42"
        assert row["signal_type"] == "ECG"

    @pytest.mark.asyncio
    async def test_idempotency_key_unique_constraint(self, db_conn):
        uid1 = str(uuid.uuid4())
        uid2 = str(uuid.uuid4())
        idem_key = str(uuid.uuid4())
        now = datetime.datetime.utcnow()

        await db_conn.execute(
            """INSERT INTO measurement_batch
               (uuid, patient_id, device_id, signal_type, schema_id,
                start_timestamp, end_timestamp, sampling_rate_hz,
                sample_count, units, idempotency_key, created_at, is_deleted)
               VALUES ($1,1,'d','ECG','s',$2,$2,1.0,1,'mV',$3,$2,false)""",
            uid1, now, idem_key,
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await db_conn.execute(
                """INSERT INTO measurement_batch
                   (uuid, patient_id, device_id, signal_type, schema_id,
                    start_timestamp, end_timestamp, sampling_rate_hz,
                    sample_count, units, idempotency_key, created_at, is_deleted)
                   VALUES ($1,1,'d','ECG','s',$2,$2,1.0,1,'mV',$3,$2,false)""",
                uid2, now, idem_key,
            )

    @pytest.mark.asyncio
    async def test_soft_delete_flag(self, db_conn):
        uid = str(uuid.uuid4())
        now = datetime.datetime.utcnow()

        await db_conn.execute(
            """INSERT INTO measurement_batch
               (uuid, patient_id, device_id, signal_type, schema_id,
                start_timestamp, end_timestamp, sampling_rate_hz,
                sample_count, units, idempotency_key, created_at, is_deleted)
               VALUES ($1,1,'d','ECG','s',$2,$2,1.0,1,'mV',$1,$2,false)""",
            uid, now,
        )
        await db_conn.execute(
            "UPDATE measurement_batch SET is_deleted=true, deleted_at=NOW() WHERE uuid=$1", uid
        )
        row = await db_conn.fetchrow(
            "SELECT is_deleted FROM measurement_batch WHERE uuid=$1", uid
        )
        assert row["is_deleted"] is True

    @pytest.mark.asyncio
    async def test_user_project_insert(self, db_conn):
        await db_conn.execute(
            "INSERT INTO user_project (username, project_id, role) VALUES ($1,$2,$3)",
            "alice", TEST_PG_DB, "researcher",
        )
        row = await db_conn.fetchrow(
            "SELECT role FROM user_project WHERE username='alice'"
        )
        assert row["role"] == "researcher"

    @pytest.mark.asyncio
    async def test_project_settings_retention(self, db_conn):
        await db_conn.execute(
            """INSERT INTO project_settings (project_id, retention_days, updated_at, updated_by)
               VALUES ($1, $2, NOW(), 'test')
               ON CONFLICT (project_id) DO UPDATE SET retention_days=$2""",
            TEST_PG_DB, 90,
        )
        row = await db_conn.fetchrow(
            "SELECT retention_days FROM project_settings WHERE project_id=$1", TEST_PG_DB
        )
        assert row["retention_days"] == 90

    @pytest.mark.asyncio
    async def test_audit_log_insert(self, db_conn):
        await db_conn.execute(
            """INSERT INTO audit_log (username, method, path, status_code, duration_ms)
               VALUES ($1,$2,$3,$4,$5)""",
            "alice", "POST", "/v1/data/batch", 201, 42.5,
        )
        row = await db_conn.fetchrow(
            "SELECT status_code FROM audit_log WHERE username='alice'"
        )
        assert row["status_code"] == 201


# ===========================================================================
# db.py helper tests
# ===========================================================================

class TestDbHelpers:
    @pytest.mark.asyncio
    async def test_get_all_projects_returns_list(self):
        from medwear_med_api.db import get_all_projects
        result = await get_all_projects()
        assert isinstance(result, list)
        assert TEST_PG_DB in result

    @pytest.mark.asyncio
    async def test_create_and_hard_delete_project(self):
        from medwear_med_api.db import create_project_db, hard_delete_project
        test_proj = "test_temp_project"

        created = await create_project_db(test_proj)
        assert created is True

        await hard_delete_project(test_proj)

        from medwear_med_api.db import get_all_projects
        projects = await get_all_projects()
        assert test_proj not in projects

    @pytest.mark.asyncio
    async def test_validate_project_name_blocks_bad_input(self):
        from medwear_med_api.db import _validate_project_name
        with pytest.raises(ValueError):
            _validate_project_name('"; DROP DATABASE postgres --')


# ===========================================================================
# audit.py — get_user_projects against real DB
# ===========================================================================

class TestAuditUserProjects:
    @pytest.mark.asyncio
    async def test_finds_user_in_project(self, db_conn):
        await db_conn.execute(
            "INSERT INTO user_project (username, project_id, role) VALUES ($1,$2,$3)",
            "bob", TEST_PG_DB, "collaborator",
        )

        from medwear_med_api.audit import get_user_projects
        with patch("medwear_med_api.audit.get_all_projects",
                   AsyncMock(return_value=[TEST_PG_DB])):
            result = await get_user_projects("bob")

        assert TEST_PG_DB in result

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_user(self, db_conn):
        from medwear_med_api.audit import get_user_projects
        with patch("medwear_med_api.audit.get_all_projects",
                   AsyncMock(return_value=[TEST_PG_DB])):
            result = await get_user_projects("nobody_xyz")

        assert result == []


# ===========================================================================
# InfluxDB reader sanitisation — live InfluxDB
# ===========================================================================

class TestInfluxSanitisationLive:
    def test_invalid_uuid_blocked_before_query(self):
        from medwear_med_api.influx_reader import read_signals_by_uuid
        with pytest.raises(ValueError):
            read_signals_by_uuid('"; import "csv" from(bucket:"other")//')

    def test_valid_uuid_reaches_influx(self):
        """Valid UUID should reach InfluxDB (returns empty list, not raises)."""
        from medwear_med_api.influx_reader import read_signals_by_uuid
        result = read_signals_by_uuid("00000000-0000-0000-0000-000000000000")
        assert isinstance(result, list)

    def test_unsafe_signal_type_blocked(self):
        from medwear_med_api.influx_reader import read_signals_by_filters
        with pytest.raises(ValueError):
            read_signals_by_filters(signal_type='ecg"; import "csv"')


# ===========================================================================
# API endpoint tests — TestClient with mocked auth + InfluxDB
# ===========================================================================

def _batch_payload(patient_id: str = "42", signal: str = "PPG", project_id: str = TEST_PG_DB):
    import uuid as _uuid
    from datetime import datetime, timezone, timedelta
    batch_uuid = str(_uuid.uuid4())
    device_id = f"test-device-{_uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    start = (now - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    created = (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n_samples = 256
    return {
        "batches": [{
            "header": {
                "uuid": batch_uuid,
                "schema_id": "ppg-1.0",
                "source_creation_date_time": created,
                "acquisition_rate": {"value": n_samples, "unit": "Hz", "number_of_times": 1},
            },
            "body": {
                "signal_data": [0.1] * n_samples,
                "unit": "mV",
                "effective_time_frame": {
                    "time_interval": {
                        "start_date_time": start,
                        "end_date_time": end,
                    }
                },
            },
            "signal_type": signal,
            "patient_id": patient_id,
            "device_id": device_id,
            "project_id": project_id,
        }]
    }


class TestBatchUploadEndpoint:

    def test_upload_batch_returns_200(self, client):
        r = client.post("/v1/data/batch", json=_batch_payload(),
                        headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200, r.text

    def test_upload_batch_missing_body_returns_4xx(self, client):
        r = client.post("/v1/data/batch", json={},
                        headers={"Authorization": "Bearer fake"})
        assert r.status_code in (400, 422)

    def test_upload_batch_invalid_signal_type_returns_4xx(self, client):
        payload = _batch_payload()
        payload["batches"][0]["signal_type"] = "INVALID_SIGNAL"
        r = client.post("/v1/data/batch", json=payload,
                        headers={"Authorization": "Bearer fake"})
        assert r.status_code in (400, 422)

    def test_upload_batch_no_auth_returns_401(self, client):
        r = client.post("/v1/data/batch", json=_batch_payload())
        assert r.status_code == 401

    def test_upload_duplicate_batch_returns_duplicate_status(self, client):
        payload = _batch_payload()
        r1 = client.post("/v1/data/batch", json=payload,
                         headers={"Authorization": "Bearer fake"})
        assert r1.status_code == 200, r1.text
        r2 = client.post("/v1/data/batch", json=payload,
                         headers={"Authorization": "Bearer fake"})
        assert r2.status_code == 200
        results = r2.json().get("results", [])
        assert any(r.get("status") == "duplicate" for r in results)


class TestBatchQueryEndpoint:

    def test_query_returns_200(self, client):
        r = client.get("/v1/data?metadata_only=true",
                       headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200
        assert "batches" in r.json()

    def test_query_no_auth_returns_401(self, client):
        r = client.get("/v1/data?metadata_only=true")
        assert r.status_code == 401

    def test_query_unknown_uuid_returns_404(self, client):
        r = client.get("/v1/data/00000000-0000-0000-0000-000000000000",
                       headers={"Authorization": "Bearer fake"})
        assert r.status_code == 404



class TestBatchDeleteEndpoint:

    def test_delete_requires_admin(self, client):
        payload = _batch_payload(patient_id="55")
        r = client.post("/v1/data/batch", json=payload,
                        headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200, r.text
        batch_uuid = payload["batches"][0]["header"]["uuid"]

        r_del = client.delete(f"/v1/data/{batch_uuid}",
                              headers={"Authorization": "Bearer fake"})
        assert r_del.status_code == 403



# ===========================================================================
# Two-phase ingest: acknowledged only when PostgreSQL and InfluxDB both have it
# ===========================================================================

def _status_of(idempotency_key_raw: str):
    """ingest_status/uuid of the row for a client idempotency key (sync helper)."""
    import hashlib

    async def _q():
        conn = await asyncpg.connect(host=TEST_PG_HOST, port=TEST_PG_PORT, user=TEST_PG_USER,
                                     password=TEST_PG_PASS, database=TEST_PG_DB)
        try:
            return await conn.fetchrow(
                "SELECT uuid, ingest_status, session_id, patient_id, device_id FROM measurement_batch "
                "WHERE idempotency_key=$1", hashlib.sha256(idempotency_key_raw.encode()).hexdigest())
        finally:
            await conn.close()
    return asyncio.new_event_loop().run_until_complete(_q())


class TestTwoPhaseIngest:

    def _payload(self):
        payload = _batch_payload(patient_id="77")
        payload["batches"][0]["idempotency_key"] = f"tp-{uuid.uuid4()}"
        return payload, payload["batches"][0]["idempotency_key"]

    def test_influx_failure_leaves_pending_and_resubmit_resumes(self, client):
        payload, ikey = self._payload()
        with patch("medwear_med_api.batch_router.write_signal_batch", side_effect=RuntimeError("influx down")):
            r1 = client.post("/v1/data/batch", json=payload, headers={"Authorization": "Bearer fake"})
        assert r1.status_code == 500
        assert r1.json()["detail"]["error"] == "PARTIAL_WRITE_FAILURE"
        row = _status_of(ikey)
        assert row["ingest_status"] == "pending"

        # a pending batch is not visible
        listed = client.get("/v1/data?metadata_only=true&patient_ids=77",
                            headers={"Authorization": "Bearer fake"}).json()["batches"]
        assert row["uuid"] not in [b["uuid"] for b in listed]

        with patch("medwear_med_api.batch_router.write_signal_batch", return_value=256) as w:
            r2 = client.post("/v1/data/batch", json=payload, headers={"Authorization": "Bearer fake"})
        assert r2.status_code == 200, r2.text
        res = r2.json()["results"][0]
        assert res["status"] == "ingested"
        assert res["batch_uuid"] == row["uuid"]            # same series in InfluxDB
        assert w.call_args.kwargs["batch_uuid"] == row["uuid"]
        assert "resumed" in res["message"]
        assert _status_of(ikey)["ingest_status"] == "complete"

        with patch("medwear_med_api.batch_router.write_signal_batch", return_value=256) as w3:
            r3 = client.post("/v1/data/batch", json=payload, headers={"Authorization": "Bearer fake"})
        assert r3.json()["results"][0]["status"] == "duplicate"
        w3.assert_not_called()

    def test_pending_with_different_samples_conflicts(self, client):
        payload, ikey = self._payload()
        with patch("medwear_med_api.batch_router.write_signal_batch", side_effect=RuntimeError("influx down")):
            client.post("/v1/data/batch", json=payload, headers={"Authorization": "Bearer fake"})
        payload["batches"][0]["body"]["signal_data"] = [0.2] * 256
        with patch("medwear_med_api.batch_router.write_signal_batch", return_value=256) as w:
            r = client.post("/v1/data/batch", json=payload, headers={"Authorization": "Bearer fake"})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "IDEMPOTENCY_CONFLICT"
        w.assert_not_called()
        assert _status_of(ikey)["ingest_status"] == "pending"

    def test_success_is_complete(self, client):
        payload, ikey = self._payload()
        with patch("medwear_med_api.batch_router.write_signal_batch", return_value=256):
            r = client.post("/v1/data/batch", json=payload, headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200, r.text
        assert _status_of(ikey)["ingest_status"] == "complete"


# ===========================================================================
# Sessions: stream metadata sent once
# ===========================================================================

def _session_payload(session_id=None, device_id=None):
    return {
        "session_id": session_id or str(uuid.uuid4()),
        "patient_id": "88",
        "device_id": device_id or f"sess-dev-{uuid.uuid4().hex[:8]}",
        "signal_type": "PPG",
        "unit": "mV",
        "project_id": TEST_PG_DB,
        "header": {"schema_id": "ppg-1.0", "acquisition_rate": {"value": 256, "unit": "Hz"}},
    }


def _lean_batch(session_id, ikey=None):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    return {
        "session_id": session_id,
        "project_id": TEST_PG_DB,
        "idempotency_key": ikey or f"lean-{uuid.uuid4()}",
        "body": {
            "signal_data": [0.1] * 256,
            "effective_time_frame": {"time_interval": {
                "start_date_time": (now - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_time": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }},
        },
    }


class TestSessions:

    def test_register_is_idempotent_and_conflicts_on_change(self, client):
        reg = _session_payload()
        r1 = client.post("/v1/data/session", json=reg, headers={"Authorization": "Bearer fake"})
        assert r1.status_code == 200, r1.text
        assert r1.json()["status"] == "created"
        r2 = client.post("/v1/data/session", json=reg, headers={"Authorization": "Bearer fake"})
        assert r2.json()["status"] == "existing"
        changed = {**reg, "device_id": "another-device"}
        r3 = client.post("/v1/data/session", json=changed, headers={"Authorization": "Bearer fake"})
        assert r3.status_code == 409
        assert r3.json()["detail"]["error"] == "SESSION_CONFLICT"

    def test_invalid_unit_for_ecg_rejected(self, client):
        reg = {**_session_payload(), "signal_type": "ECG", "unit": "ADC"}
        r = client.post("/v1/data/session", json=reg, headers={"Authorization": "Bearer fake"})
        assert r.status_code == 422

    def test_lean_batch_uses_session_metadata(self, client):
        reg = _session_payload()
        client.post("/v1/data/session", json=reg, headers={"Authorization": "Bearer fake"})
        batch = _lean_batch(reg["session_id"])
        with patch("medwear_med_api.batch_router.write_signal_batch", return_value=256) as w:
            r = client.post("/v1/data/batch", json={"batches": [batch]},
                            headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200, r.text
        assert r.json()["results"][0]["status"] == "ingested"
        assert w.call_args.kwargs["device_id"] == reg["device_id"]
        assert w.call_args.kwargs["sampling_rate_hz"] == 256
        row = _status_of(batch["idempotency_key"])
        assert (row["session_id"], row["patient_id"], row["device_id"]) == \
               (reg["session_id"], reg["patient_id"], reg["device_id"])
        assert row["ingest_status"] == "complete"

        # single (non-list) form, and duplicates still detected
        with patch("medwear_med_api.batch_router.write_signal_batch", return_value=256):
            r2 = client.post("/v1/data/batch", json=batch, headers={"Authorization": "Bearer fake"})
        assert r2.json()["results"][0]["status"] == "duplicate"

    def test_lean_batch_unknown_session_404(self, client):
        r = client.post("/v1/data/batch", json=_lean_batch(str(uuid.uuid4())),
                        headers={"Authorization": "Bearer fake"})
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "UNKNOWN_SESSION"

    def test_lean_batch_validated_like_full_batch(self, client):
        reg = _session_payload()
        client.post("/v1/data/session", json=reg, headers={"Authorization": "Bearer fake"})
        batch = _lean_batch(reg["session_id"])
        batch["body"]["signal_data"] = [0.1] * 512   # 512 samples in 1 s at 256 Hz
        r = client.post("/v1/data/batch", json=batch, headers={"Authorization": "Bearer fake"})
        assert r.status_code == 400

    def test_session_of_other_uploader_forbidden(self, client):
        reg = _session_payload()
        client.post("/v1/data/session", json=reg, headers={"Authorization": "Bearer fake"})
        other = {**FAKE_RESEARCHER, "preferred_username": "someone_else"}
        with patch("medwear_med_api.auth.decode_token", return_value=other):
            r = client.post("/v1/data/batch", json=_lean_batch(reg["session_id"]),
                            headers={"Authorization": "Bearer fake"})
        assert r.status_code == 403
