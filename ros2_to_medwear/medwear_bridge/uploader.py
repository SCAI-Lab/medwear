"""Background upload workers and the per-batch metrics file."""

import json
import logging
import queue
import threading
import time

from .client import MedWearClient

log = logging.getLogger("medwear_bridge")


class Uploader:
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
