"""
MedWear ROS 2 Bridge — real-time biosignal subscriber
======================================================
Subscribes to ROS 2 topics (ECG, EEG, EDA, HR, HRV, IMU) and POSTs them as
batches to the MedWear API: one batch per message, or, for topics with
"batch_window_s" set in config.yaml, contiguous multi-message batches.

Usage (inside Docker or with ROS 2 sourced):
    python medwear_subscriber.py \
        --config config.yaml \
        --device <device_id> \
        --patient <patient_id> \
        --project <project_id>

Required environment variables:
    MEDWEAR_USERNAME   — Keycloak username
    MEDWEAR_PASSWORD   — Keycloak password
    API_BASE_URL       — default: http://medwear-nginx/v1

Optional:
    MEDWEAR_TOKEN      — static JWT (skips auto-refresh; expires after token lifetime)
"""

import argparse
import hashlib
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("medwear_bridge")


# ── Config loading ─────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ── API client with auto token refresh ────────────────────────────────────────

class MedWearClient:
    def __init__(self, base_url: str, username: str = "", password: str = "", static_token: str = "",
                 use_sessions: bool = True):
        self.base_url      = base_url.rstrip("/")
        # Stream metadata is registered once per bridge run (POST /data/session);
        # batches then carry only session_id, samples and time frame.
        self.use_sessions  = use_sessions
        self._run_nonce    = str(uuid.uuid4())
        self._sessions: "dict[tuple, str]" = {}
        self._sessions_lock = threading.Lock()
        self.username      = username
        self.password      = password
        self.static_token  = static_token
        self._token        = static_token
        self._token_expiry = 0.0          # unix timestamp when token expires
        self.session       = requests.Session()
        self.session.headers["Content-Type"] = "application/json"
        self.session.verify = False       # accept self-signed cert on server
        self._token_lock   = threading.Lock()
        self._local        = threading.local()
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning)

    def _refresh_token(self) -> None:
        """Fetch a fresh token via the API proxy endpoint."""
        if self.static_token:
            return   # static mode — user manages expiry themselves
        url = f"{self.base_url}/data/auth/token"
        try:
            r = requests.post(
                url,
                json={"username": self.username, "password": self.password},
                timeout=15,
                verify=False,
            )
            r.raise_for_status()
            data = r.json()
            self._token        = data["access_token"]
            expires_in         = data.get("expires_in", 300)
            self._token_expiry = time.time() + expires_in - 30   # refresh 30s early
            log.info("Token refreshed, valid for %ds", expires_in)
        except Exception as exc:
            log.error("Token refresh failed: %s", exc)
            raise

    def _ensure_token(self) -> None:
        if self.static_token:
            self._token = self.static_token
        elif time.time() >= self._token_expiry:
            self._refresh_token()
        self.session.headers["Authorization"] = f"Bearer {self._token}"

    @staticmethod
    def _session_key(payload: dict) -> tuple:
        h = payload["header"]
        return (payload["patient_id"], payload["device_id"], payload["project_id"],
                payload["signal_type"], h["schema_id"], h["acquisition_rate"]["value"],
                payload["body"].get("unit"))

    def to_wire(self, payload: dict, retries: int = 0) -> "tuple[dict, float | None]":
        """The request body for a full batch payload: with sessions, register
        the stream on first use and strip the metadata. Returns (body, seconds
        spent registering, or None). Falls back to the full payload if the
        session can't be registered, so no data is lost."""
        if not self.use_sessions:
            return payload, None
        key = self._session_key(payload)
        setup_s = None
        with self._sessions_lock:   # one registration per stream, even with several workers
            sid = self._sessions.get(key)
            if sid is None:
                t0 = time.time()
                sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"medwear-bridge:{self._run_nonce}:{key}"))
                res = self._post("/data/session", {
                    "session_id": sid, "patient_id": key[0], "device_id": key[1], "project_id": key[2],
                    "signal_type": key[3], "unit": key[6],
                    "header": {"schema_id": key[4], "acquisition_rate": payload["header"]["acquisition_rate"]},
                }, retries)
                setup_s = time.time() - t0
                if not res["ok"]:
                    log.error("Session registration failed (%s); sending full batches for %s",
                              res["http_status"], key[4])
                    return payload, setup_s
                self._sessions[key] = sid
                log.info("Session %s registered for %s/%s", sid, key[1], key[4])
        return {"session_id": sid, "project_id": payload["project_id"], "body": payload["body"],
                "idempotency_key": payload["idempotency_key"], "checksum": payload["checksum"]}, setup_s

    def forget_session(self, payload: dict) -> None:
        with self._sessions_lock:
            self._sessions.pop(self._session_key(payload), None)

    def post_batch(self, payload: dict, retries: int = 0) -> dict:
        """POST one batch. Transient failures (connection errors, 429, 5xx)
        are retried up to `retries` times with exponential backoff; schema
        rejections (4xx) are not, since resending can't fix them.

        Returns {"ok", "http_status", "attempts", "response"}, where
        response is the parsed JSON body (or {"text": ...}) of the last try."""
        return self._post("/data/batch", payload, retries)

    def _post(self, path: str, payload: dict, retries: int = 0) -> dict:
        url = f"{self.base_url}{path}"
        out: dict = {"ok": False, "http_status": None, "attempts": 0, "response": None,
                     "server_times": {}}
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 30))
            out["attempts"] = attempt + 1
            try:
                with self._token_lock:
                    self._ensure_token()
                    headers = dict(self.session.headers)
            except Exception:
                continue
            try:
                r = self._thread_session().post(url, json=payload, headers=headers, timeout=60)
            except requests.RequestException as exc:
                log.error("API request failed (attempt %d/%d): %s", attempt + 1, retries + 1, exc)
                out["response"] = {"text": str(exc)}
                continue
            out["http_status"] = r.status_code
            # med-api stage timestamps (unix s): request received, validated,
            # and committed to both stores
            out["server_times"] = {k: float(r.headers[h]) for k, h in (
                ("received", "X-Received-At"), ("validated", "X-Validated-At"),
                ("stored", "X-Stored-At")) if h in r.headers}
            try:
                out["response"] = r.json()
            except ValueError:
                out["response"] = {"text": r.text[:1000]}
            # duplicates are 200 with status "duplicate"; 409 is a real conflict
            # (same idempotency key, different samples or session metadata)
            if r.status_code in (200, 201):
                out["ok"] = True
                return out
            log.error("API error %d (attempt %d/%d): %s",
                      r.status_code, attempt + 1, retries + 1, r.text[:300])
            if r.status_code != 429 and r.status_code < 500:
                return out
        return out

    def _thread_session(self) -> requests.Session:
        """One HTTP session per uploader thread (requests.Session isn't
        thread-safe); the token is shared via self.session.headers."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = requests.Session()
            s.verify = False
        return s


# ── ROS message → plain dict ───────────────────────────────────────────────────

def _ros_obj_to_dict(obj: Any) -> Any:
    """
    Recursively convert a ROS 2 message object to a plain Python dict.
    Arrays (numpy / array.array) become lists. Nested message objects recurse.
    """
    # numpy array or array.array
    if hasattr(obj, "tolist"):
        return obj.tolist()
    # ROS message object (has get_fields_and_field_types)
    if hasattr(obj, "get_fields_and_field_types"):
        return {k: _ros_obj_to_dict(getattr(obj, k))
                for k in obj.get_fields_and_field_types()}
    # generic iterable (but not string/bytes)
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
        return [_ros_obj_to_dict(v) for v in obj]
    return obj


# ── Batch builder ──────────────────────────────────────────────────────────────

def _make_idempotency_key(session_id: str, signal_type: str, stamp_ns: int,
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


def _stamp_ns(msg_dict: dict) -> int:
    """ROS header stamp in ns (after _ros_obj_to_dict the header is a plain
    dict); falls back to wall-clock now when the stamp is unset."""
    stamp = (msg_dict.get("header") or {}).get("stamp") or {}
    sec = int(stamp.get("sec", 0)) or int(time.time())
    return sec * 10**9 + int(stamp.get("nanosec", 0))


def _get_path(obj: Any, path: str) -> Any:
    """Resolve a dotted field path, e.g. "linear_acceleration" or "vector"."""
    for part in path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def extract_channels(msg_dict: dict, topic_cfg: dict) -> "dict[str | None, tuple[list[float], list[float]]]":
    """Return {channel: (samples, quality)} for one message.

    Plain topics yield a single unnamed channel (None) read from the
    float32[] `field`. Topics with `axes` (IMU: sensor_msgs/Imu,
    geometry_msgs/Vector3Stamped) yield one channel per axis, each holding
    the single sample `field.<axis>` -- the backend has no multi-axis IMU
    body, so each axis is uploaded as its own batch."""
    value = _get_path(msg_dict, topic_cfg["field"])
    axes = topic_cfg.get("axes")
    if axes:
        if not isinstance(value, dict):
            return {}
        return {axis: ([float(value[axis])], []) for axis in axes}

    samples = [float(v) for v in (value or [])]
    quality_field = topic_cfg.get("quality_field")
    quality = [float(v) for v in (msg_dict.get(quality_field) or [])] if quality_field else []
    return {None: (samples, quality)}


def build_batch(
    msg_dict: dict,
    topic_cfg: dict,
    patient_id: str,
    device_id: str,
    project_id: str,
) -> dict:
    """Convert a single-channel ROS 2 message dict to a MedWear BatchUploadRequest payload."""
    samples, quality = extract_channels(msg_dict, topic_cfg).get(None, ([], []))
    return build_batch_from_samples(
        samples, quality, _stamp_ns(msg_dict), str(msg_dict.get("session_id", "unknown")),
        topic_cfg, patient_id, device_id, project_id,
    )


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
) -> dict:
    """Build a MedWear BatchUploadRequest payload from samples starting at
    `stamp_ns`. `channel` (an IMU axis) is folded into schema_id and the
    idempotency key so per-axis batches sharing a timestamp stay distinct."""
    signal_type = topic_cfg["signal_type"]
    unit        = topic_cfg["unit"]
    fs          = float(topic_cfg["sampling_rate_hz"])

    if not samples:
        return {}

    start_s    = stamp_ns / 1e9
    start_dt   = datetime.fromtimestamp(start_s, tz=timezone.utc)
    duration_s = len(samples) / fs if fs > 0 else 0
    end_dt     = datetime.fromtimestamp(start_s + duration_s, tz=timezone.utc)

    idem_key = _make_idempotency_key(session_id, signal_type, stamp_ns, channel,
                                     patient_id, device_id)
    checksum  = hashlib.sha256(json.dumps(samples, separators=(",", ":")).encode()).hexdigest()

    # ECG uses "ecg_lead_data", BCG uses "bcg_sensor_readings", all others use "signal_data"
    _FIELD_MAP = {"ECG": "ecg_lead_data", "BCG": "bcg_sensor_readings"}
    body_field = _FIELD_MAP.get(signal_type, "signal_data")

    body: dict[str, Any] = {
        body_field: samples,
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
    batch_uuid = str(uuid.uuid4())
    return {
        "header": {
            "uuid":                      batch_uuid,
            "schema_id":                 schema_id,
            "source_creation_date_time": start_dt.isoformat(),
            "acquisition_rate": {
                "value":           fs,
                "unit":            "Hz",
                "number_of_times": 1,
            },
        },
        "body":            body,
        "signal_type":     signal_type,
        "patient_id":      patient_id,
        "device_id":       device_id,
        "project_id":      project_id,
        "idempotency_key": idem_key,
        "checksum":        checksum,
    }


# ── ROS 2 subscriber nodes ─────────────────────────────────────────────────────

# Default deadline past which a sample is flagged "stale" (manuscript
# sec:sim-design / sec:guidance: application-level staleness detection
# should catch a lost link within the control deadline, well before a
# supervision timeout is negotiated -- 5-6s for these devices). Override
# per topic with an optional "staleness_deadline_s" key in config.yaml.
DEFAULT_STALENESS_DEADLINE_S = 2.0

# Emulated devices (gym_sim/emulated_device.py) pack a per-stream sequence
# number into session_id as "<patient_id>:<device>:<seq>", since none of
# the healthcare_msgs types carry a dedicated sequence field. Real device
# drivers use a plain UUID for session_id (one per connection, not per
# sample), which never matches this pattern -- gap detection is then simply
# unavailable for that stream, logged as such rather than guessed.
_SEQ_SUFFIX_RE = re.compile(r":(\d+)$")


def _parse_seq(session_id) -> "int | None":
    if not isinstance(session_id, str):
        return None
    m = _SEQ_SUFFIX_RE.search(session_id)
    return int(m.group(1)) if m else None


class _StalenessGapTracker:
    """Per-topic sequence/staleness state -- this is the "MEDWEAR marks
    samples as stale... and flags gaps from the schema's timestamps and
    sequence numbers" instrumentation the manuscript commits to
    (sec:sim-design, marked \\TODO{implement} there). Additive only: it
    never changes what gets posted to the real API, so it can't affect the
    production ingestion path -- it only observes and logs."""

    def __init__(self, topic: str, deadline_s: float):
        self.topic = topic
        self.deadline_s = deadline_s
        self._last_seq = None
        self.n_total = 0
        self.n_stale = 0
        self.n_gap = 0

    def observe(self, msg_dict: dict, bridge_receive_time: float) -> dict:
        self.n_total += 1
        stamp = (msg_dict.get("header") or {}).get("stamp") or {}
        sample_time = int(stamp.get("sec", 0)) + int(stamp.get("nanosec", 0)) / 1e9
        age_s = max(0.0, bridge_receive_time - sample_time) if sample_time > 0 else float("nan")
        stale = age_s > self.deadline_s if age_s == age_s else False  # NaN-safe

        seq = _parse_seq(msg_dict.get("session_id"))
        gap = False
        if seq is not None:
            if self._last_seq is not None and seq != self._last_seq + 1:
                gap = True
            self._last_seq = seq

        if stale:
            self.n_stale += 1
        if gap:
            self.n_gap += 1
        return {"age_s": age_s, "stale": stale, "seq": seq, "gap": gap}


# Gap past which a buffered stream is split into a new batch: consecutive
# header stamps further apart than this mean the link dropped or stalled
# (Polar H10 ECG normally arrives in ~0.6 s notification bursts). Override
# per topic with "gap_threshold_s" in config.yaml.
DEFAULT_GAP_THRESHOLD_S = 1.0

# Hard cap on one batch, far below the backend's 1.2M-sample limit.
_MAX_BATCH_S = 600


class _SampleBuffer:
    """Accumulates the samples of one topic channel into contiguous batches,
    for topics with a batch window W ("batch_window_s" in config.yaml, or
    --batch-window). High-rate drivers (Polar H10: one ECG sample per
    message at 130 Hz) would otherwise cost one HTTP request per sample.

    A batch is flushed once its oldest sample has waited W at the bridge
    (so W bounds the staleness added by batching), when the next message's
    stamp jumps by more than gap_threshold_s or goes backwards (so a gap in
    the data stays a gap in MedWear and the backend's sample-count coherence
    check holds), or when the session_id changes (reconnect)."""

    def __init__(self, topic_cfg: dict, channel: "str | None", window_s: float):
        self.topic_cfg = topic_cfg
        self.channel = channel
        self.window_s = window_s
        fs = float(topic_cfg["sampling_rate_hz"])
        self.max_samples = max(1, math.ceil(fs * _MAX_BATCH_S))
        self.gap_ns = int(float(topic_cfg.get("gap_threshold_s", DEFAULT_GAP_THRESHOLD_S)) * 1e9)
        self._reset()

    def _reset(self) -> None:
        self.samples: "list[float]" = []
        self.quality: "list[float]" = []
        self.quality_complete = True
        self.session_id = ""
        self.msg_stamp_ns: "list[int]" = []
        self.msg_recv: "list[float]" = []
        self.msg_n: "list[int]" = []
        self.first_monotonic = 0.0

    def add(self, stamp_ns: int, session_id: str, samples: "list[float]",
            quality: "list[float]", recv_wall: float) -> "list[dict]":
        """Append one message's samples; return the chunks it closed."""
        done = []
        if self.samples:
            last = self.msg_stamp_ns[-1]
            if stamp_ns - last > self.gap_ns:
                done.append(self.take("gap"))
            elif stamp_ns < last:
                done.append(self.take("stamp_backwards"))
            elif session_id != self.session_id:
                done.append(self.take("session_change"))
        if not self.samples:
            self.session_id = session_id
            self.first_monotonic = time.monotonic()
        self.samples.extend(samples)
        if len(quality) == len(samples):
            self.quality.extend(quality)
        else:
            self.quality_complete = False
        self.msg_stamp_ns.append(stamp_ns)
        self.msg_recv.append(recv_wall)
        self.msg_n.append(len(samples))
        if len(self.samples) >= self.max_samples:
            done.append(self.take("max_size"))
        elif self.due():
            done.append(self.take("window"))
        return done

    def due(self) -> bool:
        return bool(self.samples) and time.monotonic() - self.first_monotonic >= self.window_s

    def take(self, reason: str) -> dict:
        """Remove and return the buffered chunk."""
        chunk = dict(samples=self.samples,
                     quality=self.quality if self.quality_complete else [],
                     stamp_ns=self.msg_stamp_ns[0], session_id=self.session_id,
                     channel=self.channel, msg_stamp_ns=self.msg_stamp_ns,
                     msg_recv=self.msg_recv, msg_n=self.msg_n, flush_reason=reason)
        self._reset()
        return chunk


class _Uploader:
    """Posts batches from a queue on background worker threads, so a slow or
    retrying HTTP request never blocks the ROS executor (which would let
    the subscription queue overflow and silently drop messages). With more
    than one worker, batches may be committed out of order.

    With a metrics file, writes one JSON line per batch: the original
    header stamp and bridge receipt time of every message in it, when it
    was queued/sent/answered, and the API's verdict (used by the replay
    tooling to compute per-sample live and durable latencies)."""

    def __init__(self, client: MedWearClient, workers: int = 1, retries: int = 3,
                 metrics_path: "str | None" = None):
        self.client = client
        self.retries = retries
        self.queue: "queue.Queue[tuple | None]" = queue.Queue()
        self._lock = threading.Lock()
        self.counts = {"ok": 0, "failed": 0, "duplicate": 0, "overlap": 0, "samples_ok": 0}
        self._metrics = open(metrics_path, "a", buffering=1) if metrics_path else None
        self._threads = [threading.Thread(target=self._run, name=f"medwear_uploader_{i}", daemon=True)
                         for i in range(max(1, workers))]

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def submit(self, payload: dict, chunk: dict) -> None:
        self.queue.put((payload, chunk, time.time()))

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            payload, chunk, enqueued = item
            body = payload["body"]
            n = len(body.get("signal_data") or body.get("ecg_lead_data")
                    or body.get("bcg_sensor_readings") or [])
            wire, session_setup_s = self.client.to_wire(payload, retries=self.retries)
            post_start = time.time()
            res = self.client.post_batch(wire, retries=self.retries)
            if (res["http_status"] == 404 and "session_id" in wire and
                    ((res["response"] or {}).get("detail") or {}).get("error") == "UNKNOWN_SESSION"):
                # server lost the session (e.g. database reset): register again
                self.client.forget_session(payload)
                wire, _ = self.client.to_wire(payload, retries=self.retries)
                res = self.client.post_batch(wire, retries=self.retries)
            post_end = time.time()

            results = (res["response"] or {}).get("results") or [{}]
            status = results[0].get("status") if res["ok"] else None
            overlap = bool(results[0].get("overlap_warning")) if res["ok"] else False
            with self._lock:
                if res["ok"]:
                    self.counts["ok"] += 1
                    self.counts["samples_ok"] += n
                    self.counts["duplicate"] += status == "duplicate"
                    self.counts["overlap"] += overlap
                else:
                    self.counts["failed"] += 1
                if self._metrics:
                    rec = self._metrics_record(payload, chunk, n, res, results[0], enqueued, post_start, post_end)
                    rec["session_setup_s"] = session_setup_s
                    rec["wire"] = "session" if "session_id" in wire else "full"
                    self._metrics.write(json.dumps(rec, separators=(",", ":")) + "\n")
            log.info("[%s] %s — %d samples | %s | start=%s%s",
                     payload["signal_type"], "OK" if res["ok"] else "FAIL", n,
                     payload["header"]["schema_id"],
                     body["effective_time_frame"]["time_interval"]["start_date_time"],
                     f" | {status}" if status and status != "ingested" else "")

    @staticmethod
    def _metrics_record(payload, chunk, n, res, result, enqueued, post_start, post_end) -> dict:
        detail = (res["response"] or {}).get("detail")
        error = detail if isinstance(detail, dict) else ({"detail": detail} if detail else None)
        t0_ns, r0 = chunk["msg_stamp_ns"][0], chunk["msg_recv"][0]
        return {
            "batch_uuid": payload["header"]["uuid"],
            "server_batch_uuid": result.get("batch_uuid") if res["ok"] else None,
            "result_status": result.get("status") if res["ok"] else None,
            "overlap_warning": result.get("overlap_warning") if res["ok"] else None,
            "ok": res["ok"], "http_status": res["http_status"], "attempts": res["attempts"],
            "error": error if not res["ok"] else None,
            "signal_type": payload["signal_type"], "schema_id": payload["header"]["schema_id"],
            "patient_id": payload["patient_id"], "device_id": payload["device_id"],
            "n_samples": n, "flush_reason": chunk["flush_reason"],
            # per message: header stamp (us after msg_stamp0_ns), bridge
            # receipt (us after msg_recv0), and sample count when not all 1
            "msg_stamp0_ns": t0_ns, "msg_recv0": r0,
            "msg_stamp_us": [(s - t0_ns) // 1000 for s in chunk["msg_stamp_ns"]],
            "msg_recv_us": [round((r - r0) * 1e6) for r in chunk["msg_recv"]],
            "msg_n": None if all(k == 1 for k in chunk["msg_n"]) else chunk["msg_n"],
            "enqueued": enqueued, "post_start": post_start, "post_end": post_end,
            "server_received": res["server_times"].get("received"),
            "server_validated": res["server_times"].get("validated"),
            "server_stored": res["server_times"].get("stored"),
        }

    def close(self) -> None:
        """Upload everything still queued, then stop."""
        for _ in self._threads:
            self.queue.put(None)
        for t in self._threads:
            t.join()
        if self._metrics:
            self._metrics.close()


class _TopicHandler:
    """Per-topic state: staleness/gap tracking plus optional sample buffers."""

    def __init__(self, topic_cfg: dict, uploader: _Uploader, args: argparse.Namespace, clock):
        self.topic_cfg = topic_cfg
        self.signal_type = topic_cfg["signal_type"]
        self.uploader = uploader
        self.args = args
        self.clock = clock
        self.tracker = _StalenessGapTracker(
            topic_cfg["topic"],
            float(topic_cfg.get("staleness_deadline_s", DEFAULT_STALENESS_DEADLINE_S)),
        )
        window = topic_cfg.get("batch_window_s")
        if window is not None and args.batch_window is not None:
            window = args.batch_window
        self.window_s = None if window is None else float(window)
        self.buffers: "dict[str | None, _SampleBuffer]" = {}

    @property
    def buffered(self) -> bool:
        return self.window_s is not None

    def _submit(self, chunk: dict) -> None:
        payload = build_batch_from_samples(
            chunk["samples"], chunk["quality"], chunk["stamp_ns"], chunk["session_id"],
            self.topic_cfg, patient_id=self.args.patient, device_id=self.args.device,
            project_id=self.args.project, channel=chunk["channel"],
        )
        if payload:
            self.uploader.submit(payload, chunk)

    def callback(self, msg) -> None:
        recv_wall = time.time()
        msg_dict = _ros_obj_to_dict(msg)   # fully converts nested ROS objects
        # The node clock is ROS time: wall time when live, the recording's
        # time under `ros2 bag play --clock` with --use-sim-time, so ages of
        # replayed samples mean what they meant during the recording.
        flags = self.tracker.observe(
            msg_dict, bridge_receive_time=self.clock.now().nanoseconds / 1e9)
        if flags["stale"]:
            log.warning("[%s] STALE sample: age=%.3fs exceeds deadline=%.3fs",
                        self.signal_type, flags["age_s"], self.tracker.deadline_s)
        if flags["gap"]:
            log.warning("[%s] GAP detected: sequence jumped to %s", self.signal_type, flags["seq"])

        channels = extract_channels(msg_dict, self.topic_cfg)
        if not any(samples for samples, _ in channels.values()):
            log.warning("[%s] empty samples, skipping", self.signal_type)
            return
        stamp_ns = _stamp_ns(msg_dict)
        # Imu / Vector3Stamped carry no session_id; fall back to the run's
        # identity so idempotency keys still differ between runs.
        session_id = str(msg_dict.get("session_id")
                         or f"{self.args.patient}:{self.args.device}")

        for channel, (samples, quality) in channels.items():
            if not self.buffered:
                self._submit(dict(samples=samples, quality=quality, stamp_ns=stamp_ns,
                                  session_id=session_id, channel=channel,
                                  msg_stamp_ns=[stamp_ns], msg_recv=[recv_wall],
                                  msg_n=[len(samples)], flush_reason="unbuffered"))
                continue
            buf = self.buffers.get(channel)
            if buf is None:
                buf = self.buffers[channel] = _SampleBuffer(self.topic_cfg, channel, self.window_s)
            for chunk in buf.add(stamp_ns, session_id, samples, quality, recv_wall):
                self._submit(chunk)

    def flush_due(self) -> None:
        for buf in self.buffers.values():
            if buf.due():
                self._submit(buf.take("window"))

    def flush_all(self) -> None:
        for buf in self.buffers.values():
            if buf.samples:
                self._submit(buf.take("shutdown"))


def run(args: argparse.Namespace, cfg: dict) -> None:
    import rclpy
    from rclpy.clock import Clock, ClockType
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.parameter import Parameter

    # Import healthcare_msgs message types
    try:
        from healthcare_msgs.msg import ECG, EEG, EDA, HeartRate, HeartRateVariability
    except ImportError as exc:
        log.error("healthcare_msgs not found: %s", exc)
        log.error("Install from: https://github.com/SCAI-Lab/healthcare_msgs")
        sys.exit(1)
    from geometry_msgs.msg import Vector3Stamped
    from sensor_msgs.msg import Imu
    msg_type_map: dict[str, Any] = {
        "healthcare_msgs/msg/ECG":                    ECG,
        "healthcare_msgs/msg/EEG":                    EEG,
        "healthcare_msgs/msg/EDA":                    EDA,
        "healthcare_msgs/msg/HeartRate":              HeartRate,
        "healthcare_msgs/msg/HeartRateVariability":   HeartRateVariability,
        "sensor_msgs/msg/Imu":                        Imu,
        "geometry_msgs/msg/Vector3Stamped":           Vector3Stamped,
    }

    base_url  = os.getenv("API_BASE_URL", cfg.get("api", {}).get("base_url", "http://localhost/v1"))
    static_token = os.getenv("MEDWEAR_TOKEN", "")
    username  = os.getenv("MEDWEAR_USERNAME", "")
    password  = os.getenv("MEDWEAR_PASSWORD", "")

    if not static_token and not (username and password):
        log.error("Provide MEDWEAR_TOKEN or both MEDWEAR_USERNAME + MEDWEAR_PASSWORD")
        sys.exit(1)

    client = MedWearClient(base_url, username=username, password=password, static_token=static_token,
                           use_sessions=not args.no_sessions)
    uploader = _Uploader(client, workers=args.upload_workers, metrics_path=args.metrics_file)
    uploader.start()

    rclpy.init()
    overrides = [Parameter("use_sim_time", Parameter.Type.BOOL, True)] if args.use_sim_time else []
    node = Node("medwear_bridge", parameter_overrides=overrides)
    log.info("MedWear ROS 2 bridge started | patient=%s device=%s project=%s workers=%d",
             args.patient, args.device, args.project, args.upload_workers)

    handlers: list[_TopicHandler] = []
    for topic_cfg in cfg.get("topics", []):
        topic        = topic_cfg["topic"]
        msg_type_str = topic_cfg["msg_type"]
        MsgType      = msg_type_map.get(msg_type_str)
        if MsgType is None:
            log.warning("Unknown msg_type %r, skipping %s", msg_type_str, topic)
            continue

        handler = _TopicHandler(topic_cfg, uploader, args, node.get_clock())
        handlers.append(handler)
        # Deep queue: high-rate topics (Polar ECG/ACC at 130/200 Hz) must not
        # drop messages while the executor is busy with another callback.
        node.create_subscription(MsgType, topic, handler.callback,
                                 int(topic_cfg.get("qos_depth", 1000)))
        log.info("Subscribed: %s → %s%s", topic, topic_cfg["signal_type"],
                 f" (batched, W={handler.window_s}s)" if handler.buffered else "")

    # Steady (wall) clock, so buffers still flush once a replayed bag ends
    # and /clock stops advancing. 50 ms resolution on W.
    def _flush_due() -> None:
        for h in handlers:
            h.flush_due()
    node.create_timer(0.05, _flush_due, clock=Clock(clock_type=ClockType.STEADY_TIME))

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        log.info("Bridge stopped")
    finally:
        for h in handlers:
            h.flush_all()
        log.info("Uploading %d queued batches...", uploader.queue.qsize())
        uploader.close()
        for h in handlers:
            t = h.tracker
            log.info("Topic %s: %d messages, %d stale, %d gap",
                     t.topic, t.n_total, t.n_stale, t.n_gap)
        c = uploader.counts
        # Machine-readable last line, parsed by replay tooling.
        log.info("SUMMARY batches_ok=%d batches_failed=%d samples_ok=%d duplicates=%d overlaps=%d",
                 c["ok"], c["failed"], c["samples_ok"], c["duplicate"], c["overlap"])
        node.destroy_node()
        rclpy.try_shutdown()
        if c["failed"]:
            sys.exit(2)


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MedWear ROS 2 bridge")
    p.add_argument("--config",  default="config.yaml", help="Path to config.yaml")
    p.add_argument("--device",  required=True, help="Device ID (e.g. corsano-001)")
    p.add_argument("--patient", required=True, help="Patient ID (string, e.g. sub-001)")
    p.add_argument("--project", required=True, help="Project ID (e.g. tjalf-bci)")
    p.add_argument("--use-sim-time", action="store_true",
                   help="Use /clock as ROS time (for `ros2 bag play --clock` replays)")
    p.add_argument("--batch-window", type=float,
                   help="Override batch_window_s (W, seconds) of every batched topic")
    p.add_argument("--upload-workers", type=int, default=1,
                   help="Concurrent upload threads (default 1)")
    p.add_argument("--metrics-file",
                   help="Append one JSON line of timing/verdict metrics per batch to this file")
    p.add_argument("--no-sessions", action="store_true",
                   help="Send full metadata with every batch (servers without POST /data/session)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg  = load_config(args.config)
    run(args, cfg)
