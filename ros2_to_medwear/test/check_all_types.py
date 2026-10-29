#!/usr/bin/env python3
"""Check what the mock API received in run_all_types_test.sh against what
publish_all_types.py sent: every supported type arrives, multi-channel
messages are split into the right channels with the right samples, and the
Info metadata reaches the batch header (via the session, or in full batches
with --no-sessions).

  check_all_types.py OUT_DIR      (received_batches.json, received_sessions.json)
"""
import json
import os
import statistics
import sys

MESSAGES, N = 4, 50          # as in publish_all_types.py
RAW = {"ECG": 2, "EEG": 3, "EMG": 2, "EOG": 2, "ICG": 1, "BCG": 1, "PPG": 2, "EDA": 1}
DERIVED = ["HR", "HRV", "RR_INTERVAL", "RESPIRATORY_RATE", "STRESS", "BVP",
           "CARDIAC_OUTPUT", "STROKE_VOLUME"]
INFO_KEY = {"ECG": "ecg_info", "EEG": "eeg_info", "EMG": "emg_info", "EOG": "eog_info",
            "ICG": "icg_info", "BCG": "bcg_info", "PPG": "ppg_info", "EDA": "eda_info"}

out = sys.argv[1]
batches = json.load(open(os.path.join(out, "received_batches.json")))
sessions_path = os.path.join(out, "received_sessions.json")
sessions = json.load(open(sessions_path)) if os.path.exists(sessions_path) else {}

streams = {}    # (signal_type, schema_id) -> {"samples": [...], "header": {...}}
for b in batches:
    if "session_id" in b:
        s = sessions[b["session_id"]]
        signal, header = s["signal_type"], s["header"]
    else:
        signal, header = b["signal_type"], b["header"]
    body = b["body"]
    samples = body.get("signal_data") or body.get("ecg_lead_data") or body.get("bcg_sensor_readings") or []
    st = streams.setdefault((signal, header["schema_id"]), {"samples": [], "header": header})
    st["samples"] += samples

failures = []
def check(ok, msg):
    print(("ok    " if ok else "FAIL  ") + msg)
    if not ok:
        failures.append(msg)

for signal, nch in RAW.items():
    chans = sorted(k[1] for k in streams if k[0] == signal)
    check(len(chans) == nch, f"{signal}: {len(chans)} channel stream(s), expected {nch}: {chans}")
    for c, schema in enumerate(chans):
        st = streams[(signal, schema)]
        n = len(st["samples"])
        check(n == MESSAGES * N, f"{signal} {schema}: {n} samples, expected {MESSAGES * N}")
        if nch > 1 and n:
            mean = statistics.mean(st["samples"])
            check(abs(mean - 2 * c) < 0.5, f"{signal} {schema}: mean {mean:.2f}, expected about {2 * c} (channel {c})")
        h = st["header"]
        check(bool((h.get("device_identity") or {}).get("device_manufacturer")),
              f"{signal} {schema}: device_identity in header")
        if signal in ("ECG", "EEG", "PPG"):
            check(bool(h.get(INFO_KEY[signal])), f"{signal} {schema}: {INFO_KEY[signal]} = {h.get(INFO_KEY[signal])}")

for signal in DERIVED:
    found = [k for k in streams if k[0] == signal]
    n = sum(len(streams[k]["samples"]) for k in found)
    check(n == MESSAGES, f"{signal}: {n} value(s), expected {MESSAGES}")

print(f"\n{len(batches)} batches, {len(sessions)} sessions, {len(streams)} streams; "
      f"{'all checks passed' if not failures else str(len(failures)) + ' check(s) failed'}")
sys.exit(1 if failures else 0)
