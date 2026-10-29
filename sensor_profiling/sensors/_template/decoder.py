"""Decoder template: reads a sensor's own BLE notifications for metrics only
its protocol can give. Every hook is optional; delete decoder.py entirely if
the generic link metrics are enough. The interface is described in
session_lib/sensors.py; sensors/polar_h10/decoder.py (timed ECG frames) and
sensors/corsano_287_2b/decoder.py (a polled file protocol) are complete
examples.

As written, this template counts the values of the standard Heart Rate
Measurement characteristic (0x2A37) per minute connected. Fill in the
characteristic's ATT handle for your sensor (see NOTIFICATIONS), or it is
found only on a first connection.
"""

# Characteristics whose notifications are collected on the sensor's own
# connection: {stream: {"uuid": ..., "att_handle": ...}}. "uuid" is matched
# against btmon's "Type:" line (part of the 128-bit UUID is enough);
# "att_handle" is the fallback for reconnects to a bonded device, where
# BlueZ uses its cached GATT table and btmon shows no UUID. Find both in a
# capture: btmon -r <run>.btsnoop | grep -B2 -A3 "Handle Value Notification"
NOTIFICATIONS = {
    # att_handle: the sensor's own handle of this characteristic, e.g. "0x0010"
    # on the Polar H10 (fixed by its firmware)
    "hr": {"uuid": "2a37", "att_handle": None},
}

# Every key metrics() returns: the sensor's columns in results.csv
COLUMNS = ["hr_notification_count", "hr_values_per_connected_min"]

# Optional: results.csv columns that are artefacts of how the driver reads
# the sensor, left out of results_with_background.csv
ARTEFACT_COLUMNS = ()


def _hr_values(hex_data):
    """Heart-rate values in one Heart Rate Measurement notification (one per
    notification; the flags byte says 8 or 16 bit)."""
    try:
        data = bytes.fromhex(hex_data)
    except ValueError:
        return 0
    return 1 if len(data) >= (2 if data[0] & 1 == 0 else 3) else 0


def metrics(ctx):
    """ctx (session_lib/sensors.py NotificationContext):
      ctx.frames[stream]         [(hex_value, host_ts_s, connection_index)], whole capture
      ctx.window_frames[stream]  the same, inside the planned window
      ctx.planned_duration_s, ctx.connected_duration_s, ctx.first_connect_ts
      ctx.text                   the full `btmon -r` output
    Returns a dict with exactly the COLUMNS keys (None where not measurable)."""
    frames = ctx.frames.get("hr", [])
    n = sum(_hr_values(h) for h, _ts, _c in frames)
    minutes = (ctx.connected_duration_s or 0) / 60
    return {
        "hr_notification_count": len(frames),
        "hr_values_per_connected_min": round(n / minutes, 2) if minutes > 0 else None,
    }


def summary_lines(parsed):
    """Lines for the per-run summary in session.log: [(level, text)], level
    "info" or "warning"."""
    if not parsed.get("hr_notification_count"):
        return [("warning", "no heart-rate notifications in this run")]
    return [("info", f"heart rate: {parsed['hr_values_per_connected_min']} values "
                     f"per connected minute")]


def baseline_check(parsed):
    """Optional: None if a baseline run recorded real sensor data, else the
    reason it did not (the run then fails). Use it when the sensor can be
    connected and streaming without measuring (e.g. off the body)."""
    return None


# Optional, for sensors whose notifications carry the sensor's own sample
# clock: analyze_session.py then computes completeness, added delay
# (host arrival - sensor timestamp), deadline misses M(D) and runaways.
# TIMED_STREAM names the stream and its sampling rate; timed_frame gets the
# first bytes of every notification value (at least 9) and its full length,
# and returns (sensor time of the frame's last sample in s, samples in the
# frame), or None for notifications that aren't such frames. The Polar
# H10's ECG is the worked example (sensors/polar_h10/decoder.py).
#
# TIMED_STREAM = {"name": "ECG", "sampling_rate_hz": 250.0}
#
# def timed_frame(value_start, value_len):
#     if value_start[0] != 0x01:
#         return None
#     return int.from_bytes(value_start[1:9], "little") / 1e6, (value_len - 9) // 2
