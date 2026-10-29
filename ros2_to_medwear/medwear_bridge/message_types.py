"""Which ROS 2 messages the bridge understands and how they map onto MedWear.

Every healthcare_msgs type that carries a biosignal array is listed here, so
a config.yaml entry only needs `topic` and `msg_type`; any key given there
(signal_type, field, quality_field, unit, sampling_rate_hz, ...) overrides
the defaults below. The categorical and model-output messages (Concentration,
ERP, MotorImagery, SleepStage, Mood, ActivityDailyLife,
ADLModelClassificationResult) and PressureMap are not supported.

Channel layouts (from the .msg comments):
  channel_major  [channel]x[sample]: value of channel c, sample s at
                 c * samples_per_channel + s (ECG, EEG, EMG, EOG)
  sample_major   [sample]x[channel]: at s * channels + c (PPG wavelengths)
  flat           one channel
Multi-channel messages are uploaded as one batch per channel, because a
MedWear batch is a single time series (its sample count is checked against
duration x sampling rate).
"""

from dataclasses import dataclass, field

HC = "healthcare_msgs/msg/"


@dataclass(frozen=True)
class DataType:
    signal_type: str                 # MedWear SignalType
    field: str                       # message field holding the samples
    quality_field: "str | None"      # per-sample quality, if any
    unit: "str | None"               # fallback when neither config nor Info gives one
    info_type: "str | None" = None   # matching ...Info message (metadata)
    layout: str = "flat"             # flat | channel_major | sample_major
    channels_field: "str | None" = None   # message field with the channel count
    axes: tuple = field(default_factory=tuple)   # IMU: one channel per vector component


DATA_TYPES: "dict[str, DataType]" = {
    # Raw biosignals
    HC + "ECG": DataType("ECG", "ecg", "signal_quality", "mV", HC + "ECGInfo", "channel_major", "channel_size"),
    HC + "EEG": DataType("EEG", "eeg", "signal_quality", "uV", HC + "EEGInfo", "channel_major"),  # count in EEGInfo
    HC + "EMG": DataType("EMG", "emg", "signal_quality", "mV", HC + "EMGInfo", "channel_major", "channel_size"),
    HC + "EOG": DataType("EOG", "eog", "signal_quality", "uV", HC + "EOGInfo", "channel_major", "channel_size"),
    HC + "ICG": DataType("ICG", "icg", "signal_quality", "Ohm", HC + "ICGInfo"),
    HC + "BCG": DataType("BCG", "bcg", "signal_quality", "mV", HC + "BCGInfo"),
    HC + "PPG": DataType("PPG", "ppg", "signal_quality", "counts", HC + "PPGInfo", "sample_major", "wavelength_size"),
    HC + "EDA": DataType("EDA", "eda", "signal_quality", "uS", HC + "EDAInfo"),
    # Derived biosignals
    HC + "HeartRate": DataType("HR", "heart_rate", "quality", "bpm", HC + "HeartRateInfo"),
    HC + "HeartRateVariability": DataType("HRV", "hrv", "quality", "ms", HC + "HeartRateVariabilityInfo"),
    HC + "RRInterval": DataType("RR_INTERVAL", "rr_interval", "quality", "ms", HC + "RRIntervalInfo"),
    HC + "RespiratoryRate": DataType("RESPIRATORY_RATE", "respiratory_rate", "quality", "breaths/min",
                                     HC + "RespiratoryRateInfo"),
    HC + "Stress": DataType("STRESS", "stress_level", "quality", "score", HC + "StressInfo"),
    HC + "BloodVolumePulse": DataType("BVP", "bvp", "quality", "a.u.", HC + "BloodVolumePulseInfo"),
    HC + "CardiacOutput": DataType("CARDIAC_OUTPUT", "cardiac_output", "quality", "L/min",
                                   HC + "CardiacOutputInfo"),
    HC + "StrokeVolume": DataType("STROKE_VOLUME", "stroke_volume", "quality", "mL", HC + "StrokeVolumeInfo"),
    # Inertial data from standard ROS messages
    "sensor_msgs/msg/Imu": DataType("IMU", "linear_acceleration", None, "m/s^2", axes=("x", "y", "z")),
    "geometry_msgs/msg/Vector3Stamped": DataType("IMU", "vector", None, "g", axes=("x", "y", "z")),
}


def import_message(type_str: str):
    """Import a message class from its "pkg/msg/Name" string."""
    from rosidl_runtime_py.utilities import get_message
    return get_message(type_str)


def resolve_topic_config(topic_cfg: dict) -> dict:
    """config.yaml entry merged over the registry defaults of its msg_type.
    Unknown msg_types are passed through: the entry must then give
    signal_type, field and unit itself (as before the registry existed)."""
    spec = DATA_TYPES.get(topic_cfg["msg_type"])
    if spec is None:
        return dict(topic_cfg)
    merged = {
        "signal_type": spec.signal_type,
        "field": spec.field,
        "quality_field": spec.quality_field,
        "unit": spec.unit,
        "layout": spec.layout,
        "channels_field": spec.channels_field,
        "info_type": spec.info_type,
    }
    if spec.axes:
        merged["axes"] = list(spec.axes)
    if spec.info_type:
        merged["info_topic"] = f"{topic_cfg['topic']}_info"   # drivers' naming, e.g. /polar_ecg_info
    merged.update({k: v for k, v in topic_cfg.items() if v is not None})
    # keys the user set themselves win over the Info message (handler.py)
    merged["_explicit_keys"] = sorted(k for k, v in topic_cfg.items() if v is not None)
    return merged
