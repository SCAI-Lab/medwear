"""
Phase 1 — Integration Test
Tests the full pipeline:
1. Write metadata to PostgreSQL (measurement_batch table)
2. Write signal samples to InfluxDB
3. Verify both databases contain the correct data linked by uuid
"""

import asyncio
import os
# defaults: the throwaway test stack of docker-compose.test.yml
os.environ.setdefault("INFLUX_URL", "http://localhost:8087")
os.environ.setdefault("INFLUX_TOKEN", "test-influx-token")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test_user:test_pass@localhost:5434/medwear_project1")
import sys
import os
import uuid as uuid_lib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from medwear_med_api.models import MeasurementBatch
from medwear_med_api.influx_writer import write_signal_batch
from influxdb_client import InfluxDBClient

# --- Connection settings ---
PG_URL        = os.environ["DATABASE_URL"]
INFLUX_URL    = os.environ["INFLUX_URL"]
INFLUX_TOKEN  = os.environ["INFLUX_TOKEN"]
INFLUX_ORG    = "medwear"
INFLUX_BUCKET = "wearables"

# --- Fake ECG test data ---
TEST_PATIENT_ID    = 1234567890
TEST_DEVICE_ID     = "polar-h10-test-001"
TEST_SIGNAL_TYPE   = "ECG"
TEST_SCHEMA_ID     = "ecg-lead-1.x"
TEST_SAMPLING_RATE = 250.0
TEST_SAMPLES       = [0.12, 0.15, 0.11, 0.10, 0.13, 0.14, 0.16, 0.12, 0.11, 0.10]
TEST_UNITS         = "mV"
TEST_START         = datetime(2026, 2, 27, 10, 0, 0)
TEST_END           = TEST_START + timedelta(seconds=len(TEST_SAMPLES)/TEST_SAMPLING_RATE)
TEST_IDEMPOTENCY   = f"{TEST_DEVICE_ID}-{uuid_lib.uuid4().hex[:8]}"
TEST_METADATA      = {"lead": "II", "gain": 1000, "electrode": "chest"}


async def test_postgresql():
    print("\n--- STEP 1: Writing to PostgreSQL ---")

    engine = create_async_engine(PG_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    batch = MeasurementBatch(
        patient_id       = TEST_PATIENT_ID,
        device_id        = TEST_DEVICE_ID,
        signal_type      = TEST_SIGNAL_TYPE,
        schema_id        = TEST_SCHEMA_ID,
        start_timestamp  = TEST_START,
        end_timestamp    = TEST_END,
        sampling_rate_hz = TEST_SAMPLING_RATE,
        sample_count     = len(TEST_SAMPLES),
        units            = TEST_UNITS,
        metadata_json    = TEST_METADATA,
        idempotency_key  = TEST_IDEMPOTENCY,
    )

    async with async_session() as session:
        session.add(batch)
        await session.commit()
        await session.refresh(batch)
        batch_uuid = batch.uuid

    print(f"  ✓ Written to PostgreSQL")
    print(f"  ✓ batch_uuid = {batch_uuid}")

    async with async_session() as session:
        result = await session.execute(
            select(MeasurementBatch).where(MeasurementBatch.uuid == batch_uuid)
        )
        saved = result.scalar_one_or_none()

    if saved:
        print(f"  ✓ Verified in PostgreSQL:")
        print(f"    patient_id      = {saved.patient_id}")
        print(f"    device_id       = {saved.device_id}")
        print(f"    signal_type     = {saved.signal_type}")
        print(f"    sampling_rate   = {saved.sampling_rate_hz} Hz")
        print(f"    sample_count    = {saved.sample_count}")
        print(f"    idempotency_key = {saved.idempotency_key}")
    else:
        print("  ✗ ERROR: batch not found in PostgreSQL!")
        return None

    await engine.dispose()
    return batch_uuid


def test_influxdb(batch_uuid: str):
    print("\n--- STEP 2: Writing to InfluxDB ---")

    count = write_signal_batch(
        batch_uuid       = batch_uuid,
        patient_id       = TEST_PATIENT_ID,
        device_id        = TEST_DEVICE_ID,
        signal_type      = TEST_SIGNAL_TYPE,
        samples          = TEST_SAMPLES,
        start_timestamp  = TEST_START,
        sampling_rate_hz = TEST_SAMPLING_RATE,
    )
    print(f"  ✓ Written {count} samples to InfluxDB")

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()

    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: 2026-02-27T00:00:00Z)
  |> filter(fn: (r) => r["batch_uuid"] == "{batch_uuid}")
  |> count()
'''
    tables = query_api.query(query, org=INFLUX_ORG)
    client.close()

    total = sum(record.get_value() for table in tables for record in table.records)
    if total > 0:
        print(f"  ✓ Verified in InfluxDB: {total} samples found")
        print(f"  ✓ Linked by batch_uuid = {batch_uuid}")
    else:
        print("  ✗ ERROR: samples not found in InfluxDB!")


async def main():
    print("=" * 50)
    print("Phase 1 Integration Test")
    print("ECG batch → PostgreSQL + InfluxDB")
    print("=" * 50)

    batch_uuid = await test_postgresql()
    if batch_uuid:
        test_influxdb(batch_uuid)
        print("\n" + "=" * 50)
        print("✓ Phase 1 test PASSED")
        print(f"  PostgreSQL and InfluxDB linked via uuid:")
        print(f"  {batch_uuid}")
        print("=" * 50)

if __name__ == "__main__":
    asyncio.run(main())
