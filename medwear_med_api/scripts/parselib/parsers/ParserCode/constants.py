folder_delimiter = "/"
folder_paths = [
    "corsano_bioz",
    "corsano_wrist"
]
data_type_per_corsano = {
    "corsano_bioz": [
        "bioz",
        "activity",
        "acc",
        "emography",
        "rr_interval",
        "temperature",
        "sleep",
    ],
    "corsano_wrist": [
        "ppg", 
        "activity",
         "acc",
         "rr_interval", 
        "temperature", 
        "sleep"
        ],
}
output_folder_paths = {
    "corsano_bioz": "corsano_bioz",
    "corsano_wrist": "corsano_wrist"
}

data_type_headers = {
    "bioz": ["timestamp", "date", "metric_id", "chunk_index", "value"],
    "ppg": [
        "timestamp",
        "date",
        "metric_id",
        "chunk_index",
        "quality",
        "body_pose",
        "led_pd_pos",
        "offset",
        "exp",
        "led",
        "gain",
        "value",
    ],
    "acc": [
        "timestamp",
        "date",
        "metric_id",
        "chunk_index",
        "quality",
        "body_pose",
        "accX",
        "accY",
        "accZ",
    ],
    "emography": [
        "timestamp",
        "date",
        "cz",
        "pcz",
        "pczt",
        "czh",
        "cc",
        "quality",
        "stress_skin",
        "stress_skin_quality",
    ],
    "rr_interval": ["timestamp", "date", "rr"],
    "temperature": ["timestamp", "date", "temp_sk1", "temp_sk2", "temp_amb"],
    "sleep": ["timestamp", "date", "sleep_state", "sleep_q"],
    "activity":["timestamp", "date","bpm", "bpm_q", "steps","activity_type","energy_exp", "resp_rate", "resp_rate_q", "wearing", "battery","charging", "sp02", "sp02_q"],
}
