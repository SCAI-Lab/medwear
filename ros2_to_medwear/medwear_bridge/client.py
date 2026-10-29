"""MedWear API client: token handling, sessions, batch upload with retries."""

import json
import logging
import threading
import time
import uuid

import requests

log = logging.getLogger("medwear_bridge")


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
    def _session_header(payload: dict) -> dict:
        """The header fields that stay constant for a stream (everything but
        the batch's uuid and creation time): schema_id, acquisition rate,
        device identity and the signal's Info block."""
        return {k: v for k, v in payload["header"].items()
                if k not in ("uuid", "source_creation_date_time")}

    @classmethod
    def _session_key(cls, payload: dict) -> tuple:
        return (payload["patient_id"], payload["device_id"], payload["project_id"],
                payload["signal_type"], payload["body"].get("unit"),
                json.dumps(cls._session_header(payload), sort_keys=True))

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
                    "signal_type": key[3], "unit": key[4], "header": self._session_header(payload),
                }, retries)
                setup_s = time.time() - t0
                if not res["ok"]:
                    log.error("Session registration failed (%s); sending full batches for %s",
                              res["http_status"], payload["header"]["schema_id"])
                    return payload, setup_s
                self._sessions[key] = sid
                log.info("Session %s registered for %s/%s", sid, key[1], payload["header"]["schema_id"])
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
