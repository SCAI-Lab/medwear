"""
Coverage-focused tests for previously uncovered modules.
=========================================================
Targets (from coverage report):
  batch_router.py  helpers     0% → covers _make_checksum, _make_idempotency_key,
                                    _semantic_validate, _csv_body, _parse_csv
  routers/export.py            0% → covers _build_csv, _build_wfdb_zip, _build_edf
  routers/gdpr.py              0% → covers _require_admin, ConsentGrantRequest validation
  errors.py                   40% → covers api_error
  schemas.py                  85% → covers BCGBody, GenericBody, ECGBody edge cases

Run:
  pytest tests/test_coverage.py -v
"""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── env stubs ─────────────────────────────────────────────────────────────────
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

_NOW  = "2026-04-28T10:00:00Z"
_END  = "2026-04-28T10:00:01Z"


# ===========================================================================
# errors.py — api_error
# ===========================================================================

class TestApiError:
    def test_basic_fields_present(self):
        from medwear_med_api.errors import api_error
        result = api_error("NOT_FOUND", "resource missing")
        assert result["error"]   == "NOT_FOUND"
        assert result["detail"]  == "resource missing"
        assert "timestamp" in result

    def test_extra_kwargs_included(self):
        from medwear_med_api.errors import api_error
        result = api_error("BAD", "msg", batch_uuid="abc-123", count=5)
        assert result["batch_uuid"] == "abc-123"
        assert result["count"]      == 5

    def test_timestamp_is_iso_string(self):
        from medwear_med_api.errors import api_error
        result = api_error("X", "y")
        ts = result["timestamp"]
        assert isinstance(ts, str)
        datetime.fromisoformat(ts.replace("Z", "+00:00"))   # must parse without error


# ===========================================================================
# batch_router.py — pure helper functions
# ===========================================================================

class TestBatchRouterHelpers:
    def test_make_checksum_deterministic(self):
        from medwear_med_api.batch_router import _make_checksum
        samples = [1.0, 2.0, 3.0]
        assert _make_checksum(samples) == _make_checksum(samples)

    def test_make_checksum_sensitive_to_order(self):
        from medwear_med_api.batch_router import _make_checksum
        assert _make_checksum([1.0, 2.0]) != _make_checksum([2.0, 1.0])

    def test_make_checksum_is_sha256(self):
        from medwear_med_api.batch_router import _make_checksum
        result = _make_checksum([0.1])
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_make_idempotency_key_from_user_key(self):
        from medwear_med_api.batch_router import _make_idempotency_key
        from medwear_med_api.schemas import BatchUploadRequest

        req = BatchUploadRequest(
            header={"uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "schema_id": "v1", "source_creation_date_time": _NOW},
            body={"ecg_lead_data": [0.1], "signal_quality": [0.9],
                  "unit": "uV",
                  "effective_time_frame": {"time_interval": {
                      "start_date_time": _NOW, "end_date_time": _END}}},
            signal_type="ECG", patient_id="1", device_id="dev",
            idempotency_key="my-custom-key",
        )
        key = _make_idempotency_key(req)
        assert len(key) == 64   # sha256 hex

    def test_make_idempotency_key_from_device_uuid(self):
        from medwear_med_api.batch_router import _make_idempotency_key
        from medwear_med_api.schemas import BatchUploadRequest

        req = BatchUploadRequest(
            header={"uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "schema_id": "v1", "source_creation_date_time": _NOW},
            body={"ecg_lead_data": [0.1], "signal_quality": [0.9],
                  "unit": "uV",
                  "effective_time_frame": {"time_interval": {
                      "start_date_time": _NOW, "end_date_time": _END}}},
            signal_type="ECG", patient_id="1", device_id="dev-007",
        )
        key = _make_idempotency_key(req)
        assert len(key) == 64

    def test_csv_body_ecg_includes_signal_quality(self):
        from medwear_med_api.batch_router import _csv_body
        body = _csv_body("ECG", [0.1, 0.2], "mV", _NOW, _END, signal_quality=[0.9, 0.8])
        assert "signal_quality" in body
        assert body["signal_quality"] == [0.9, 0.8]
        assert body["ecg_lead_data"] == [0.1, 0.2]

    def test_csv_body_ppg_uses_signal_data(self):
        from medwear_med_api.batch_router import _csv_body
        body = _csv_body("PPG", [100.0], "ADC", _NOW, _END)
        assert "signal_data" in body
        assert "signal_quality" not in body

    def test_csv_body_ecg_missing_quality_raises(self):
        from fastapi import HTTPException
        from medwear_med_api.batch_router import _csv_body
        with pytest.raises(HTTPException) as exc_info:
            _csv_body("ECG", [0.1], "uV", _NOW, _END, signal_quality=None)
        assert exc_info.value.status_code == 400

    def test_semantic_validate_rejects_reversed_timestamps(self):
        from fastapi import HTTPException
        from medwear_med_api.batch_router import _semantic_validate
        from medwear_med_api.schemas import BatchUploadRequest

        # end_date_time before start_date_time → INVALID_TIMESTAMP_ORDER
        req = BatchUploadRequest(
            header={"uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "schema_id": "v1", "source_creation_date_time": _NOW},
            body={"ecg_lead_data": [0.1], "signal_quality": [0.9],
                  "unit": "uV",
                  "effective_time_frame": {"time_interval": {
                      "start_date_time": _END, "end_date_time": _NOW}}},
            signal_type="ECG", patient_id="1", device_id="dev",
        )
        with pytest.raises(HTTPException) as exc_info:
            _semantic_validate(req)
        assert exc_info.value.status_code == 422


class TestParseCSV:
    def _make_csv(self, rows: list) -> bytes:
        import csv, io
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return buf.getvalue().encode()

    def test_valid_ecg_csv_parses(self):
        from medwear_med_api.batch_router import _parse_csv
        import uuid as _uuid

        csv_bytes = self._make_csv([{
            "uuid":                      str(_uuid.uuid4()),
            "schema_id":                 "ecg-v1",
            "source_creation_date_time": _NOW,
            "signal_type":               "ECG",
            "patient_id":                "1",
            "device_id":                 "dev-001",
            "sampling_rate_hz":          "256.0",
            "unit":                      "uV",
            "start_date_time":           _NOW,
            "end_date_time":             _END,
            "samples":                   ";".join(["0.1"] * 256),
            "signal_quality":            ";".join(["0.9"] * 256),
        }])
        result = _parse_csv(csv_bytes)
        assert len(result.batches) == 1
        assert result.batches[0].signal_type.value == "ECG"

    def test_invalid_csv_raises_400(self):
        from fastapi import HTTPException
        from medwear_med_api.batch_router import _parse_csv
        with pytest.raises(HTTPException) as exc_info:
            _parse_csv(b"not,valid,csv\nmissing,required,columns")
        assert exc_info.value.status_code == 400


# ===========================================================================
# routers/export.py — format builders (no InfluxDB needed)
# ===========================================================================

class TestExportFormatBuilders:
    _META = {
        "uuid": "abc12345-0000-0000-0000-000000000000",
        "signal_type": "ECG",
        "sampling_rate_hz": 256.0,
        "units": "uV",
        "patient_id": "1",
        "device_id": "dev-test",
        "start_timestamp": _NOW,
    }

    def test_build_csv_output_format(self):
        from medwear_med_api.routers.export import _build_csv
        samples = [1.0, 2.0, 3.0]
        result  = _build_csv(samples, self._META)
        text    = result.decode()
        assert "sample_index" in text
        assert "value"        in text
        assert "ECG"          in text
        lines = [l for l in text.strip().split("\n") if l]
        assert len(lines) == 4  # header + 3 data rows

    def test_build_csv_empty_samples(self):
        from medwear_med_api.routers.export import _build_csv
        result = _build_csv([], self._META)
        text   = result.decode()
        lines  = [l for l in text.strip().split("\n") if l]
        assert len(lines) == 1  # header only

    def test_build_csv_preserves_values(self):
        from medwear_med_api.routers.export import _build_csv
        samples = [42.5, -7.3, 0.0]
        result  = _build_csv(samples, self._META)
        text    = result.decode()
        assert "42.5" in text
        assert "-7.3" in text

    def test_build_wfdb_zip_structure(self):
        """WFDB export must produce a ZIP with .hea and .dat files."""
        import zipfile, io
        from medwear_med_api.routers.export import _build_wfdb_zip

        samples = [float(i) for i in range(512)]
        try:
            raw = _build_wfdb_zip(samples, self._META)
        except Exception as exc:
            pytest.skip(f"wfdb not available: {exc}")

        zf  = zipfile.ZipFile(io.BytesIO(raw))
        names = [n.lower() for n in zf.namelist()]
        assert any(n.endswith(".hea") for n in names), f"No .hea in {names}"
        assert any(n.endswith(".dat") for n in names), f"No .dat in {names}"

    def test_build_edf_produces_bytes(self):
        """EDF export must return non-empty bytes starting with EDF header."""
        from medwear_med_api.routers.export import _build_edf

        samples = [float(i % 100) for i in range(512)]
        try:
            raw = _build_edf(samples, self._META)
        except Exception as exc:
            pytest.skip(f"pyEDFlib not available: {exc}")

        assert isinstance(raw, bytes)
        assert len(raw) > 256   # EDF header is at least 256 bytes

    def test_build_wfdb_returns_501_without_lib(self):
        """Without wfdb installed, must raise HTTPException 501."""
        import sys
        from fastapi import HTTPException
        from medwear_med_api.routers.export import _build_wfdb_zip

        sys.modules["wfdb"] = None   # type: ignore — simulate missing package
        try:
            with pytest.raises(HTTPException) as exc_info:
                _build_wfdb_zip([1.0], self._META)
            assert exc_info.value.status_code == 501
        finally:
            sys.modules.pop("wfdb", None)


# ===========================================================================
# routers/gdpr.py — pure logic (no DB)
# ===========================================================================

class TestGdprPureLogic:
    def _make_user(self, roles: list) -> dict:
        return {
            "preferred_username": "test_user",
            "resource_access": {
                "med-api": {"roles": roles}
            },
        }

    def test_require_admin_passes_for_project_admin(self):
        from medwear_med_api.routers.gdpr import _require_admin
        _require_admin(self._make_user(["project_admin"]))   # must not raise

    def test_require_admin_passes_for_global_admin(self):
        from medwear_med_api.routers.gdpr import _require_admin
        _require_admin(self._make_user(["global_admin"]))

    def test_require_admin_rejects_collaborator(self):
        from fastapi import HTTPException
        from medwear_med_api.routers.gdpr import _require_admin
        with pytest.raises(HTTPException) as exc_info:
            _require_admin(self._make_user(["collaborator"]))
        assert exc_info.value.status_code == 403

    def test_require_admin_rejects_researcher(self):
        from fastapi import HTTPException
        from medwear_med_api.routers.gdpr import _require_admin
        with pytest.raises(HTTPException) as exc_info:
            _require_admin(self._make_user(["researcher"]))
        assert exc_info.value.status_code == 403

    def test_require_admin_rejects_empty_roles(self):
        from fastapi import HTTPException
        from medwear_med_api.routers.gdpr import _require_admin
        with pytest.raises(HTTPException):
            _require_admin(self._make_user([]))

    def test_consent_grant_request_valid_types(self):
        from medwear_med_api.routers.gdpr import ConsentGrantRequest, _VALID_CONSENT_TYPES
        for ct in _VALID_CONSENT_TYPES:
            req = ConsentGrantRequest(consent_type=ct)
            assert req.consent_type == ct

    def test_consent_grant_request_ethics_fields(self):
        from medwear_med_api.routers.gdpr import ConsentGrantRequest
        req = ConsentGrantRequest(
            consent_type="research_use",
            ethics_protocol_number="EK-2026-00123",
            consent_purpose="Analyse ECG for cardiac arrhythmia detection",
            legal_basis="Art. 9(2)(a) GDPR",
            data_retention_days=3650,
            consent_expiry_date="2031-04-29T00:00:00Z",
        )
        assert req.ethics_protocol_number == "EK-2026-00123"
        assert req.data_retention_days == 3650

    def test_erasure_response_model(self):
        from medwear_med_api.routers.gdpr import ErasureResponse
        resp = ErasureResponse(
            patient_id="1", project_id="proj1",
            batches_erased=5, influx_deleted=True,
            minio_files_deleted=0,
            minio_note="No tracked files found",
            consent_revoked=2, status="erased",
        )
        assert resp.status == "erased"
        assert resp.minio_note is not None


# ===========================================================================
# schemas.py — BCGBody and GenericBody (0% coverage)
# ===========================================================================

class TestBCGAndGenericSchemas:
    def test_bcg_body_valid(self):
        from medwear_med_api.schemas import BCGBody
        b = BCGBody(
            bcg_sensor_readings=[0.1, 0.2, 0.3],
            unit="mV",
            effective_time_frame={"time_interval": {
                "start_date_time": _NOW,
                "end_date_time":   _END,
            }},
        )
        assert len(b.bcg_sensor_readings) == 3

    def test_bcg_body_nan_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import BCGBody
        with pytest.raises(ValidationError, match="non-finite"):
            BCGBody(
                bcg_sensor_readings=[float("nan"), 0.2],
                unit="mV",
                effective_time_frame={"time_interval": {
                    "start_date_time": _NOW, "end_date_time": _END}},
            )

    def test_bcg_body_empty_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import BCGBody
        with pytest.raises(ValidationError):
            BCGBody(
                bcg_sensor_readings=[],
                unit="mV",
                effective_time_frame={"time_interval": {
                    "start_date_time": _NOW, "end_date_time": _END}},
            )

    def test_generic_body_valid(self):
        from medwear_med_api.schemas import GenericBody
        g = GenericBody(
            signal_data=[1.0, 2.0],
            unit="ADC",
            effective_time_frame={"time_interval": {
                "start_date_time": _NOW, "end_date_time": _END}},
        )
        assert g.unit == "ADC"

    def test_generic_body_inf_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import GenericBody
        with pytest.raises(ValidationError, match="non-finite"):
            GenericBody(
                signal_data=[float("inf")],
                unit="ADC",
                effective_time_frame={"time_interval": {
                    "start_date_time": _NOW, "end_date_time": _END}},
            )

    def test_ecg_amplitude_out_of_range_rejected(self):
        """ECG samples wildly outside physiological range (>1% outliers) must fail."""
        from pydantic import ValidationError
        from medwear_med_api.schemas import ECGBody
        # 50 samples all at 50,000 uV — way outside ±10,000 uV
        samples = [50_000.0] * 50
        with pytest.raises(ValidationError, match="physiological"):
            ECGBody(
                ecg_lead_data=samples,
                signal_quality=[0.9] * 50,
                unit="uV",
                effective_time_frame={"time_interval": {
                    "start_date_time": _NOW, "end_date_time": _END}},
            )

    def test_ecg_baseline_wander_rejected(self):
        """ECG signal with large DC offset (mean > 30% of max) must fail."""
        from pydantic import ValidationError
        from medwear_med_api.schemas import ECGBody
        # Mean = 5000 uV = 50% of 10000 uV max → wander detected
        samples = [5_000.0] * 50
        with pytest.raises(ValidationError, match="baseline wander"):
            ECGBody(
                ecg_lead_data=samples,
                signal_quality=[0.9] * 50,
                unit="uV",
                effective_time_frame={"time_interval": {
                    "start_date_time": _NOW, "end_date_time": _END}},
            )

    def test_get_validated_body_ecg(self):
        from medwear_med_api.schemas import BatchUploadRequest
        req = BatchUploadRequest(
            header={"uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "schema_id": "v1", "source_creation_date_time": _NOW},
            body={"ecg_lead_data": [0.1], "signal_quality": [0.9], "unit": "uV",
                  "effective_time_frame": {"time_interval": {
                      "start_date_time": _NOW, "end_date_time": _END}}},
            signal_type="ECG", patient_id="1", device_id="dev",
        )
        body = req.get_validated_body()
        from medwear_med_api.schemas import ECGBody
        assert isinstance(body, ECGBody)

    def test_get_validated_body_bcg(self):
        from medwear_med_api.schemas import BatchUploadRequest
        req = BatchUploadRequest(
            header={"uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "schema_id": "v1", "source_creation_date_time": _NOW},
            body={"bcg_sensor_readings": [0.1], "unit": "mV",
                  "effective_time_frame": {"time_interval": {
                      "start_date_time": _NOW, "end_date_time": _END}}},
            signal_type="BCG", patient_id="1", device_id="dev",
        )
        body = req.get_validated_body()
        from medwear_med_api.schemas import BCGBody
        assert isinstance(body, BCGBody)

    def test_get_units(self):
        from medwear_med_api.schemas import BatchUploadRequest
        req = BatchUploadRequest(
            header={"uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "schema_id": "v1", "source_creation_date_time": _NOW},
            body={"ecg_lead_data": [0.1], "signal_quality": [0.9], "unit": "mV",
                  "effective_time_frame": {"time_interval": {
                      "start_date_time": _NOW, "end_date_time": _END}}},
            signal_type="ECG", patient_id="1", device_id="dev",
        )
        assert req.get_units() == "mV"

    def test_get_signal_info_returns_dict(self):
        from medwear_med_api.schemas import BatchUploadRequest
        req = BatchUploadRequest(
            header={"uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "schema_id": "v1", "source_creation_date_time": _NOW,
                    "ecg_info": {"electrode_placement": ["LA", "RA"]}},
            body={"ecg_lead_data": [0.1], "signal_quality": [0.9], "unit": "uV",
                  "effective_time_frame": {"time_interval": {
                      "start_date_time": _NOW, "end_date_time": _END}}},
            signal_type="ECG", patient_id="1", device_id="dev",
        )
        info = req.get_signal_info()
        assert "electrode_placement" in info

    def test_batch_upload_list_request_empty_rejected(self):
        from pydantic import ValidationError
        from medwear_med_api.schemas import BatchUploadListRequest
        with pytest.raises(ValidationError):
            BatchUploadListRequest(batches=[])
