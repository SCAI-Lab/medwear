"""Per-topic message handling: staleness tracking, channels, batching, upload."""

import logging
import time

from .buffering import SampleBuffer
from .info import channel_header, decode_info
from .payload import build_batch_from_samples, extract_channels, ros_obj_to_dict, stamp_ns
from .tracking import DEFAULT_STALENESS_DEADLINE_S, StalenessGapTracker
from .uploader import Uploader

log = logging.getLogger("medwear_bridge")


class TopicHandler:
    """State of one data topic. `topic_cfg` is already merged with the
    registry defaults (message_types.resolve_topic_config); sampling rate,
    unit and channel count still missing there are taken from the topic's
    Info message once it arrives."""

    def __init__(self, topic_cfg: dict, uploader: Uploader, args, clock):
        self.topic_cfg = topic_cfg
        self.signal_type = topic_cfg["signal_type"]
        self.uploader = uploader
        self.args = args
        self.clock = clock
        self.tracker = StalenessGapTracker(
            topic_cfg["topic"],
            float(topic_cfg.get("staleness_deadline_s", DEFAULT_STALENESS_DEADLINE_S)),
        )
        window = topic_cfg.get("batch_window_s")
        if window is not None and args.batch_window is not None:
            window = args.batch_window
        self.window_s = None if window is None else float(window)
        self.buffers: "dict[str | None, SampleBuffer]" = {}
        self.info: "dict | None" = None          # decoded Info message
        self._effective: "dict | None" = None
        self._warned_no_rate = False
        self._n_channels = 1

    @property
    def buffered(self) -> bool:
        return self.window_s is not None

    # ── Info ───────────────────────────────────────────────────────────────
    def info_callback(self, msg) -> None:
        self.info = decode_info(self.topic_cfg["info_type"], type(msg), ros_obj_to_dict(msg))
        self._effective = None
        log.info("[%s] Info received: %s | rate %s | unit %s | channels %s",
                 self.signal_type,
                 " ".join(v for v in (self.info.get("device_identity") or {}).values() if v) or "device n/a",
                 f"{self.info['sampling_rate_hz']:g} Hz" if self.info.get("sampling_rate_hz") else "not given",
                 self.info.get("unit") or "not given",
                 self.info.get("channel_count") or "from data messages")

    def effective_config(self) -> "dict | None":
        """topic_cfg completed from the Info message; None until a sampling
        rate is known (data is dropped, with one warning, until then)."""
        if self._effective is None:
            # precedence: config.yaml > Info message > registry default
            cfg = dict(self.topic_cfg)
            info = self.info or {}
            explicit = set(cfg.get("_explicit_keys", ()))
            for key in ("sampling_rate_hz", "unit"):
                if key not in explicit and info.get(key):
                    cfg[key] = info[key]
            if not cfg.get("sampling_rate_hz"):
                return None
            self._effective = cfg
        return self._effective

    def _channel_count(self, msg_dict: dict) -> int:
        field = self.topic_cfg.get("channels_field")
        if field and msg_dict.get(field):
            return int(msg_dict[field])
        if self.topic_cfg.get("channels"):
            return int(self.topic_cfg["channels"])
        return int((self.info or {}).get("channel_count") or 1)

    # ── Data ───────────────────────────────────────────────────────────────
    def _submit(self, cfg: dict, chunk: dict, n_channels: int) -> None:
        ch = chunk["channel"]
        ch_index = int(ch[2:]) if isinstance(ch, str) and ch.startswith("ch") else None
        payload = build_batch_from_samples(
            chunk["samples"], chunk["quality"], chunk["stamp_ns"], chunk["session_id"],
            cfg, patient_id=self.args.patient, device_id=self.args.device,
            project_id=self.args.project, channel=ch,
            header_extras=channel_header(self.info, ch_index, n_channels),
        )
        if payload:
            self.uploader.submit(payload, chunk)

    def callback(self, msg) -> None:
        recv_wall = time.time()
        msg_dict = ros_obj_to_dict(msg)   # fully converts nested ROS objects
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

        cfg = self.effective_config()
        if cfg is None:
            if not self._warned_no_rate:
                log.warning("[%s] no sampling_rate_hz in config and no Info message yet on %s: "
                            "dropping data until one arrives", self.signal_type,
                            self.topic_cfg.get("info_topic") or "(no info topic)")
                self._warned_no_rate = True
            return

        n_channels = self._channel_count(msg_dict)
        channels = extract_channels(msg_dict, cfg, n_channels)
        if not any(samples for samples, _ in channels.values()):
            log.warning("[%s] empty samples, skipping", self.signal_type)
            return
        t_ns = stamp_ns(msg_dict)
        # Imu / Vector3Stamped carry no session_id; fall back to the run's
        # identity so idempotency keys still differ between runs.
        session_id = str(msg_dict.get("session_id")
                         or f"{self.args.patient}:{self.args.device}")

        for channel, (samples, quality) in channels.items():
            if not self.buffered:
                self._submit(cfg, dict(samples=samples, quality=quality, stamp_ns=t_ns,
                                       session_id=session_id, channel=channel,
                                       msg_stamp_ns=[t_ns], msg_recv=[recv_wall],
                                       msg_n=[len(samples)], flush_reason="unbuffered"), n_channels)
                continue
            buf = self.buffers.get(channel)
            if buf is None:
                buf = self.buffers[channel] = SampleBuffer(cfg, channel, self.window_s)
            for chunk in buf.add(t_ns, session_id, samples, quality, recv_wall):
                self._submit(cfg, chunk, n_channels)
        self._n_channels = n_channels

    def flush_due(self) -> None:
        cfg = self.effective_config()
        for buf in self.buffers.values():
            if buf.due():
                self._submit(cfg, buf.take("window"), self._n_channels)

    def flush_all(self) -> None:
        cfg = self.effective_config()
        for buf in self.buffers.values():
            if buf.samples:
                self._submit(cfg, buf.take("shutdown"), self._n_channels)
