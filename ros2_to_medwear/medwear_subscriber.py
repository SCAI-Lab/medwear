"""
MedWear ROS 2 Bridge — real-time biosignal subscriber
======================================================
Subscribes to ROS 2 topics carrying healthcare_msgs biosignals (ECG, EEG,
EMG, EOG, ICG, BCG, PPG, EDA, heart rate, HRV, RR interval, respiratory rate,
stress, blood volume pulse, cardiac output, stroke volume) and IMU data, and
POSTs them as batches to the MedWear API: one batch per message, or, for
topics with "batch_window_s" set in config.yaml, contiguous multi-message
batches. Each topic's ...Info message, when published, supplies the device
identity, sampling rate, unit, channel labels and signal-specific metadata.
See medwear_bridge/message_types.py for the supported types.

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
import logging
import os
import sys

import yaml

from medwear_bridge.client import MedWearClient
from medwear_bridge.handler import TopicHandler
from medwear_bridge.message_types import import_message, resolve_topic_config
from medwear_bridge.uploader import Uploader
# Re-exported for code that imported them from this module before the split
from medwear_bridge.payload import (build_batch, build_batch_from_samples,  # noqa: F401
                                    extract_channels)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("medwear_bridge")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run(args: argparse.Namespace, cfg: dict) -> None:
    import rclpy
    from rclpy.clock import Clock, ClockType
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

    base_url  = os.getenv("API_BASE_URL", cfg.get("api", {}).get("base_url", "http://localhost/v1"))
    static_token = os.getenv("MEDWEAR_TOKEN", "")
    username  = os.getenv("MEDWEAR_USERNAME", "")
    password  = os.getenv("MEDWEAR_PASSWORD", "")

    if not static_token and not (username and password):
        log.error("Provide MEDWEAR_TOKEN or both MEDWEAR_USERNAME + MEDWEAR_PASSWORD")
        sys.exit(1)

    client = MedWearClient(base_url, username=username, password=password, static_token=static_token,
                           use_sessions=not args.no_sessions)
    uploader = Uploader(client, workers=args.upload_workers, metrics_path=args.metrics_file)
    uploader.start()

    rclpy.init()
    overrides = [Parameter("use_sim_time", Parameter.Type.BOOL, True)] if args.use_sim_time else []
    node = Node("medwear_bridge", parameter_overrides=overrides)
    log.info("MedWear ROS 2 bridge started | patient=%s device=%s project=%s workers=%d",
             args.patient, args.device, args.project, args.upload_workers)

    handlers: list[TopicHandler] = []
    for raw_cfg in cfg.get("topics", []):
        topic_cfg = resolve_topic_config(raw_cfg)
        topic = topic_cfg["topic"]
        try:
            MsgType = import_message(topic_cfg["msg_type"])
        except (ImportError, AttributeError, ValueError) as exc:
            log.warning("Unknown msg_type %r (%s), skipping %s", topic_cfg["msg_type"], exc, topic)
            continue
        if not topic_cfg.get("signal_type") or not topic_cfg.get("field"):
            log.warning("%s: msg_type %s is not in the registry; set signal_type and field in "
                        "config.yaml. Skipping.", topic, topic_cfg["msg_type"])
            continue

        handler = TopicHandler(topic_cfg, uploader, args, node.get_clock())
        handlers.append(handler)
        # Deep queue: high-rate topics (Polar ECG/ACC at 130/200 Hz) must not
        # drop messages while the executor is busy with another callback.
        node.create_subscription(MsgType, topic, handler.callback,
                                 int(topic_cfg.get("qos_depth", 1000)))
        log.info("Subscribed: %s → %s%s", topic, topic_cfg["signal_type"],
                 f" (batched, W={handler.window_s}s)" if handler.buffered else "")

        # The Info message is latched by the drivers (transient local), so a
        # transient-local subscription also receives one published earlier.
        info_topic = topic_cfg.get("info_topic")
        if info_topic and topic_cfg.get("info_type"):
            durability = (DurabilityPolicy.VOLATILE if topic_cfg.get("info_durability") == "volatile"
                          else DurabilityPolicy.TRANSIENT_LOCAL)
            qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=durability)
            node.create_subscription(import_message(topic_cfg["info_type"]), info_topic,
                                     handler.info_callback, qos)
            log.info("Subscribed (info): %s", info_topic)

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
    p.add_argument("--project", required=True, help="Project ID (e.g. medwear_demo)")
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
