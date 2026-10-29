# MedWear ROS 2 Bridge

Subscribes to ROS 2 topics and streams biosignal data into the MedWear API in real time.

## Supported signal types

| ROS 2 topic       | Message type                              | Signal |
|-------------------|-------------------------------------------|--------|
| `/eeg/data`       | `healthcare_msgs/msg/EEG`                 | EEG    |
| `/eda/data`       | `healthcare_msgs/msg/EDA`                 | EDA    |
| `/heart_rate/data`| `healthcare_msgs/msg/HeartRate`           | HR     |
| `/hrv/data`       | `healthcare_msgs/msg/HeartRateVariability`| HRV    |
| `/polar_acc`, `/corsano/acceleration` | `geometry_msgs/msg/Vector3Stamped`, `sensor_msgs/msg/Imu` | IMU (one batch per axis) |

High-rate topics that publish one sample per message (e.g. Polar ECG/ACC)
set `batch_window_s` in `config.yaml`. The bridge then buffers contiguous
samples into batches and splits them at stamp gaps larger than
`gap_threshold_s`. Uploads run on a background thread, with retries for
transient errors.

To replay a rosbag through the bridge, pass `--use-sim-time` and play the
bag with `ros2 bag play --clock`. See `wireless_experiments_replay_medwear/`.

## Running locally (with ROS 2 Jazzy installed)

```bash
source /opt/ros/jazzy/setup.bash
source /ros2_ws/install/setup.bash   # if healthcare_msgs built in a workspace

cd ros2_to_medwear
python3 medwear_subscriber.py \
    --config  config.yaml \
    --token   <your_JWT_token> \
    --device  corsano-001 \
    --patient 1 \
    --project tjalf-bci
```

## Running in Docker

```bash
cd ros2_to_medwear

docker build -t medwear-ros2-bridge .

docker run --rm \
    --network host \
    -e MEDWEAR_TOKEN=<your_JWT_token> \
    -e API_BASE_URL=http://localhost/v1 \
    medwear-ros2-bridge \
    --device corsano-001 --patient 1 --project tjalf-bci
```

`--network host` is required so the container can reach the ROS 2 DDS network on the recording computer.

## Sessions

On the first batch of each stream (patient, device, signal, `schema_id`,
sampling rate, unit), the bridge registers the stream's metadata once with
`POST /v1/data/session`. Every later batch of that stream sends only
`session_id`, its samples and time frame, the idempotency key and the
checksum. Each bridge run uses new sessions. If registration fails, the
bridge falls back to full batches, so no data is lost. Use `--no-sessions`
with MedWear versions that have no session endpoint.

Transient failures (connection errors, 429, 5xx) are retried. When an earlier
attempt reached PostgreSQL but not InfluxDB, the retry completes that write
instead of being reported as a duplicate.

## Environment variables

| Variable        | Default                        | Description              |
|-----------------|--------------------------------|--------------------------|
| `MEDWEAR_TOKEN` | (required)                     | JWT bearer token         |
| `API_BASE_URL`  | `http://medwear-nginx/v1`      | MedWear API base URL     |

## Customising topics

Edit `config.yaml` — add/remove topics, change sampling rates, field names, and units.
