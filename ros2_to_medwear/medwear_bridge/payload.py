"""ROS 2 message dicts -> per-channel samples -> MedWear BatchUploadRequest payloads."""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# Backend body field per signal type; every other type uses "signal_data"
BODY_FIELD = {"ECG": "ecg_lead_data", "BCG": "bcg_sensor_readings"}


def ros_obj_to_dict(obj: Any) -> Any:
    """
    Recursively convert a ROS 2 message object to a plain Python dict.
    Arrays (numpy / array.array) become lists. Nested message objects recurse.
    """
    # numpy array or array.array
    if hasattr(obj, "tolist"):
        return obj.tolist()
    # ROS message object (has get_fields_and_field_types)
    if hasattr(obj, "get_fields_and_field_types"):
        return {k: ros_obj_to_dict(getattr(obj, k))
                for k in obj.get_fields_and_field_types()}
    # generic iterable (but not string/bytes)
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
        return [ros_obj_to_dict(v) for v in obj]
    return obj


def stamp_ns(msg_dict: dict) -> int:
    """ROS header stamp in ns (after ros_obj_to_dict the header is a plain
    dict); falls back to wall-clock now when the stamp is unset."""
    stamp = (msg_dict.get("header") or {}).get("stamp") or {}
    sec = int(stamp.get("sec", 0)) or int(time.time())
    return sec * 10**9 + int(stamp.get("nanosec", 0))


def get_path(obj: Any, path: str) -> Any:
    """Resolve a dotted field path, e.g. "linear_acceleration" or "vector"."""
    for part in path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def make_idempotency_key(session_id: str, signal_type: str, stamp_ns: int,
                         channel: "str | None" = None, patient_id: str = "",
                         device_id: str = "") -> str:
    raw = f"{session_id}:{signal_type}:{stamp_ns}"
    # Patient/device are part of the identity of a batch: the same samples
    # uploaded under another patient_id (e.g. a replay experiment) are new
    # data for that patient, not a retransmission.
    if patient_id or device_id:
        raw = f"{patient_id}:{device_id}:{raw}"
    if channel:
        raw += f":{channel}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def _split(values: list, n_channels: int, layout: str) -> "list[list[float]]":
    if n_channels <= 1 or not values:
        return [values]
    per = len(values) // n_channels
    if layout == "sample_major":
        return [values[c::n_channels][:per] for c in range(n_channels)]
    return [values[c * per:(c + 1) * per] for c in range(n_channels)]   # channel_major


def extract_channels(msg_dict: dict, topic_cfg: dict, n_channels: int = 1
                     ) -> "dict[str | None, tuple[list[float], list[float]]]":
    """Return {channel: (samples, quality)} for one message.

    Single-channel streams yield one unnamed channel (None), so their
    schema_id and idempotency keys are unchanged. Multi-channel streams
    (n_channels > 1, laid out per topic_cfg["layout"]) yield "ch0", "ch1", ...
    Topics with `axes` (IMU: sensor_msgs/Imu, geometry_msgs/Vector3Stamped)
    yield one channel per axis, each holding the single sample `field.<axis>`;
    the backend has no multi-channel body, so each is its own batch."""
    value = get_path(msg_dict, topic_cfg["field"])
    axes = topic_cfg.get("axes")
    if axes:
        if not isinstance(value, dict):
            return {}
        return {axis: ([float(value[axis])], []) for axis in axes}

    samples = [float(v) for v in (value or [])]
    quality_field = topic_cfg.get("quality_field")
    quality = [float(v) for v in (msg_dict.get(quality_field) or [])] if quality_field else []
    if n_channels <= 1:
        return {None: (samples, quality)}

    layout = topic_cfg.get("layout", "channel_major")
    data = _split(samples, n_channels, layout)
    if len(quality) == len(samples):
        qual = _split(quality, n_channels, layout)
    elif len(quality) == len(data[0]):          # one quality value per sample, shared
        qual = [quality] * n_channels
    else:
        qual = [[]] * n_channels
    return {f"ch{c}": (data[c], qual[c]) for c in range(n_channels)}


def build_batch_from_samples(
    samples: "list[float]",
    quality: "list[float]",
    stamp_ns: int,
    session_id: str,
    topic_cfg: dict,
    patient_id: str,
    device_id: str,
    project_id: str,
    channel: "str | None" = None,
    header_extras: "dict | None" = None,
) -> dict:
    """Build a MedWear BatchUploadRequest payload from samples starting at
    `stamp_ns`. `channel` (IMU axis or "chN") is folded into schema_id and the
    idempotency key so per-channel batches sharing a timestamp stay distinct.
    `header_extras` (device_identity, ecg_info, ...) come from the topic's
    Info message (see info.py)."""
    signal_type = topic_cfg["signal_type"]
    unit        = topic_cfg["unit"]
    fs          = float(topic_cfg["sampling_rate_hz"])

    if not samples:
        return {}

    start_s    = stamp_ns / 1e9
    start_dt   = datetime.fromtimestamp(start_s, tz=timezone.utc)
    duration_s = len(samples) / fs if fs > 0 else 0
    end_dt     = datetime.fromtimestamp(start_s + duration_s, tz=timezone.utc)

    idem_key = make_idempotency_key(session_id, signal_type, stamp_ns, channel,
                                    patient_id, device_id)
    checksum  = hashlib.sha256(json.dumps(samples, separators=(",", ":")).encode()).hexdigest()

    body: dict[str, Any] = {
        BODY_FIELD.get(signal_type, "signal_data"): samples,
        "unit": unit,
        "effective_time_frame": {
            "time_interval": {
                "start_date_time": start_dt.isoformat(),
                "end_date_time":   end_dt.isoformat(),
            }
        },
    }

    # ECG requires signal_quality; attach if available
    if quality and signal_type == "ECG":
        body["signal_quality"] = quality

    schema_id = f"ros2_{signal_type.lower()}_{channel}_v1" if channel else f"ros2_{signal_type.lower()}_v1"
    header = {
        "uuid":                      str(uuid.uuid4()),
        "schema_id":                 schema_id,
        "source_creation_date_time": start_dt.isoformat(),
        "acquisition_rate": {
            "value":           fs,
            "unit":            "Hz",
            "number_of_times": 1,
        },
    }
    header.update(header_extras or {})
    return {
        "header":          header,
        "body":            body,
        "signal_type":     signal_type,
        "patient_id":      patient_id,
        "device_id":       device_id,
        "project_id":      project_id,
        "idempotency_key": idem_key,
        "checksum":        checksum,
    }


def build_batch(msg_dict: dict, topic_cfg: dict, patient_id: str, device_id: str,
                project_id: str) -> dict:
    """Convert a single-channel ROS 2 message dict to a MedWear BatchUploadRequest payload."""
    samples, quality = extract_channels(msg_dict, topic_cfg).get(None, ([], []))
    return build_batch_from_samples(
        samples, quality, stamp_ns(msg_dict), str(msg_dict.get("session_id", "unknown")),
        topic_cfg, patient_id, device_id, project_id,
    )
