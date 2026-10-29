# MedWear ROS 2 Bridge

Subscribes to ROS 2 topics carrying `healthcare_msgs` biosignals and streams
them into the MedWear API in real time.

## Supported messages

| Message (`healthcare_msgs/msg/…`) | MedWear signal | Samples field | Default unit | Channels |
|---|---|---|---|---|
| `ECG` | ECG | `ecg` | mV | `channel_size`, channel-major |
| `EEG` | EEG | `eeg` | uV | from `EEGInfo.channel_size`, channel-major |
| `EMG` | EMG | `emg` | mV | `channel_size`, channel-major |
| `EOG` | EOG | `eog` | uV | `channel_size`, channel-major |
| `ICG` | ICG | `icg` | Ohm | 1 |
| `BCG` | BCG | `bcg` | mV | 1 |
| `PPG` | PPG | `ppg` | counts | `wavelength_size`, sample-major |
| `EDA` | EDA | `eda` | uS | 1 |
| `HeartRate` | HR | `heart_rate` | bpm | 1 |
| `HeartRateVariability` | HRV | `hrv` | ms | 1 |
| `RRInterval` | RR_INTERVAL | `rr_interval` | ms | 1 |
| `RespiratoryRate` | RESPIRATORY_RATE | `respiratory_rate` | breaths/min | 1 |
| `Stress` | STRESS | `stress_level` | score | 1 |
| `BloodVolumePulse` | BVP | `bvp` | a.u. | 1 |
| `CardiacOutput` | CARDIAC_OUTPUT | `cardiac_output` | L/min | 1 |
| `StrokeVolume` | STROKE_VOLUME | `stroke_volume` | mL | 1 |
| `sensor_msgs/msg/Imu`, `geometry_msgs/msg/Vector3Stamped` | IMU | `linear_acceleration` / `vector` | m/s^2 / g | one per axis |

The table lives in `medwear_bridge/message_types.py`.

These are all `healthcare_msgs` types that carry sample arrays. Not
forwarded: the categorical and model-output messages (`Concentration`, `ERP`,
`MotorImagery`, `SleepStage`, `Mood`, `ActivityDailyLife`,
`ADLModelClassificationResult`), `PressureMap` (a 2-D map, not a time
series), and the descriptive messages (`DeviceInfo`, `MedicalDeviceSystem`,
the filter messages, `PlaneAngleUnitValue`), which have no samples of their
own; `DeviceInfo` is read from the `…Info` messages.

**Multi-channel messages** are uploaded as one batch per channel
(`schema_id` `ros2_<signal>_ch<N>_v1`), because a MedWear batch is a single
time series. Single-channel streams keep `ros2_<signal>_v1`.

**Info messages.** For every type with an `…Info` message, the bridge also
subscribes to `<topic>_info` (transient local, as the drivers latch it). From
it come:

- the device identity, sampling rate and unit (when `config.yaml` doesn't set them);
- per-channel labels (ECG leads, EEG electrode sites, PPG wavelengths);
- the signal-specific header block MedWear stores (`ecg_info`, `eeg_info`,
  `eda_info`, `ppg_info`, `bcg_info`, `emg_info`, `eog_info`, `icg_info`).
  Coded fields are stored by name (e.g. `chest`, `ii`, `fp1`).

## Configuring topics

`config.yaml` lists the topics to forward. `topic` and `msg_type` are
enough; every other key overrides the defaults above:

```yaml
topics:
  - topic: /polar_ecg
    msg_type: healthcare_msgs/msg/ECG
    batch_window_s: 1          # batch samples for W seconds (high-rate topics)
  - topic: /corsano/hr
    msg_type: healthcare_msgs/msg/HeartRate
    sampling_rate_hz: 1        # needed when no Info message gives a rate
```

| Key | Meaning |
|---|---|
| `signal_type`, `field`, `quality_field`, `unit` | override the defaults |
| `sampling_rate_hz` | needed if the Info message doesn't give it; without it, data is dropped (with one warning) until it does |
| `batch_window_s` | send a batch once its oldest sample has waited W seconds (`--batch-window` overrides) |
| `gap_threshold_s` | close a batch early at timestamp gaps longer than this (default 1 s) |
| `channels` | channel count, when neither the message nor its Info gives it |
| `info_topic`, `info_durability` | Info topic (default `<topic>_info`, `""` to disable) and its QoS (`transient_local` or `volatile`) |
| `axes` | IMU vector components to upload |
| `staleness_deadline_s`, `qos_depth` | staleness warning threshold, subscription depth |

Uploads run on background threads (`--upload-workers`), with retries for
transient errors. `--metrics-file` writes one JSON line per batch (timing and
the API's verdict). To replay a rosbag through the bridge, pass
`--use-sim-time` and play the bag with `ros2 bag play --clock`.

## Sessions

On the first batch of each stream (patient, device, signal, channel,
sampling rate, unit and the metadata from the `…Info` message), the bridge
registers the stream once with `POST /v1/data/session`. Every later batch of
that stream sends only `session_id`, its samples and time frame, the
idempotency key and the checksum. Each bridge run uses new sessions. If
registration fails, the bridge falls back to full batches, so no data is
lost. Use `--no-sessions` with MedWear versions that have no session
endpoint.

Transient failures (connection errors, 429, 5xx) are retried. When an earlier
attempt reached PostgreSQL but not InfluxDB, the retry completes that write
instead of being reported as a duplicate.

## Code layout

| File | Contents |
|---|---|
| `medwear_subscriber.py` | entry point: configuration, ROS node, command line |
| `medwear_bridge/message_types.py` | supported messages and their defaults |
| `medwear_bridge/info.py` | Info messages → header metadata |
| `medwear_bridge/handler.py` | per-topic handling: channels, batching, upload |
| `medwear_bridge/payload.py` | message → samples → batch payload |
| `medwear_bridge/buffering.py` | batch window |
| `medwear_bridge/uploader.py` | upload threads, metrics file |
| `medwear_bridge/client.py` | API client, token refresh, sessions |
| `medwear_bridge/tracking.py` | staleness and sequence-gap tracking |

## Running locally (with ROS 2 Jazzy installed)

```bash
source /opt/ros/jazzy/setup.bash
source /ros2_ws/install/setup.bash   # healthcare_msgs built in a workspace

cd ros2_to_medwear
MEDWEAR_USERNAME=<user> MEDWEAR_PASSWORD=<password> API_BASE_URL=https://localhost/v1 \
python3 medwear_subscriber.py \
    --config  config.yaml \
    --device  wristband-01 \
    --patient 1 \
    --project medwear_demo
```

## Running in Docker

```bash
cd ros2_to_medwear

docker build -t medwear-ros2-bridge .

docker run --rm \
    --network host \
    -e MEDWEAR_TOKEN=<your_JWT_token> \
    -e API_BASE_URL=https://localhost/v1 \
    medwear-ros2-bridge \
    --device wristband-01 --patient 1 --project medwear_demo
```

`--network host` is required so the container can reach the ROS 2 DDS network on the recording computer.
Use the HTTPS address of the API: nginx redirects plain HTTP, and a
redirected upload would be lost. The bridge accepts MEDWEAR's self-signed
certificate.
The image builds `healthcare_msgs` from GitHub; message types that exist only
in a local working copy must be built into the image separately.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MEDWEAR_USERNAME`, `MEDWEAR_PASSWORD` | — | Keycloak login; the token is refreshed automatically |
| `MEDWEAR_TOKEN` | — | static JWT instead of a login (expires) |
| `API_BASE_URL` | `http://medwear-nginx/v1` | MedWear API base URL |

## Tests

Both tests run the bridge against a mock API (`test/mock_api.py`), so no
MEDWEAR installation is needed.

| Test | What it checks |
|---|---|
| `test/run_test.sh` | one EEG and one HR message reach the API |
| `test/run_all_types_test.sh` | every supported type: `publish_all_types.py` sends an Info message and four data messages per type (multi-channel ECG, EEG, EMG and EOG, two-wavelength PPG); `check_all_types.py` checks the channels, the sample counts, the channel separation and the Info metadata in the batch headers. `--no-sessions` runs it with full batches |

In Docker (builds the bridge image and a test image, then runs both):

```bash
./run_integration_test.sh
```

Locally, with ROS 2 and `healthcare_msgs` sourced:

```bash
source /opt/ros/jazzy/setup.bash && source <ws>/install/setup.bash
test/run_all_types_test.sh                 # with sessions
test/run_all_types_test.sh --no-sessions   # full batches
```
