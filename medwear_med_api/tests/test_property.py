"""
Property-based tests using Hypothesis.
======================================
These tests generate random inputs automatically and find edge cases
that hand-written unit tests miss.

Run:
  pytest tests/test_property.py -v
  pytest tests/test_property.py -v --hypothesis-seed=0   # reproducible
"""
import math
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import assume, given, settings, HealthCheck
from hypothesis import strategies as st

# ── env stubs (same as test_unit.py) ─────────────────────────────────────────
os.environ.setdefault("POSTGRES_USER",          "test_user")
os.environ.setdefault("POSTGRES_PASSWORD",       "test_pass")
os.environ.setdefault("INFLUX_TOKEN",            "test-influx-token")
os.environ.setdefault("MINIO_API_ACCESS_KEY",    "test-access")
os.environ.setdefault("MINIO_API_SECRET_KEY",    "test-secret")
os.environ.setdefault("KEYCLOAK_URL",            "http://localhost:8080")
os.environ.setdefault("KEYCLOAK_REALM",          "medwear")
os.environ.setdefault("KEYCLOAK_CLIENT_ID",      "med-api")
os.environ.setdefault("KEYCLOAK_CLIENT_SECRET",  "test-secret")
os.environ.setdefault("KEYCLOAK_ADMIN",          "admin")
os.environ.setdefault("KEYCLOAK_ADMIN_PASSWORD", "admin-pass")

# ── Shared strategies ─────────────────────────────────────────────────────────

_PAST_DT = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2026, 1, 1),
    timezones=st.just(timezone.utc),
)

_VALID_UUID = st.uuids(version=4).map(str)

_FINITE_FLOAT = st.floats(
    min_value=-9_000.0, max_value=9_000.0,
    allow_nan=False, allow_infinity=False,
)

_QUALITY_FLOAT = st.floats(
    min_value=0.0, max_value=1.0,
    allow_nan=False, allow_infinity=False,
)

_SMALL_SAMPLES = st.lists(_FINITE_FLOAT, min_size=1, max_size=50)


def _make_time_interval(start: datetime, duration_s: float = 1.0) -> dict:
    end = start + timedelta(seconds=duration_s)
    return {
        "time_interval": {
            "start_date_time": start.isoformat(),
            "end_date_time":   end.isoformat(),
        }
    }


# ── TestSchemaValidatorsProperty ─────────────────────────────────────────────

class TestECGBodyProperty:
    """Hypothesis tests for ECGBody schema."""

    @given(samples=_SMALL_SAMPLES, start=_PAST_DT)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_finite_ecg_always_accepted(self, samples: list, start: datetime):
        """Any list of finite floats in the uV physiological range must not
        raise a NaN/Inf or amplitude error."""
        from medwear_med_api.schemas import ECGBody
        from pydantic import ValidationError

        quality = [0.9] * len(samples)
        try:
            ECGBody(
                ecg_lead_data    = samples,
                signal_quality   = quality,
                unit             = "uV",
                effective_time_frame = {"time_interval": {
                    "start_date_time": start.isoformat(),
                    "end_date_time":   (start + timedelta(seconds=1)).isoformat(),
                }},
            )
        except ValidationError as exc:
            msgs = str(exc)
            # Only physiological / coherence errors are acceptable here
            assert any(kw in msgs for kw in (
                "physiological", "baseline", "coherence", "length",
            )), f"Unexpected validation error for finite samples: {msgs}"

    @given(
        nan_idx=st.integers(min_value=0, max_value=9),
        value=st.just(float("nan")),
        start=_PAST_DT,
    )
    @settings(max_examples=50)
    def test_nan_always_rejected(self, nan_idx: int, value: float, start: datetime):
        """NaN anywhere in ecg_lead_data must always raise ValidationError."""
        from medwear_med_api.schemas import ECGBody
        from pydantic import ValidationError

        samples = [0.1] * 10
        samples[nan_idx] = value
        with pytest.raises(ValidationError, match="non-finite"):
            ECGBody(
                ecg_lead_data    = samples,
                signal_quality   = [0.9] * 10,
                unit             = "uV",
                effective_time_frame = {"time_interval": {
                    "start_date_time": start.isoformat(),
                    "end_date_time":   (start + timedelta(seconds=1)).isoformat(),
                }},
            )

    @given(bad_q=st.floats(min_value=1.001, max_value=100.0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=100)
    def test_signal_quality_above_1_always_rejected(self, bad_q: float):
        """signal_quality > 1.0 must always raise ValidationError."""
        from medwear_med_api.schemas import ECGBody
        from pydantic import ValidationError

        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValidationError):
            ECGBody(
                ecg_lead_data    = [0.1],
                signal_quality   = [bad_q],
                unit             = "uV",
                effective_time_frame = {"time_interval": {
                    "start_date_time": start.isoformat(),
                    "end_date_time":   (start + timedelta(seconds=1)).isoformat(),
                }},
            )

    @given(bad_q=st.floats(max_value=-0.001, allow_nan=False, allow_infinity=False))
    @settings(max_examples=100)
    def test_signal_quality_below_0_always_rejected(self, bad_q: float):
        """signal_quality < 0.0 must always raise ValidationError."""
        from medwear_med_api.schemas import ECGBody
        from pydantic import ValidationError

        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValidationError):
            ECGBody(
                ecg_lead_data    = [0.1],
                signal_quality   = [bad_q],
                unit             = "uV",
                effective_time_frame = {"time_interval": {
                    "start_date_time": start.isoformat(),
                    "end_date_time":   (start + timedelta(seconds=1)).isoformat(),
                }},
            )


class TestTimeIntervalProperty:
    """Hypothesis tests for TimeInterval schema."""

    @given(
        start=_PAST_DT,
        delta=st.integers(min_value=1, max_value=3600),
    )
    @settings(max_examples=200)
    def test_end_after_start_always_accepted(self, start: datetime, delta: int):
        """end = start + Δ (Δ > 0) must always be accepted."""
        from medwear_med_api.schemas import TimeInterval

        end = start + timedelta(seconds=delta)
        ti  = TimeInterval(
            start_date_time=start.isoformat(),
            end_date_time=end.isoformat(),
        )
        assert ti is not None

    @given(
        start=_PAST_DT,
        delta=st.integers(min_value=1, max_value=3600),
    )
    @settings(max_examples=200)
    def test_end_before_start_always_rejected(self, start: datetime, delta: int):
        """end = start − Δ (Δ > 0) must always be rejected."""
        from medwear_med_api.schemas import TimeInterval
        from pydantic import ValidationError

        end = start - timedelta(seconds=delta)
        with pytest.raises(ValidationError, match="after"):
            TimeInterval(
                start_date_time=start.isoformat(),
                end_date_time=end.isoformat(),
            )


class TestBatchHeaderProperty:
    """Hypothesis tests for BatchHeader UUID and timestamp validators."""

    @given(uid=_VALID_UUID, start=_PAST_DT)
    @settings(max_examples=200)
    def test_valid_uuid_always_accepted(self, uid: str, start: datetime):
        """Any UUID v4 must pass the uuid_must_be_valid validator."""
        from medwear_med_api.schemas import BatchHeader

        hdr = BatchHeader(
            uuid=uid,
            schema_id="test-v1",
            source_creation_date_time=start.isoformat(),
        )
        assert hdr.uuid == uid

    @given(garbage=st.text(min_size=1, max_size=40).filter(
        lambda s: not s.replace("-", "").isalnum() or len(s) != 36
    ))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
    def test_garbage_uuid_always_rejected(self, garbage: str):
        """Random non-UUID strings must always be rejected."""
        from medwear_med_api.schemas import BatchHeader
        from pydantic import ValidationError

        assume(not __import__("re").match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            garbage, __import__("re").IGNORECASE,
        ))
        with pytest.raises(ValidationError):
            BatchHeader(
                uuid=garbage,
                schema_id="test",
                source_creation_date_time="2025-01-01T00:00:00Z",
            )


class TestAcquisitionRateProperty:
    """Hypothesis tests for AcquisitionRate."""

    @given(rate=st.floats(min_value=0.001, max_value=50_000.0,
                          allow_nan=False, allow_infinity=False))
    @settings(max_examples=200)
    def test_positive_rate_always_accepted(self, rate: float):
        """Any strictly positive rate must be accepted."""
        from medwear_med_api.schemas import AcquisitionRate

        ar = AcquisitionRate(value=rate)
        assert ar.value == rate

    @given(rate=st.floats(max_value=0.0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=100)
    def test_non_positive_rate_always_rejected(self, rate: float):
        """Zero or negative rates must always be rejected."""
        from medwear_med_api.schemas import AcquisitionRate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AcquisitionRate(value=rate)


class TestBatchUploadRequestProperty:
    """Hypothesis tests for BatchUploadRequest patient_id field."""

    @given(pid=st.text(min_size=1, max_size=255))
    @settings(max_examples=100)
    def test_nonempty_patient_id_always_accepted(self, pid: str):
        """Any non-empty string patient_id must be accepted at the schema level."""
        from medwear_med_api.schemas import BatchUploadRequest

        start = datetime(2025, 6, 1, tzinfo=timezone.utc)
        end   = start + timedelta(seconds=1)
        req   = BatchUploadRequest(
            header={
                "uuid": "123e4567-e89b-12d3-a456-426614174000",
                "schema_id": "test",
                "source_creation_date_time": start.isoformat(),
            },
            body={
                "ecg_lead_data":  [0.1],
                "signal_quality": [0.9],
                "unit": "uV",
                "effective_time_frame": {"time_interval": {
                    "start_date_time": start.isoformat(),
                    "end_date_time":   end.isoformat(),
                }},
            },
            signal_type="ECG",
            patient_id=pid,
            device_id="dev-001",
        )
        assert req.patient_id == pid

    def test_empty_patient_id_always_rejected(self):
        """Empty string patient_id must raise ValidationError."""
        from medwear_med_api.schemas import BatchUploadRequest
        from pydantic import ValidationError

        start = datetime(2025, 6, 1, tzinfo=timezone.utc)
        end   = start + timedelta(seconds=1)
        with pytest.raises(ValidationError):
            BatchUploadRequest(
                header={
                    "uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "schema_id": "test",
                    "source_creation_date_time": start.isoformat(),
                },
                body={
                    "ecg_lead_data":  [0.1],
                    "signal_quality": [0.9],
                    "unit": "uV",
                    "effective_time_frame": {"time_interval": {
                        "start_date_time": start.isoformat(),
                        "end_date_time":   end.isoformat(),
                    }},
                },
                signal_type="ECG",
                patient_id="",
                device_id="dev-001",
            )
