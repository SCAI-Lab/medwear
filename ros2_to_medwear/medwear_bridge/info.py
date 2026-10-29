"""healthcare_msgs ...Info messages -> MedWear batch-header metadata.

An Info message is published once per session (transient-local) next to its
data topic. From it the bridge takes the device identity, the sampling rate
and unit when config.yaml doesn't set them, the channel count and per-channel
labels (ECG leads, EEG sites, PPG wavelengths), and the signal-specific
header block MedWear stores (ecg_info, eeg_info, ...). Coded fields
(e.g. PLACEMENT_CHEST = 3) are decoded with the message's own constants and
stored by name ("chest").
"""

import logging

log = logging.getLogger("medwear_bridge")

# Info type -> (MedWear header key, {header field: (message field, constant prefix,
# prefixes to exclude, is_list)}). A prefix of None stores the raw value.
INFO_FIELDS = {
    "ECGInfo": ("ecg_info", {
        "electrode_placement": ("electrode_placement", "PLACEMENT_", (), True),
        "lead_type": ("lead_type", "LEAD_", (), True),
    }),
    "EEGInfo": ("eeg_info", {
        "channel_size": ("channel_size", None, (), False),
        "montage_type": ("montage_type", "MONTAGE_TYPE_", (), False),
        "electrode_sites": ("electrode_sites", "ELECTRODE_", ("ELECTRODE_PHYSICAL_",), True),
        "selected_preprocessing": ("selected_preprocessing", "EEG_PREPROC_", (), True),
    }),
    "EDAInfo": ("eda_info", {
        "electrode_placement": ("electrode_placement", "PLACEMENT_", (), True),
        "electrode_type": ("electrode_type", "ELECTRODE_TYPE_", (), True),
        "measuring_technique": ("measuring_technique", "TECHNIQUE_", (), False),
    }),
    "PPGInfo": ("ppg_info", {
        "sensor_placement": ("sensor_placement", "PLACEMENT_", (), False),
        "wavelengths_nm": ("wavelengths_nm", None, (), True),
        "wavelengths_labels": ("wavelengths_labels", "WAVELENGTH_", (), True),
        "measuring_mode": ("measuring_mode", "MODE_", (), False),
    }),
    "BCGInfo": ("bcg_info", {
        "sensor_placement": ("sensor_placement", "PLACEMENT_", (), False),
        "sensing_direction": ("sensing_direction", "DIRECTION_", (), False),
        "sensing_modality": ("sensing_modality", "MODALITY_", (), False),
        "measurement_system": ("measurement_system", "SYSTEM_", (), False),
    }),
    "EMGInfo": ("emg_info", {
        "electrode_placement": ("electrode_placement", "PLACEMENT_", (), True),
        "electrode_type": ("electrode_type", "ELECTRODE_", (), True),
        "signal_mode": ("signal_mode", "MODE_", (), False),
    }),
    "EOGInfo": ("eog_info", {
        "electrode_placement": ("electrode_placement", "PLACEMENT_", (), True),
        "lead_type": ("lead_type", "LEAD_", (), True),
        "signal_mode": ("signal_mode", "MODE_", (), False),
    }),
    "ICGInfo": ("icg_info", {
        "electrode_placement": ("electrode_placement", "PLACEMENT_", (), True),
        "lead_type": ("lead_type", "LEAD_", (), True),
        "technique": ("technique", "TECHNIQUE_", (), False),
    }),
}

# Per-channel label per Info type: which decoded list names the channels
CHANNEL_LABELS = {
    "ECGInfo": "lead_type", "EEGInfo": "electrode_sites", "EMGInfo": "electrode_placement",
    "EOGInfo": "electrode_placement", "PPGInfo": "wavelengths_nm",
}

# Unit constants (UNIT_MV, ...) -> MedWear unit strings
UNIT_NAMES = {
    "MV": "mV", "UV": "uV", "COUNTS": "counts", "M_S2": "m/s^2", "NEWTON": "N",
    "OHMS": "Ohm", "MOHMS": "mOhm", "RLU": "RLU", "BPM": "bpm", "HZ": "Hz",
}


def _constants(msg_class, prefix: str, exclude: tuple) -> "dict[int, str]":
    """{value: name} of the message's constants starting with prefix."""
    out = {}
    for name in dir(msg_class):
        if name.isupper() and name.startswith(prefix) and not name.startswith(exclude or ("\0",)):
            value = getattr(msg_class, name)
            if isinstance(value, int) and value not in out:
                out[value] = name[len(prefix):].lower()
    return out


def decode_info(info_type: str, msg_class, msg_dict: dict) -> dict:
    """Return {"device_identity", "sampling_rate_hz", "unit", "channel_count",
    "channel_labels", "header_key", "header_info"} from one Info message."""
    short = info_type.rsplit("/", 1)[-1]
    dev = msg_dict.get("device_info") or {}
    out = {
        "device_identity": {k: dev.get(k) or None for k in
                            ("device_manufacturer", "device_name", "device_type", "device_serial_number")},
        "sampling_rate_hz": float(dev.get("acquisition_rate_hz") or 0) or None,
        "unit": dev.get("measuring_unit") or None,
        "channel_count": None, "channel_labels": None, "header_key": None, "header_info": {},
    }
    if not any(out["device_identity"].values()):
        out["device_identity"] = None
    # a coded unit (UNIT_MV ...) when device_info has no measuring_unit
    code_field = "units" if "units" in msg_dict else ("unit" if "unit" in msg_dict else None)
    if out["unit"] is None and code_field is not None:
        name = _constants(msg_class, "UNIT_", ()).get(msg_dict[code_field])
        if name and name != "unknown":
            out["unit"] = UNIT_NAMES.get(name.upper(), name)

    header_key, fields = INFO_FIELDS.get(short, (None, {}))
    info = {}
    for hdr_field, (msg_field, prefix, exclude, is_list) in fields.items():
        if msg_field not in msg_dict:
            continue
        raw = msg_dict[msg_field]
        if prefix is None:
            value = list(raw) if is_list else raw
        else:
            names = _constants(msg_class, prefix, exclude)
            decode = lambda v: names.get(v, str(v))
            value = [decode(v) for v in raw] if is_list else decode(raw)
        # unset coded fields decode to "unknown": not worth storing
        if value in (None, [], "", "unknown") or (isinstance(value, list) and all(v == "unknown" for v in value)):
            continue
        if hdr_field == "wavelengths_nm":
            value = [int(v) for v in value]
        info[hdr_field] = value
    out["header_key"], out["header_info"] = header_key, info

    if short == "EEGInfo" and msg_dict.get("channel_size"):
        out["channel_count"] = int(msg_dict["channel_size"])
    label_field = CHANNEL_LABELS.get(short)
    if label_field and info.get(label_field):
        out["channel_labels"] = [str(v) for v in info[label_field]]
    return out


def channel_header(decoded: "dict | None", channel_index: "int | None", n_channels: int) -> dict:
    """Header extras for one channel's batch: device identity plus the
    signal-specific block, with list fields that describe every channel
    (length == n_channels) narrowed to this channel."""
    if not decoded:
        return {}
    extras = {}
    if decoded.get("device_identity"):
        extras["device_identity"] = decoded["device_identity"]
    info = dict(decoded.get("header_info") or {})
    if info and decoded.get("header_key"):
        if channel_index is not None and n_channels > 1:
            for k, v in list(info.items()):
                if isinstance(v, list) and len(v) == n_channels:
                    info[k] = [v[channel_index]]
            if "channel_size" in info:
                info["channel_size"] = 1
        extras[decoded["header_key"]] = info
    return extras
