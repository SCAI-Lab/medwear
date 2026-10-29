#!/usr/bin/env python3
"""Publish one Info message and a few data messages for every healthcare_msgs
type the bridge supports (topics as in all_types_config.yaml), including
multi-channel ECG/EEG/EMG/EOG and two-wavelength PPG. Used by
run_all_types_test.sh to check the bridge end to end against the mock API."""

import math
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

import healthcare_msgs.msg as hm

FS = 100.0              # Hz, all raw signals in this test
N = 50                  # samples per channel per message
MESSAGES = 4            # data messages per topic (0.5 s each)

# topic, data type, data field, quality field, channels, channel field, Info setup
RAW = [
    ("/test/ecg", hm.ECG, "ecg", "signal_quality", 2, "channel_size",
     lambda i: (setattr(i, "lead_type", [hm.ECGInfo.LEAD_I, hm.ECGInfo.LEAD_II]),
                setattr(i, "units", hm.ECGInfo.UNIT_MV))),
    ("/test/eeg", hm.EEG, "eeg", "signal_quality", 3, None,
     lambda i: (setattr(i, "channel_size", 3), setattr(i, "units", hm.EEGInfo.UNIT_UV),
                setattr(i, "electrode_sites", [hm.EEGInfo.ELECTRODE_FP1, hm.EEGInfo.ELECTRODE_CZ,
                                               hm.EEGInfo.ELECTRODE_O2]))),
    ("/test/emg", hm.EMG, "emg", "signal_quality", 2, "channel_size",
     lambda i: setattr(i, "units", hm.EMGInfo.UNIT_MV)),
    ("/test/eog", hm.EOG, "eog", "signal_quality", 2, "channel_size",
     lambda i: setattr(i, "units", hm.EOGInfo.UNIT_UV)),
    ("/test/icg", hm.ICG, "icg", "signal_quality", 1, None,
     lambda i: setattr(i, "units", hm.ICGInfo.UNIT_OHMS)),
    ("/test/bcg", hm.BCG, "bcg", "signal_quality", 1, None,
     lambda i: setattr(i, "units", hm.BCGInfo.UNIT_MV)),
    ("/test/ppg", hm.PPG, "ppg", "signal_quality", 2, "wavelength_size",
     lambda i: (setattr(i, "wavelengths_nm", [530, 940]), setattr(i, "units", hm.PPGInfo.UNIT_COUNTS))),
    ("/test/eda", hm.EDA, "eda", "signal_quality", 1, None, lambda i: None),
]
# derived types: one value per message
DERIVED = [
    ("/test/hr", hm.HeartRate, "heart_rate"), ("/test/hrv", hm.HeartRateVariability, "hrv"),
    ("/test/rri", hm.RRInterval, "rr_interval"), ("/test/resp", hm.RespiratoryRate, "respiratory_rate"),
    ("/test/stress", hm.Stress, "stress_level"), ("/test/bvp", hm.BloodVolumePulse, "bvp"),
    ("/test/co", hm.CardiacOutput, "cardiac_output"), ("/test/sv", hm.StrokeVolume, "stroke_volume"),
]


def main() -> None:
    rclpy.init()
    node = Node("all_types_publisher")
    latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
    pubs = []
    for topic, typ, field, qfield, nch, chfield, setup in RAW:
        info_cls = getattr(hm, typ.__name__ + "Info")
        info = info_cls()
        info.device_info.device_manufacturer = "TestCo"
        info.device_info.device_name = typ.__name__ + "-sim"
        info.device_info.acquisition_rate_hz = int(FS)
        setup(info)
        node.create_publisher(info_cls, topic + "_info", latched).publish(info)
        pubs.append((node.create_publisher(typ, topic, 10), typ, field, qfield, nch, chfield))
    derived = [(node.create_publisher(t, topic, 10), t, f) for topic, t, f in DERIVED]
    time.sleep(3.0)                       # discovery

    t0 = time.time()
    for k in range(MESSAGES):
        stamp = node.get_clock().now().to_msg()
        for pub, typ, field, qfield, nch, chfield in pubs:
            m = typ()
            m.header.stamp = stamp
            m.session_id = "test-session"
            m.sample_size = N
            if chfield:
                setattr(m, chfield, nch)
            if typ is hm.PPG:     # sample-major: [sample][wavelength]
                vals = [math.sin((k * N + s) / 10) + 2 * c for s in range(N) for c in range(nch)]
            else:                 # channel-major: [channel][sample]
                vals = [math.sin((k * N + s) / 10) + 2 * c for c in range(nch) for s in range(N)]
            setattr(m, field, vals)
            setattr(m, qfield, [1.0] * len(vals))
            pub.publish(m)
        for pub, typ, field in derived:
            m = typ()
            m.header.stamp = stamp
            m.session_id = "test-session"
            m.sample_size = 1
            setattr(m, field, [60.0 + k])
            pub.publish(m)
        time.sleep(max(0.0, t0 + (k + 1) * N / FS - time.time()))
    time.sleep(2.0)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
