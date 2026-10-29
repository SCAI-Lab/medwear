"""Sensor profiles: what the tool knows about a sensor model.

A profile is a folder sensors/<model>/ with
  sensor.yaml   the driver (ROS 2 launch command, source tree), its topics and
                battery topic -- enough for the generic metrics of any sensor
  decoder.py    optional: reads the sensor's own BLE notifications (sample
                timestamps, sequence numbers, file transfers) for metrics only
                the protocol can give: completeness, added delay, M(D)

The units used in a session are listed in config.yaml under `sensors:`
(profile, name, mac_address); load_config turns them into cfg["devices"],
one entry per unit, with the profile's settings and its decoder.

A decoder module may define (all optional):
  NOTIFICATIONS   {stream: {"uuid": <characteristic UUID substring>,
                            "att_handle": "0x00NN"}}; notifications of these
                  characteristics on the sensor's own connection are passed
                  to metrics(). The ATT handle is the fallback for reconnects
                  to a bonded device, where btmon shows no UUID.
  COLUMNS         names of the keys metrics() returns (results.csv columns)
  metrics(ctx)    -> dict of those keys; ctx is a NotificationContext
  summary_lines(parsed) -> lines for the per-run summary in session.log
  baseline_check(parsed) -> None, or the reason a baseline run did not
                  record new sensor data
  ARTEFACT_COLUMNS  columns left out of results_with_background.csv
  TIMED_STREAM    {"name": "ECG", "sampling_rate_hz": 130.0}: the stream
                  whose notifications carry the sensor's sample clock
  timed_frame(value: bytes) -> (sensor_time_s, n_samples) or None, for one
                  notification value of any characteristic; used by
                  analyze_session.py for added delay and deadline misses
"""

import importlib.util
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SENSORS_DIR = Path(__file__).resolve().parent.parent / "sensors"

# sensor.yaml keys copied into the device entry (cfg["devices"])
PROFILE_KEYS = ("address_type", "launch_cmd", "driver_source_dir", "battery_topic",
                "expected_topics", "advisory_topics")


@dataclass
class NotificationContext:
    """What a decoder's metrics() gets for one capture.

    frames[stream]         [(hex_value, host_ts_s, connection_index)] on the
                           sensor's own connections, whole capture
    window_frames[stream]  the same, only those inside the planned window
                           (from the first connection; the stream goes on
                           until the driver is stopped)
    text                   the full btmon -r text, for decoders that follow a
                           protocol across characteristics themselves
    """
    text: str
    frames: dict = field(default_factory=dict)
    window_frames: dict = field(default_factory=dict)
    planned_duration_s: "float | None" = None
    connected_duration_s: float = 0.0
    first_connect_ts: "float | None" = None


def _load_decoder(path: Path):
    spec = importlib.util.spec_from_file_location(f"sensor_decoder_{path.parent.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_profile(profile: str) -> dict:
    """sensor.yaml of sensors/<profile>/ (or a path to such a folder), with
    the decoder module under "decoder" (None without decoder.py)."""
    folder = Path(profile) if os.sep in profile else SENSORS_DIR / profile
    sensor_yaml = folder / "sensor.yaml"
    if not sensor_yaml.exists():
        raise FileNotFoundError(f"sensor profile {profile!r}: {sensor_yaml} not found")
    with open(sensor_yaml) as f:
        prof = yaml.safe_load(f) or {}
    decoder_py = folder / "decoder.py"
    prof["decoder"] = _load_decoder(decoder_py) if decoder_py.exists() else None
    prof["profile_dir"] = str(folder)
    return prof


def device_entries(units: list) -> list:
    """cfg["devices"] from config.yaml's `sensors:` list. In the profile's
    launch_cmd, {mac_address} is replaced by the unit's address."""
    devices = []
    for unit in units:
        prof = load_profile(unit["profile"])
        dev = {k: prof.get(k) for k in PROFILE_KEYS if prof.get(k) is not None}
        dev.update({k: v for k, v in unit.items() if k != "profile"})
        dev.setdefault("name", prof.get("name", unit["profile"]))
        if dev.get("launch_cmd") and dev.get("mac_address"):
            dev["launch_cmd"] = dev["launch_cmd"].replace("{mac_address}", dev["mac_address"])
        dev["profile"] = unit["profile"]
        dev["decoder"] = prof["decoder"]
        devices.append(dev)
    return devices


def decoder_for(cfg: dict, device_name: str):
    """The decoder module of a device of this session, or None."""
    for d in cfg.get("devices", []):
        if d["name"] == device_name:
            return d.get("decoder")
    return None


def decoder_columns(cfg: dict) -> list:
    """Every decoder's COLUMNS, in device order, without repeats."""
    cols = []
    for d in cfg.get("devices", []):
        for c in getattr(d.get("decoder"), "COLUMNS", ()):
            if c not in cols:
                cols.append(c)
    return cols
