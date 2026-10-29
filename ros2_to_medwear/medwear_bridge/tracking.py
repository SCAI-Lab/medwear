"""Per-topic staleness and sequence-gap tracking (observes and logs only)."""

import re

# Default deadline past which a sample is flagged "stale": detecting stale
# data at the application should catch a lost link within a control deadline,
# well before the BLE supervision timeout ends the connection (5-6 s for
# common wearables). Override per topic with "staleness_deadline_s" in
# config.yaml.
DEFAULT_STALENESS_DEADLINE_S = 2.0

# Emulated devices pack a per-stream sequence number into session_id as
# "<patient_id>:<device>:<seq>", since none of the healthcare_msgs types
# carry a dedicated sequence field. Real device
# drivers use a plain UUID for session_id (one per connection, not per
# sample), which never matches this pattern -- gap detection is then simply
# unavailable for that stream, logged as such rather than guessed.
_SEQ_SUFFIX_RE = re.compile(r":(\d+)$")


def _parse_seq(session_id) -> "int | None":
    if not isinstance(session_id, str):
        return None
    m = _SEQ_SUFFIX_RE.search(session_id)
    return int(m.group(1)) if m else None


class StalenessGapTracker:
    """Per-topic sequence and staleness state: flags samples older than the
    deadline and gaps in the sequence numbers. It only observes and logs;
    what is posted to the API is unchanged."""

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
