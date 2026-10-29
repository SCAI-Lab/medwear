import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel, Column
from sqlalchemy import UniqueConstraint
import sqlalchemy.dialects.postgresql as pg


def generate_uuid() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MeasurementBatch(SQLModel, table=True):
    __tablename__ = "measurement_batch"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_idempotency_key"),
    )

    uuid: str = Field(default_factory=generate_uuid, primary_key=True)
    patient_id: str = Field(nullable=False, max_length=255)
    device_id: str = Field(nullable=False, max_length=255)
    signal_type: str = Field(nullable=False, max_length=50)
    schema_id: str = Field(nullable=False, max_length=100)
    start_timestamp: datetime = Field(nullable=False)
    end_timestamp: datetime = Field(nullable=False)
    sampling_rate_hz: float = Field(nullable=False)
    sample_count: int = Field(nullable=False)
    units: str = Field(nullable=False, max_length=20)
    metadata_json: Optional[dict] = Field(default=None, sa_column=Column(pg.JSONB, nullable=True))
    project_id: Optional[str] = Field(default=None, max_length=100)
    idempotency_key: str = Field(nullable=False, max_length=255)
    checksum: Optional[str] = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=now_utc)
    is_deleted: bool = Field(default=False)
    deleted_at: Optional[datetime] = Field(default=None)
    # "pending" from the PostgreSQL insert until the InfluxDB write has
    # succeeded, then "complete". Only complete batches are acknowledged and
    # visible; a resubmission of a pending batch resumes the write.
    ingest_status: str = Field(default="complete", max_length=16)
    session_id: Optional[str] = Field(default=None, max_length=64)


class IngestSession(SQLModel, table=True):
    """Stream metadata registered once (POST /data/session); batches that
    reference session_id carry only their samples and time frame."""
    __tablename__ = "ingest_session"

    session_id: str = Field(default_factory=generate_uuid, primary_key=True, max_length=64)
    patient_id: str = Field(nullable=False, max_length=255)
    device_id: str = Field(nullable=False, max_length=255)
    signal_type: str = Field(nullable=False, max_length=50)
    schema_id: str = Field(nullable=False, max_length=100)
    sampling_rate_hz: float = Field(nullable=False)
    units: str = Field(nullable=False, max_length=20)
    header_json: Optional[dict] = Field(default=None, sa_column=Column(pg.JSONB, nullable=True))
    project_id: str = Field(nullable=False, max_length=100)
    created_by: str = Field(nullable=False, max_length=255)
    created_at: datetime = Field(default_factory=now_utc)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=now_utc)
    username: str = Field(nullable=False, max_length=255)
    method: str = Field(nullable=False, max_length=10)
    path: str = Field(nullable=False, max_length=500)
    status_code: int = Field(nullable=False)
    duration_ms: float = Field(nullable=False)


class UserProject(SQLModel, table=True):
    __tablename__ = "user_project"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(nullable=False, max_length=255)
    project_id: str = Field(nullable=False, max_length=100)
    role: str = Field(nullable=False, max_length=50)
    created_at: datetime = Field(default_factory=now_utc)


class PatientConsent(SQLModel, table=True):
    """
    GDPR Art. 6/7 — explicit consent records per patient per purpose.
    Fields align with ETH Zürich Ethics Commission requirements.
    """
    __tablename__ = "patient_consent"

    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: str = Field(nullable=False, max_length=255, index=True)
    # consent_type: "data_collection" | "data_sharing" | "research_use" | "third_party_transfer"
    consent_type: str = Field(nullable=False, max_length=100)
    granted: bool = Field(default=True)
    granted_at: datetime = Field(default_factory=now_utc)
    revoked_at: Optional[datetime] = Field(default=None)
    consent_version: str = Field(default="1.0", max_length=20)
    granted_by: str = Field(nullable=False, max_length=255)
    project_id: str = Field(nullable=False, max_length=100)
    notes: Optional[str] = Field(default=None)


class ProjectSettings(SQLModel, table=True):
    __tablename__ = "project_settings"

    project_id: str = Field(primary_key=True, max_length=100)
    retention_days: Optional[int] = Field(default=None)  # None = never auto-expire
    updated_at: datetime = Field(default_factory=now_utc)
    updated_by: str = Field(default="system", max_length=255)
