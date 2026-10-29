"""Sensor profiles load, and every decoder returns exactly the columns it
declares (results.csv depends on it). No hardware or captures needed:
  cd sensor_profiling && python3 -m pytest tests/"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_lib.btmon_parse import parse_btmon_text  # noqa: E402
from session_lib.sensors import SENSORS_DIR, NotificationContext, device_entries, load_profile  # noqa: E402

PROFILES = sorted(p.name for p in SENSORS_DIR.iterdir() if (p / "sensor.yaml").exists())


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_loads(profile):
    prof = load_profile(profile)
    assert prof.get("name") and prof.get("launch_cmd"), "sensor.yaml needs name and launch_cmd"
    assert "{mac_address}" in prof["launch_cmd"], "launch_cmd should take {mac_address}"


@pytest.mark.parametrize("profile", PROFILES)
def test_decoder_columns(profile):
    dec = load_profile(profile)["decoder"]
    if dec is None or not hasattr(dec, "metrics"):
        pytest.skip("no decoder metrics")
    out = dec.metrics(NotificationContext(text="", frames={s: [] for s in getattr(dec, "NOTIFICATIONS", {})},
                                          window_frames={s: [] for s in getattr(dec, "NOTIFICATIONS", {})},
                                          planned_duration_s=300, connected_duration_s=0.0))
    assert set(out) == set(dec.COLUMNS)
    for level, _text in getattr(dec, "summary_lines", lambda p: [])(out):
        assert level in ("info", "warning")


@pytest.mark.parametrize("profile", PROFILES)
def test_parse_empty_capture(profile):
    """The generic parser plus the decoder on a capture with no traffic."""
    dec = load_profile(profile)["decoder"]
    parsed = parse_btmon_text("", mac_address="AA:BB:CC:DD:EE:01", planned_duration_s=300, decoder=dec)
    assert parsed["disconnect_count"] == 0
    assert set(getattr(dec, "COLUMNS", ())) <= set(parsed)


def test_timed_frame_polar():
    dec = load_profile("polar_h10")["decoder"]
    value = bytes([0x00]) + (5_000_000_000).to_bytes(8, "little") + bytes([0x00]) + bytes(3 * 73)
    assert dec.timed_frame(value[:10], len(value)) == (5.0, 73)
    assert dec.timed_frame(bytes([0x02]) + bytes(9), 40) is None   # ACC frame, not timed


def test_device_entries_fill_address():
    devs = device_entries([{"profile": "polar_h10", "name": "H10", "mac_address": "AA:BB:CC:DD:EE:01"}])
    assert devs[0]["name"] == "H10" and "AA:BB:CC:DD:EE:01" in devs[0]["launch_cmd"]
    assert devs[0]["decoder"] is not None
