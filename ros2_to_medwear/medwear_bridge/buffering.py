"""Batch window W: accumulates the samples of one channel into batches."""

import math
import time

# Gap past which a buffered stream is split into a new batch: consecutive
# header stamps further apart than this mean the link dropped or stalled
# (Polar H10 ECG normally arrives in ~0.6 s notification bursts). Override
# per topic with "gap_threshold_s" in config.yaml.
DEFAULT_GAP_THRESHOLD_S = 1.0

# Hard cap on one batch, far below the backend's 1.2M-sample limit.
_MAX_BATCH_S = 600


class SampleBuffer:
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
