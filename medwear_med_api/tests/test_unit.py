"""
Unit tests — no server, no database, no InfluxDB required.
Run: pytest tests/test_unit.py -v
"""
import os
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Environment stubs — must be set before importing project modules
# ---------------------------------------------------------------------------
os.environ.setdefault("POSTGRES_USER",           "test_user")
os.environ.setdefault("POSTGRES_PASSWORD",        "test_pass")
os.environ.setdefault("INFLUX_TOKEN",             "test-influx-token")
os.environ.setdefault("MINIO_API_ACCESS_KEY",     "test-access")
os.environ.setdefault("MINIO_API_SECRET_KEY",     "test-secret")
os.environ.setdefault("KEYCLOAK_URL",             "http://localhost:8080")
os.environ.setdefault("KEYCLOAK_REALM",           "medwear")
os.environ.setdefault("KEYCLOAK_CLIENT_ID",       "med-api")
os.environ.setdefault("KEYCLOAK_CLIENT_SECRET",   "test-secret")
os.environ.setdefault("KEYCLOAK_ADMIN",           "admin")
os.environ.setdefault("KEYCLOAK_ADMIN_PASSWORD",  "admin-pass")


# ===========================================================================
# influx_reader — input sanitisation
# ===========================================================================

class TestFluxSanitisation:
    def setup_method(self):
        from medwear_med_api.influx_reader import _flux_str, _require_uuid, _require_safe
        self.flux_str      = _flux_str
        self.require_uuid  = _require_uuid
        self.require_safe  = _require_safe

    # --- _flux_str ---
    def test_flux_str_passthrough(self):
        assert self.flux_str("normal-value") == "normal-value"

    def test_flux_str_escapes_double_quote(self):
        assert self.flux_str('say "hi"') == 'say \\"hi\\"'

    def test_flux_str_escapes_backslash(self):
        assert self.flux_str("a\\b") == "a\\\\b"

    def test_flux_str_escapes_both(self):
        result = self.flux_str('a\\"b')
        assert '\\"' in result

    # --- _require_uuid ---
    def test_require_uuid_valid(self):
        uid = "123e4567-e89b-12d3-a456-426614174000"
        assert self.require_uuid(uid) == uid

    def test_require_uuid_uppercase_valid(self):
        uid = "123E4567-E89B-12D3-A456-426614174000"
        assert self.require_uuid(uid) == uid

    def test_require_uuid_invalid_raises(self):
        with pytest.raises(ValueError):
            self.require_uuid("not-a-uuid")

    def test_require_uuid_injection_raises(self):
        with pytest.raises(ValueError):
            self.require_uuid('"; DROP TABLE batches; --')

    # --- _require_safe ---
    def test_require_safe_alphanumeric(self):
        assert self.require_safe("ECG") == "ECG"

    def test_require_safe_underscore_hyphen(self):
        assert self.require_safe("signal_type-v2") == "signal_type-v2"

    def test_require_safe_injection_raises(self):
        with pytest.raises(ValueError):
            self.require_safe('ecg"; DROP TABLE--')

    def test_require_safe_space_raises(self):
        with pytest.raises(ValueError):
            self.require_safe("ecg type")


# ===========================================================================
# db — project name validation
# ===========================================================================

class TestProjectNameValidation:
    def setup_method(self):
        from medwear_med_api.db import _validate_project_name
        self.validate = _validate_project_name

    def test_valid_name(self):
        self.validate("medwear_project1")

    def test_valid_letters_only(self):
        self.validate("projectalpha")

    def test_valid_mixed(self):
        self.validate("Project_123")

    def test_rejects_starts_with_digit(self):
        with pytest.raises(ValueError):
            self.validate("1badname")

    def test_rejects_hyphen(self):
        with pytest.raises(ValueError):
            self.validate("bad-name")

    def test_rejects_semicolon_injection(self):
        with pytest.raises(ValueError):
            self.validate('project1"; DROP DATABASE postgres;--')

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            self.validate("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError):
            self.validate("a" * 64)


# ===========================================================================
# auth — JWKS caching
# ===========================================================================

class TestJwksCache:
    def setup_method(self):
        import medwear_med_api.auth as auth_module
        # Reset cache before each test
        auth_module._jwks_cache["keys"] = None
        auth_module._jwks_cache["expires_at"] = 0.0
        self.auth = auth_module

    def test_cache_populated_on_first_call(self):
        fake_keys = {"keys": [{"kid": "test"}]}
        mock_response = MagicMock()
        mock_response.json.return_value = fake_keys

        with patch("medwear_med_api.auth.httpx.get", return_value=mock_response) as mock_get:
            result = self.auth.get_jwks()
            assert result == fake_keys
            assert mock_get.call_count == 1

    def test_cache_not_refetched_within_ttl(self):
        fake_keys = {"keys": [{"kid": "cached"}]}
        mock_response = MagicMock()
        mock_response.json.return_value = fake_keys

        with patch("medwear_med_api.auth.httpx.get", return_value=mock_response) as mock_get:
            self.auth.get_jwks()
            self.auth.get_jwks()
            self.auth.get_jwks()
            assert mock_get.call_count == 1  # fetched only once

    def test_cache_refreshed_after_ttl(self):
        fake_keys = {"keys": [{"kid": "fresh"}]}
        mock_response = MagicMock()
        mock_response.json.return_value = fake_keys

        with patch("medwear_med_api.auth.httpx.get", return_value=mock_response) as mock_get:
            self.auth.get_jwks()               # populates cache (count=1)
            # Expire the cache so next call must refetch
            self.auth._jwks_cache["expires_at"] = time.monotonic() - 1
            self.auth.get_jwks()               # expired → refetches (count=2)
            assert mock_get.call_count == 2


# ===========================================================================
# admin — OperationStatus enum serialisation
# ===========================================================================

class TestOperationStatus:
    def setup_method(self):
        from medwear_med_api.routers.admin import OperationStatus
        self.OperationStatus = OperationStatus

    def test_values_are_strings(self):
        assert self.OperationStatus.ASSIGNED   == "assigned"
        assert self.OperationStatus.REMOVED    == "removed"
        assert self.OperationStatus.CREATED    == "created"
        assert self.OperationStatus.SOFT_DELETED == "soft_deleted"
        assert self.OperationStatus.RESTORED   == "restored"
        assert self.OperationStatus.PASSWORD_CHANGED == "password_changed"

    def test_is_str_subclass(self):
        assert isinstance(self.OperationStatus.ASSIGNED, str)

    def test_json_serialisable(self):
        import json
        result = json.dumps({"status": self.OperationStatus.CREATED})
        assert '"created"' in result


# ===========================================================================
# audit — get_user_projects logic
# ===========================================================================

class TestGetUserProjects:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_match(self):
        from medwear_med_api.audit import get_user_projects

        mock_row = MagicMock()
        mock_row.fetchone.return_value = None

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_row
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("medwear_med_api.audit.get_all_projects", AsyncMock(return_value=["proj1"])), \
             patch("medwear_med_api.audit.get_project_session", return_value=mock_session):
            result = await get_user_projects("unknown_user")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_matched_projects(self):
        from medwear_med_api.audit import get_user_projects

        mock_row = MagicMock()
        mock_row.fetchone.return_value = ("proj1",)

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_row
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("medwear_med_api.audit.get_all_projects", AsyncMock(return_value=["proj1", "proj2"])), \
             patch("medwear_med_api.audit.get_project_session", return_value=mock_session):
            result = await get_user_projects("alice")

        assert "proj1" in result
        assert "proj2" in result

    @pytest.mark.asyncio
    async def test_raises_on_get_all_projects_failure(self):
        from medwear_med_api.audit import get_user_projects

        with patch("medwear_med_api.audit.get_all_projects", AsyncMock(side_effect=RuntimeError("DB down"))):
            with pytest.raises(RuntimeError, match="DB down"):
                await get_user_projects("alice")

    @pytest.mark.asyncio
    async def test_raises_when_all_project_queries_fail(self):
        from medwear_med_api.audit import get_user_projects

        mock_session = AsyncMock()
        mock_session.execute.side_effect = Exception("connection refused")
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("medwear_med_api.audit.get_all_projects", AsyncMock(return_value=["proj1"])), \
             patch("medwear_med_api.audit.get_project_session", return_value=mock_session):
            with pytest.raises(RuntimeError, match="could not be queried"):
                await get_user_projects("alice")


# ===========================================================================
# influx_reader — hardcoded token removed
# ===========================================================================

class TestInfluxTokenRequired:
    def test_module_loads_with_token_set(self):
        # Token is set via os.environ at top of file — import should succeed
        import importlib
        import medwear_med_api.influx_reader as m
        importlib.reload(m)  # reload to re-evaluate module-level check
        assert m.INFLUX_TOKEN == "test-influx-token"

    def test_module_raises_without_token(self):
        import importlib
        import medwear_med_api.influx_reader as m

        original = os.environ.pop("INFLUX_TOKEN", None)
        try:
            with pytest.raises(RuntimeError, match="INFLUX_TOKEN"):
                importlib.reload(m)
        finally:
            if original is not None:
                os.environ["INFLUX_TOKEN"] = original
            importlib.reload(m)  # restore


# ===========================================================================
# Request ID middleware
# ===========================================================================

class TestRequestIdMiddleware:
    @pytest.mark.asyncio
    async def test_request_id_added_to_response(self):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.middleware.base import BaseHTTPMiddleware

        async def homepage(request):
            return JSONResponse({"ok": True})

        async def _request_id_middleware(request, call_next):
            import uuid as _uuid
            request_id = request.headers.get("X-Request-ID") or str(_uuid.uuid4())
            request.state.request_id = request_id
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response

        app = Starlette(routes=[Route("/", homepage)])
        app.add_middleware(BaseHTTPMiddleware, dispatch=_request_id_middleware)

        client = TestClient(app)
        response = client.get("/")
        assert "X-Request-ID" in response.headers
        assert len(response.headers["X-Request-ID"]) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_request_id_propagated_from_header(self):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.middleware.base import BaseHTTPMiddleware

        async def homepage(request):
            return JSONResponse({"ok": True})

        async def _request_id_middleware(request, call_next):
            import uuid as _uuid
            request_id = request.headers.get("X-Request-ID") or str(_uuid.uuid4())
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response

        app = Starlette(routes=[Route("/", homepage)])
        app.add_middleware(BaseHTTPMiddleware, dispatch=_request_id_middleware)

        client = TestClient(app)
        response = client.get("/", headers={"X-Request-ID": "my-custom-id"})
        assert response.headers["X-Request-ID"] == "my-custom-id"


# ===========================================================================
# circuit_breaker — state machine
# ===========================================================================

class TestCircuitBreaker:
    def _make(self, threshold=3, timeout=60):
        from medwear_med_api.circuit_breaker import CircuitBreaker
        return CircuitBreaker("test", failure_threshold=threshold, recovery_timeout=timeout)

    def test_starts_closed(self):
        cb = self._make()
        assert cb.state == "closed"

    def test_successful_call_stays_closed(self):
        cb = self._make()
        result = cb.call(lambda: 42)
        assert result == 42
        assert cb.state == "closed"

    def test_opens_after_threshold_failures(self):
        from medwear_med_api.circuit_breaker import CircuitOpenError
        cb = self._make(threshold=3)
        for _ in range(3):
            try:
                cb.call(_raise)
            except RuntimeError:
                pass
        assert cb.state == "open"

    def test_open_rejects_immediately(self):
        from medwear_med_api.circuit_breaker import CircuitBreaker, CircuitOpenError
        cb = self._make(threshold=1)
        try:
            cb.call(_raise)
        except RuntimeError:
            pass
        assert cb.state == "open"
        with pytest.raises(CircuitOpenError):
            cb.call(lambda: None)

    def test_transitions_to_half_open_after_timeout(self):
        import time as _time
        from unittest.mock import patch
        from medwear_med_api.circuit_breaker import CircuitBreaker
        cb = self._make(threshold=1, timeout=30)
        try:
            cb.call(_raise)
        except RuntimeError:
            pass
        # Simulate timeout elapsed
        with patch("medwear_med_api.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb._opened_at + 31
            assert cb.state == "half_open"

    def test_recovery_closes_circuit(self):
        from medwear_med_api.circuit_breaker import CircuitBreaker, CircuitOpenError
        cb = self._make(threshold=1, timeout=0)
        try:
            cb.call(_raise)
        except RuntimeError:
            pass
        # Force half-open by making timeout=0 (already elapsed)
        cb._opened_at = 0.0
        cb.call(lambda: "ok")
        assert cb.state == "closed"


def _raise():
    raise RuntimeError("simulated failure")


# ===========================================================================
# logging_config — TraceIdFilter
# ===========================================================================

class TestTraceIdFilter:
    def setup_method(self):
        from medwear_med_api.logging_config import TraceIdFilter, set_trace_id, _TRACE_ID
        self.filter = TraceIdFilter()
        self.set_trace_id = set_trace_id
        self._TRACE_ID = _TRACE_ID

    def _make_record(self):
        import logging
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="hello", args=(), exc_info=None,
        )
        return record

    def test_default_trace_id_is_dash(self):
        self._TRACE_ID.set("-")
        record = self._make_record()
        self.filter.filter(record)
        assert record.trace_id == "-"

    def test_set_trace_id_propagates_to_log_record(self):
        self.set_trace_id("abc-123")
        record = self._make_record()
        self.filter.filter(record)
        assert record.trace_id == "abc-123"

    def test_different_context_has_independent_trace_id(self):
        import contextvars
        self.set_trace_id("outer")

        inner_id = None

        def _run():
            nonlocal inner_id
            self.set_trace_id("inner")
            r = self._make_record()
            self.filter.filter(r)
            inner_id = r.trace_id

        ctx = contextvars.copy_context()
        ctx.run(_run)
        # outer context unchanged
        record = self._make_record()
        self.filter.filter(record)
        assert record.trace_id == "outer"
        assert inner_id == "inner"


# ===========================================================================
# metrics — module loads, counters are singletons
# ===========================================================================

class TestMetrics:
    def test_counters_importable(self):
        from medwear_med_api.metrics import (
            BATCH_INGESTED, INFLUX_WRITE_TOTAL,
            INFLUX_WRITE_DURATION, CIRCUIT_BREAKER_OPEN,
        )
        assert BATCH_INGESTED is not None

    def test_influx_write_total_labels(self):
        from medwear_med_api.metrics import INFLUX_WRITE_TOTAL
        before = INFLUX_WRITE_TOTAL.labels(outcome="success")._value.get()
        INFLUX_WRITE_TOTAL.labels(outcome="success").inc()
        after = INFLUX_WRITE_TOTAL.labels(outcome="success")._value.get()
        assert after == before + 1

    def test_batch_ingested_labels(self):
        from medwear_med_api.metrics import BATCH_INGESTED
        BATCH_INGESTED.labels(signal_type="ECG", project_id="proj1").inc()
        val = BATCH_INGESTED.labels(signal_type="ECG", project_id="proj1")._value.get()
        assert val >= 1


# ===========================================================================
# tracing — init with OTEL_TRACES_EXPORTER=none is always safe
# ===========================================================================

class TestTracingInit:
    def test_noop_when_disabled(self):
        from unittest.mock import MagicMock
        from medwear_med_api.tracing import init_tracing
        app = MagicMock()
        result = init_tracing(app)
        assert result is False  # OTEL_TRACES_EXPORTER defaults to none

    def test_get_tracer_returns_noop_without_otel(self):
        import importlib, sys
        # Temporarily hide opentelemetry to simulate missing package
        otel = sys.modules.get("opentelemetry")
        sys.modules["opentelemetry"] = None  # type: ignore
        try:
            import medwear_med_api.tracing as tracing_mod
            importlib.reload(tracing_mod)
            tracer = tracing_mod.get_tracer("test")
            assert hasattr(tracer, "start_as_current_span")
        finally:
            if otel is not None:
                sys.modules["opentelemetry"] = otel
            else:
                sys.modules.pop("opentelemetry", None)
            importlib.reload(tracing_mod)


# ===========================================================================
# influx_writer — retry logic
# ===========================================================================

class TestInfluxWriterRetry:
    def test_succeeds_on_first_attempt(self):
        from unittest.mock import patch, MagicMock
        from medwear_med_api import influx_writer

        mock_cb = MagicMock()
        mock_cb.call.return_value = None
        mock_cb.state = "closed"

        import datetime
        with patch.object(influx_writer, "_influx_cb", mock_cb):
            result = influx_writer.write_signal_batch(
                batch_uuid="test-uuid", patient_id="1", device_id="dev",
                signal_type="ECG", samples=[0.1, 0.2, 0.3],
                start_timestamp=datetime.datetime.utcnow(), sampling_rate_hz=128.0,
            )
        assert result == 3
        assert mock_cb.call.call_count == 1

    def test_retries_on_transient_failure(self):
        from unittest.mock import patch, MagicMock, call
        from medwear_med_api import influx_writer
        import datetime

        call_count = {"n": 0}

        def side_effect(func, points):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise ConnectionError("transient")
            # Third attempt succeeds
            return None

        mock_cb = MagicMock()
        mock_cb.call.side_effect = side_effect

        with patch.object(influx_writer, "_influx_cb", mock_cb), \
             patch.object(influx_writer, "_RETRY_BASE_DELAY", 0):  # no sleep in tests
            result = influx_writer.write_signal_batch(
                batch_uuid="uuid2", patient_id="2", device_id="dev",
                signal_type="PPG", samples=[1.0],
                start_timestamp=datetime.datetime.utcnow(), sampling_rate_hz=64.0,
            )
        assert result == 1
        assert call_count["n"] == 3

    def test_raises_after_max_retries(self):
        from unittest.mock import patch, MagicMock
        from medwear_med_api import influx_writer
        import datetime

        mock_cb = MagicMock()
        mock_cb.call.side_effect = IOError("persistent failure")

        with patch.object(influx_writer, "_influx_cb", mock_cb), \
             patch.object(influx_writer, "_RETRY_BASE_DELAY", 0):
            with pytest.raises(IOError, match="persistent failure"):
                influx_writer.write_signal_batch(
                    batch_uuid="uuid3", patient_id="3", device_id="dev",
                    signal_type="ECG", samples=[1.0],
                    start_timestamp=datetime.datetime.utcnow(), sampling_rate_hz=128.0,
                )


# ===========================================================================
# schemas — Pydantic data quality validators
# ===========================================================================

class TestSchemaValidators:
    _NOW = "2026-04-28T10:00:00Z"
    _END = "2026-04-28T10:01:00Z"  # 60 seconds later

    def _valid_header(self, **kwargs):
        base = {
            "uuid": "123e4567-e89b-12d3-a456-426614174000",
            "schema_id": "test-schema-v1",
            "source_creation_date_time": self._NOW,
        }
        base.update(kwargs)
        return base

    def _valid_ecg_body(self, samples=None):
        if samples is None:
            samples = [0.1] * 100
        return {
            "ecg_lead_data":    samples,
            "signal_quality":   [0.9] * len(samples),
            "unit":             "uV",
            "effective_time_frame": {
                "time_interval": {
                    "start_date_time": self._NOW,
                    "end_date_time":   self._END,
                }
            },
        }

    # --- UUID validation ---
    def test_invalid_uuid_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import BatchHeader
        with pytest.raises(ValidationError, match="uuid"):
            BatchHeader(**self._valid_header(uuid="not-a-valid-uuid"))

    def test_valid_uuid_accepted(self):
        from medwear_med_api.schemas import BatchHeader
        h = BatchHeader(**self._valid_header())
        assert h.uuid == "123e4567-e89b-12d3-a456-426614174000"

    # --- Future timestamp ---
    def test_far_future_timestamp_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import BatchHeader
        with pytest.raises(ValidationError, match="future"):
            BatchHeader(**self._valid_header(source_creation_date_time="2099-01-01T00:00:00Z"))

    def test_unparseable_timestamp_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import BatchHeader
        with pytest.raises(ValidationError):
            BatchHeader(**self._valid_header(source_creation_date_time="not-a-date"))

    # --- NaN / Inf rejection ---
    def test_nan_in_ecg_data_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import ECGBody
        body = self._valid_ecg_body(samples=[0.1, float("nan"), 0.3])
        body["signal_quality"] = [0.9] * 3
        with pytest.raises(ValidationError, match="non-finite"):
            ECGBody(**body)

    def test_inf_in_ecg_data_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import ECGBody
        body = self._valid_ecg_body(samples=[float("inf"), 0.1])
        body["signal_quality"] = [0.9] * 2
        with pytest.raises(ValidationError, match="non-finite"):
            ECGBody(**body)

    # --- Signal quality range ---
    def test_signal_quality_out_of_range_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import ECGBody
        body = self._valid_ecg_body()
        body["signal_quality"] = [1.5] * 100
        with pytest.raises(ValidationError, match="0"):
            ECGBody(**body)

    # --- patient_id non-empty string ---
    def test_patient_id_empty_string_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import BatchUploadRequest
        with pytest.raises(ValidationError):
            BatchUploadRequest(
                header=self._valid_header(),
                body=self._valid_ecg_body(),
                signal_type="ECG",
                patient_id="",
                device_id="dev-001",
            )

    def test_patient_id_string_accepted(self):
        from medwear_med_api.schemas import BatchUploadRequest
        req = BatchUploadRequest(
            header=self._valid_header(),
            body=self._valid_ecg_body(),
            signal_type="ECG",
            patient_id="somnomat_sub0102",
            device_id="dev-001",
        )
        assert req.patient_id == "somnomat_sub0102"

    # --- AcquisitionRate.value > 0 ---
    def test_acquisition_rate_zero_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import AcquisitionRate
        with pytest.raises(ValidationError):
            AcquisitionRate(value=0.0)

    def test_acquisition_rate_negative_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import AcquisitionRate
        with pytest.raises(ValidationError):
            AcquisitionRate(value=-10.0)

    # --- TimeInterval: end must be after start ---
    def test_time_interval_end_before_start_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import TimeInterval
        with pytest.raises(ValidationError, match="after"):
            TimeInterval(
                start_date_time="2026-04-28T10:01:00Z",
                end_date_time="2026-04-28T10:00:00Z",
            )

    def test_time_interval_equal_times_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import TimeInterval
        with pytest.raises(ValidationError, match="after"):
            TimeInterval(
                start_date_time="2026-04-28T10:00:00Z",
                end_date_time="2026-04-28T10:00:00Z",
            )

    # --- Sample count coherence ---
    def test_sample_count_coherence_passes_when_correct(self):
        from medwear_med_api.schemas import BatchUploadRequest
        # 128 Hz × 60 s = 7680 samples (within 5% tolerance)
        body = self._valid_ecg_body(samples=[0.1] * 7680)
        req = BatchUploadRequest(
            header={**self._valid_header(), "acquisition_rate": {"value": 128.0, "unit": "Hz"}},
            body=body,
            signal_type="ECG",
            patient_id="1",
            device_id="dev-001",
        )
        assert req is not None

    def test_sample_count_coherence_fails_when_far_off(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import BatchUploadRequest
        # 128 Hz × 60 s = 7680 expected, providing only 100 → way outside tolerance
        body = self._valid_ecg_body(samples=[0.1] * 100)
        with pytest.raises(ValidationError, match="coherence"):
            BatchUploadRequest(
                header={**self._valid_header(), "acquisition_rate": {"value": 128.0, "unit": "Hz"}},
                body=body,
                signal_type="ECG",
                patient_id="1",
                device_id="dev-001",
            )

    def test_sample_count_coherence_skipped_without_rate(self):
        from medwear_med_api.schemas import BatchUploadRequest
        # No acquisition_rate — coherence check is skipped
        req = BatchUploadRequest(
            header=self._valid_header(),
            body=self._valid_ecg_body(samples=[0.1] * 10),
            signal_type="ECG",
            patient_id="1",
            device_id="dev-001",
        )
        assert req is not None
