"""
MED-API Phase 3 — Batch Upload Schemas
"""

import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# UUID v1-v5 format
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Allow at most 5 minutes of clock skew for future timestamps
_MAX_FUTURE_SKEW_SECONDS = 300

# Sample count coherence tolerance: 20 % of expected OR 2 samples, whichever is larger
_SAMPLE_COUNT_TOLERANCE_PCT = 0.20
_SAMPLE_COUNT_MIN_TOLERANCE = 2

# Maximum samples per batch (10 minutes at 2 kHz)
_MAX_SAMPLES = 2_000 * 600

# ECG physiological amplitude limits per unit
_ECG_AMPLITUDE_LIMITS: dict = {
    "uV": (-10_000.0, 10_000.0),
    "mV": (-10.0, 10.0),
}
# Up to 1% of samples may be transient noise spikes outside the range
_ECG_OUTLIER_TOLERANCE = 0.01
# Baseline wander: signal mean > 30% of max amplitude indicates DC offset
_ECG_BASELINE_WANDER_RATIO = 0.30


def _validate_no_nan_inf(samples: List[float], field_name: str) -> List[float]:
    bad = [i for i, v in enumerate(samples) if not math.isfinite(v)]
    if bad:
        raise ValueError(
            f"{field_name} contains non-finite values (NaN/Inf) at indices {bad[:5]}"
            + (" …" if len(bad) > 5 else "")
        )
    return samples


def _validate_sample_count(samples: List[float], field_name: str) -> List[float]:
    if len(samples) > _MAX_SAMPLES:
        raise ValueError(
            f"{field_name} has {len(samples)} samples; "
            f"maximum allowed per batch is {_MAX_SAMPLES}"
        )
    return samples


class SignalType(str, Enum):
    ECG = "ECG"
    BCG = "BCG"
    PPG = "PPG"
    IMU = "IMU"
    EEG = "EEG"
    EDA = "EDA"
    HR = "HR"
    HRV = "HRV"
    # Further healthcare_msgs types (raw and derived biosignals). They use
    # the generic body (signal_data + unit).
    EMG = "EMG"
    EOG = "EOG"
    ICG = "ICG"
    BVP = "BVP"
    RR_INTERVAL = "RR_INTERVAL"
    RESPIRATORY_RATE = "RESPIRATORY_RATE"
    STRESS = "STRESS"
    CARDIAC_OUTPUT = "CARDIAC_OUTPUT"
    STROKE_VOLUME = "STROKE_VOLUME"


class ECGUnit(str, Enum):
    uV = "uV"
    mV = "mV"


class BCGUnit(str, Enum):
    mV = "mV"
    uV = "uV"
    V = "V"


class Modality(str, Enum):
    sensed = "sensed"
    self_reported = "self-reported"


class AcquisitionRate(BaseModel):
    value: float = Field(..., gt=0)   # strictly positive — 0 Hz is invalid
    unit: Literal["Hz"] = "Hz"
    number_of_times: int = Field(default=1, ge=1)


class DeviceIdentity(BaseModel):
    device_manufacturer: Optional[str] = None
    device_name: Optional[str] = None
    device_type: Optional[str] = None
    device_serial_number: Optional[str] = None


class ECGInfo(BaseModel):
    electrode_placement: Optional[List[str]] = None
    lead_type: Optional[List[str]] = None


class BCGInfo(BaseModel):
    sensor_placement: Optional[str] = None
    sensing_direction: Optional[str] = None
    sensing_modality: Optional[str] = None
    measurement_system: Optional[str] = None


class PPGInfo(BaseModel):
    sensor_placement: Optional[str] = None
    wavelengths_nm: Optional[List[int]] = None
    wavelengths_labels: Optional[List[str]] = None
    measuring_mode: Optional[str] = None


class EEGInfo(BaseModel):
    channel_size: Optional[int] = None
    montage_type: Optional[str] = None
    electrode_sites: Optional[List[str]] = None
    selected_preprocessing: Optional[List[str]] = None


class EDAInfo(BaseModel):
    electrode_placement: Optional[List[str]] = None
    electrode_type: Optional[List[str]] = None
    measuring_technique: Optional[str] = None


class EMGInfo(BaseModel):
    electrode_placement: Optional[List[str]] = None
    electrode_type: Optional[List[str]] = None
    signal_mode: Optional[str] = None


class EOGInfo(BaseModel):
    electrode_placement: Optional[List[str]] = None
    lead_type: Optional[List[str]] = None
    signal_mode: Optional[str] = None


class ICGInfo(BaseModel):
    electrode_placement: Optional[List[str]] = None
    lead_type: Optional[List[str]] = None
    technique: Optional[str] = None


class BatchHeader(BaseModel):
    uuid: str = Field(...)
    schema_id: str = Field(...)
    source_creation_date_time: str = Field(...)
    modality: Optional[Modality] = None
    acquisition_rate: Optional[AcquisitionRate] = None
    device_identity: Optional[DeviceIdentity] = None
    ecg_info: Optional[ECGInfo] = None
    bcg_info: Optional[BCGInfo] = None
    ppg_info: Optional[PPGInfo] = None
    eeg_info: Optional[EEGInfo] = None
    eda_info: Optional[EDAInfo] = None
    emg_info: Optional[EMGInfo] = None
    eog_info: Optional[EOGInfo] = None
    icg_info: Optional[ICGInfo] = None

    @field_validator("uuid")
    @classmethod
    def uuid_must_be_valid(cls, v: str) -> str:
        if not _UUID_RE.match(v):
            raise ValueError(f"header.uuid must be a valid UUID, got {v!r}")
        return v

    @field_validator("source_creation_date_time")
    @classmethod
    def creation_datetime_must_parse(cls, v: str) -> str:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"source_creation_date_time must be ISO-8601, got {v!r}"
            )
        now_utc = datetime.now(timezone.utc)
        if dt.tzinfo:
            skew = (dt - now_utc).total_seconds()
        else:
            skew = (dt - now_utc.replace(tzinfo=None)).total_seconds()
        if skew > _MAX_FUTURE_SKEW_SECONDS:
            raise ValueError(
                f"source_creation_date_time is {skew:.0f}s in the future "
                f"(allowed skew: {_MAX_FUTURE_SKEW_SECONDS}s). Check device clock."
            )
        return v


class TimeInterval(BaseModel):
    start_date_time: str
    end_date_time: str

    @model_validator(mode="after")
    def end_after_start(self) -> "TimeInterval":
        try:
            start = datetime.fromisoformat(self.start_date_time.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.end_date_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid ISO-8601 timestamp: {exc}") from exc
        if end <= start:
            raise ValueError(
                f"end_date_time ({self.end_date_time}) must be after "
                f"start_date_time ({self.start_date_time})"
            )
        # Future check on start_date_time
        now_utc = datetime.now(timezone.utc)
        if start.tzinfo:
            skew = (start - now_utc).total_seconds()
        else:
            skew = (start - now_utc.replace(tzinfo=None)).total_seconds()
        if skew > _MAX_FUTURE_SKEW_SECONDS:
            raise ValueError(
                f"start_date_time is {skew:.0f}s in the future. Check device clock."
            )
        return self


class EffectiveTimeFrame(BaseModel):
    time_interval: TimeInterval


class ECGBody(BaseModel):
    ecg_lead_data: List[float] = Field(..., min_length=1)
    signal_quality: List[float] = Field(..., min_length=1)
    unit: ECGUnit
    effective_time_frame: EffectiveTimeFrame

    @field_validator("ecg_lead_data")
    @classmethod
    def ecg_data_finite(cls, v: List[float]) -> List[float]:
        _validate_no_nan_inf(v, "ecg_lead_data")
        return _validate_sample_count(v, "ecg_lead_data")

    @field_validator("signal_quality")
    @classmethod
    def quality_in_range(cls, v: List[float]) -> List[float]:
        bad = [i for i, q in enumerate(v) if not (0.0 <= q <= 1.0)]
        if bad:
            raise ValueError(
                f"signal_quality values must be 0–1; "
                f"out-of-range at indices {bad[:5]}"
                + (" …" if len(bad) > 5 else "")
            )
        return v

    @model_validator(mode="after")
    def lengths_must_match(self) -> "ECGBody":
        if len(self.ecg_lead_data) != len(self.signal_quality):
            raise ValueError(
                f"ecg_lead_data ({len(self.ecg_lead_data)}) and "
                f"signal_quality ({len(self.signal_quality)}) must have the same length"
            )
        return self

    @model_validator(mode="after")
    def ecg_physiological_check(self) -> "ECGBody":
        lo, hi = _ECG_AMPLITUDE_LIMITS.get(self.unit.value, (-10_000.0, 10_000.0))
        n = len(self.ecg_lead_data)
        outliers = [i for i, v in enumerate(self.ecg_lead_data) if not (lo <= v <= hi)]
        max_outliers = max(1, int(n * _ECG_OUTLIER_TOLERANCE))
        if len(outliers) > max_outliers:
            raise ValueError(
                f"ECG amplitude outside physiological range [{lo:.0f}, {hi:.0f}] {self.unit.value}: "
                f"{len(outliers)} samples exceed limits (tolerance: {max_outliers}). "
                "Check electrode placement or amplifier gain."
            )
        mean_val = sum(self.ecg_lead_data) / n if n else 0.0
        if abs(mean_val) > _ECG_BASELINE_WANDER_RATIO * hi:
            raise ValueError(
                f"ECG baseline wander detected: signal mean={mean_val:.2f} {self.unit.value} "
                f"exceeds {int(_ECG_BASELINE_WANDER_RATIO * 100)}% of max amplitude. "
                "Apply a high-pass filter (≥0.05 Hz) before upload."
            )
        return self


class BCGBody(BaseModel):
    bcg_sensor_readings: List[float] = Field(..., min_length=1)
    unit: BCGUnit
    effective_time_frame: EffectiveTimeFrame
    error_message: Optional[str] = None

    @field_validator("bcg_sensor_readings")
    @classmethod
    def bcg_data_finite(cls, v: List[float]) -> List[float]:
        _validate_no_nan_inf(v, "bcg_sensor_readings")
        return _validate_sample_count(v, "bcg_sensor_readings")


class GenericBody(BaseModel):
    signal_data: List[float] = Field(..., min_length=1)
    unit: str
    effective_time_frame: EffectiveTimeFrame

    @field_validator("signal_data")
    @classmethod
    def signal_data_finite(cls, v: List[float]) -> List[float]:
        _validate_no_nan_inf(v, "signal_data")
        return _validate_sample_count(v, "signal_data")


class BatchUploadRequest(BaseModel):
    header: BatchHeader
    body: dict
    signal_type: SignalType
    patient_id: str = Field(..., min_length=1, max_length=255)
    device_id: str = Field(..., min_length=1, max_length=255)
    idempotency_key: Optional[str] = None
    checksum: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None   # set when expanded from a session batch

    def get_validated_body(self):
        if self.signal_type == SignalType.ECG:
            return ECGBody(**self.body)
        elif self.signal_type == SignalType.BCG:
            return BCGBody(**self.body)
        else:
            return GenericBody(**self.body)

    def get_samples(self):
        if self.signal_type == SignalType.ECG:
            return self.body.get("ecg_lead_data", [])
        elif self.signal_type == SignalType.BCG:
            return self.body.get("bcg_sensor_readings", [])
        else:
            return self.body.get("signal_data", [])

    def get_units(self):
        return self.body.get("unit", "unknown")

    def get_start_datetime(self):
        raw = (self.body
               .get("effective_time_frame", {})
               .get("time_interval", {})
               .get("start_date_time", ""))
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)

    def get_end_datetime(self):
        raw = (self.body
               .get("effective_time_frame", {})
               .get("time_interval", {})
               .get("end_date_time", ""))
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)

    def get_sampling_rate(self):
        if self.header.acquisition_rate:
            return self.header.acquisition_rate.value
        try:
            samples = self.get_samples()
            duration = (self.get_end_datetime() - self.get_start_datetime()).total_seconds()
            if duration > 0 and samples:
                return round(len(samples) / duration, 2)
        except Exception:
            pass
        return 0.0

    def get_signal_info(self):
        info_map = {
            SignalType.ECG: self.header.ecg_info,
            SignalType.BCG: self.header.bcg_info,
            SignalType.PPG: self.header.ppg_info,
            SignalType.EEG: self.header.eeg_info,
            SignalType.EDA: self.header.eda_info,
            SignalType.EMG: self.header.emg_info,
            SignalType.EOG: self.header.eog_info,
            SignalType.ICG: self.header.icg_info,
        }
        info = info_map.get(self.signal_type)
        return info.model_dump(exclude_none=True) if info else {}

    @model_validator(mode="after")
    def sample_count_coherence(self) -> "BatchUploadRequest":
        if not self.header.acquisition_rate:
            return self
        try:
            samples = self.get_samples()
            duration = (self.get_end_datetime() - self.get_start_datetime()).total_seconds()
        except Exception:
            return self
        if duration <= 0 or not samples:
            return self
        # Skip coherence check for small test uploads (< 50 samples)
        if len(samples) < 50:
            return self
        expected = self.header.acquisition_rate.value * duration
        tolerance = max(_SAMPLE_COUNT_TOLERANCE_PCT * expected, _SAMPLE_COUNT_MIN_TOLERANCE)
        delta = abs(len(samples) - expected)
        if delta > tolerance:
            raise ValueError(
                f"Sample count coherence check failed: expected ≈{expected:.0f} samples "
                f"({self.header.acquisition_rate.value} Hz × {duration:.1f}s), "
                f"got {len(samples)} (tolerance ±{tolerance:.1f})"
            )
        return self


class BatchUploadListRequest(BaseModel):
    batches: List[BatchUploadRequest] = Field(..., min_length=1)


class SessionHeader(BaseModel):
    """BatchHeader fields that stay constant for a stream."""
    schema_id: str = Field(..., min_length=1, max_length=100)
    acquisition_rate: AcquisitionRate
    modality: Optional[Modality] = None
    device_identity: Optional[DeviceIdentity] = None
    ecg_info: Optional[ECGInfo] = None
    bcg_info: Optional[BCGInfo] = None
    ppg_info: Optional[PPGInfo] = None
    eeg_info: Optional[EEGInfo] = None
    eda_info: Optional[EDAInfo] = None
    emg_info: Optional[EMGInfo] = None
    eog_info: Optional[EOGInfo] = None
    icg_info: Optional[ICGInfo] = None


class SessionCreateRequest(BaseModel):
    """Registered once per stream (POST /data/session). Batches then send
    {"session_id", "body", "idempotency_key", "checksum"} only."""
    session_id: Optional[str] = None   # client-chosen UUID makes registration retry-safe
    patient_id: str = Field(..., min_length=1, max_length=255)
    device_id: str = Field(..., min_length=1, max_length=255)
    signal_type: SignalType
    unit: str = Field(..., min_length=1, max_length=20)
    header: SessionHeader
    project_id: Optional[str] = None

    @field_validator("session_id")
    @classmethod
    def session_id_must_be_uuid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _UUID_RE.match(v):
            raise ValueError(f"session_id must be a valid UUID, got {v!r}")
        return v

    @model_validator(mode="after")
    def unit_matches_signal_type(self) -> "SessionCreateRequest":
        allowed = {SignalType.ECG: ECGUnit, SignalType.BCG: BCGUnit}.get(self.signal_type)
        if allowed and self.unit not in {u.value for u in allowed}:
            raise ValueError(f"unit {self.unit!r} is not valid for {self.signal_type.value}")
        return self


class SessionResponse(BaseModel):
    session_id: str
    project_id: str
    status: str   # "created" | "existing"


class BatchResult(BaseModel):
    batch_uuid: str
    status: str
    sample_count: int
    message: Optional[str] = None
    overlap_warning: Optional[str] = None


class BatchUploadListResponse(BaseModel):
    results: List[BatchResult]


class BatchUploadResponse(BaseModel):
    batch_uuid: str
    status: str = "ingested"
    message: str
