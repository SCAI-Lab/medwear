"""
Migration correctness tests.

Requires: docker compose -f docker-compose.test.yml up -d (from repo root)

Run: cd medwear_med_api && pytest tests/test_migrations.py -v
"""
import os
import pytest
import asyncpg
import asyncio

# --- Test DB connection -------------------------------------------------------
TEST_PG_HOST = os.getenv("TEST_PG_HOST", "localhost")
TEST_PG_PORT = int(os.getenv("POSTGRES_PORT", os.getenv("TEST_PG_PORT", "5432")))
TEST_PG_USER = os.getenv("TEST_PG_USER", "test_user")
TEST_PG_PASS = os.getenv("TEST_PG_PASS", "test_pass")
TEST_PG_DB   = "medwear_migration_test"  # isolated DB — never conflicts with integration tests

TEST_DB_URL = (
    f"postgresql://{TEST_PG_USER}:{TEST_PG_PASS}"
    f"@{TEST_PG_HOST}:{TEST_PG_PORT}/{TEST_PG_DB}"
)


def _set_alembic_env():
    os.environ["POSTGRES_USER"]     = TEST_PG_USER
    os.environ["POSTGRES_PASSWORD"] = TEST_PG_PASS
    os.environ["POSTGRES_HOST"]     = TEST_PG_HOST
    os.environ["POSTGRES_PORT"]     = str(TEST_PG_PORT)
    os.environ["POSTGRES_DB"]       = TEST_PG_DB
    os.environ.setdefault("INFLUX_TOKEN",            "test-influx-token")
    os.environ.setdefault("MINIO_API_ACCESS_KEY",    "test-access")
    os.environ.setdefault("MINIO_API_SECRET_KEY",    "test-secret")
    os.environ.setdefault("KEYCLOAK_URL",            "http://localhost:8080")
    os.environ.setdefault("KEYCLOAK_REALM",          "medwear")
    os.environ.setdefault("KEYCLOAK_CLIENT_ID",      "med-api")
    os.environ.setdefault("KEYCLOAK_CLIENT_SECRET",  "test-secret")
    os.environ.setdefault("KEYCLOAK_ADMIN",          "admin")
    os.environ.setdefault("KEYCLOAK_ADMIN_PASSWORD", "admin-pass")


@pytest.fixture(scope="module", autouse=True)
def run_migrations():
    """Create isolated DB, run Alembic migrations, tear down after."""
    import asyncio
    import asyncpg

    _set_alembic_env()

    async def _create_db():
        conn = await asyncpg.connect(
            host=TEST_PG_HOST, port=TEST_PG_PORT,
            user=TEST_PG_USER, password=TEST_PG_PASS,
            database="postgres",
        )
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname=$1", TEST_PG_DB
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_PG_DB}" OWNER {TEST_PG_USER}')
        await conn.close()

    async def _drop_db():
        conn = await asyncpg.connect(
            host=TEST_PG_HOST, port=TEST_PG_PORT,
            user=TEST_PG_USER, password=TEST_PG_PASS,
            database="postgres",
        )
        await conn.execute(
            f'SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',
            TEST_PG_DB,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_PG_DB}"')
        await conn.close()

    asyncio.run(_create_db())

    from alembic.config import Config
    from alembic import command
    import pathlib

    ini_path = pathlib.Path(__file__).parent.parent / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)

    command.upgrade(alembic_cfg, "head")
    yield
    command.downgrade(alembic_cfg, "base")
    asyncio.run(_drop_db())


@pytest.fixture
async def conn():
    connection = await asyncpg.connect(
        host=TEST_PG_HOST, port=TEST_PG_PORT,
        user=TEST_PG_USER, password=TEST_PG_PASS,
        database=TEST_PG_DB,
    )
    yield connection
    await connection.close()


# =============================================================================
# Migration correctness tests
# =============================================================================

class TestMigrationSchema:
    """Verify every expected table and column exists after upgrade."""

    @pytest.mark.asyncio
    async def test_measurement_batch_table_exists(self, conn):
        row = await conn.fetchrow(
            "SELECT to_regclass('public.measurement_batch') AS tbl"
        )
        assert row["tbl"] is not None

    @pytest.mark.asyncio
    async def test_audit_log_table_exists(self, conn):
        row = await conn.fetchrow(
            "SELECT to_regclass('public.audit_log') AS tbl"
        )
        assert row["tbl"] is not None

    @pytest.mark.asyncio
    async def test_user_project_table_exists(self, conn):
        row = await conn.fetchrow(
            "SELECT to_regclass('public.user_project') AS tbl"
        )
        assert row["tbl"] is not None

    @pytest.mark.asyncio
    async def test_project_settings_table_exists(self, conn):
        """Verifies the new migration (c4d5e6f7a8b9) was applied."""
        row = await conn.fetchrow(
            "SELECT to_regclass('public.project_settings') AS tbl"
        )
        assert row["tbl"] is not None, (
            "project_settings table missing — migration c4d5e6f7a8b9 may not have run"
        )

    @pytest.mark.asyncio
    async def test_project_settings_columns(self, conn):
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'project_settings'
            """
        )
        cols = {r["column_name"] for r in rows}
        assert {"project_id", "retention_days", "updated_at", "updated_by"} <= cols

    @pytest.mark.asyncio
    async def test_measurement_batch_idempotency_unique(self, conn):
        """idempotency_key must have a unique constraint."""
        rows = await conn.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'measurement_batch'
              AND indexdef LIKE '%idempotency_key%'
            """
        )
        assert len(rows) >= 1, "No unique index on measurement_batch.idempotency_key"

    @pytest.mark.asyncio
    async def test_alembic_version_recorded(self, conn):
        """Alembic version table must record the latest revision."""
        rows = await conn.fetch("SELECT version_num FROM alembic_version")
        assert len(rows) == 1
        assert rows[0]["version_num"] == "83241c70658c"

    @pytest.mark.asyncio
    async def test_roundtrip_insert_measurement_batch(self, conn):
        """Basic insert → select → delete to confirm the schema is functional."""
        import uuid, datetime
        uid = str(uuid.uuid4())
        now = datetime.datetime.utcnow()

        await conn.execute(
            """
            INSERT INTO measurement_batch
              (uuid, patient_id, device_id, signal_type, schema_id,
               start_timestamp, end_timestamp, sampling_rate_hz,
               sample_count, units, idempotency_key, created_at, is_deleted)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            uid, "1", "test-device", "ECG", "test-schema",
            now, now, 128.0, 10, "mV", uid, now, False,
        )
        row = await conn.fetchrow(
            "SELECT uuid FROM measurement_batch WHERE uuid=$1", uid
        )
        assert row["uuid"] == uid

        await conn.execute("DELETE FROM measurement_batch WHERE uuid=$1", uid)

    @pytest.mark.asyncio
    async def test_roundtrip_insert_project_settings(self, conn):
        await conn.execute(
            """
            INSERT INTO project_settings (project_id, retention_days, updated_at, updated_by)
            VALUES ($1, $2, NOW(), $3)
            ON CONFLICT (project_id) DO NOTHING
            """,
            "test_project", 30, "test"
        )
        row = await conn.fetchrow(
            "SELECT retention_days FROM project_settings WHERE project_id='test_project'"
        )
        assert row["retention_days"] == 30
        await conn.execute("DELETE FROM project_settings WHERE project_id='test_project'")
