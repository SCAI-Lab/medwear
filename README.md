# MEDWEAR

MEDWEAR is a self-hosted platform for collecting data from wearable and
clinical sensors in research studies. Devices and users upload sensor batches
over an HTTPS API. MEDWEAR stores the samples in InfluxDB, their metadata in
one PostgreSQL database per project and raw files in MinIO, with Keycloak for
accounts and roles. It includes a web interface, an audit log, soft delete,
research exports (WFDB, EDF+, CSV) and GDPR endpoints.

| Component | Role | Address after setup |
|---|---|---|
| nginx | HTTPS entry point | `https://<host>` (web interface), `https://<host>/v1` (API), `https://<host>/docs` (interactive API documentation) |
| med-api | FastAPI application | behind nginx |
| PostgreSQL 15 | batch metadata, one database per project | `localhost:5433` |
| InfluxDB 2.7 | samples | `http://localhost:8086` |
| MinIO | raw files | `http://localhost:9001` (console) |
| Keycloak 23 | accounts, roles, tokens | `http://localhost:8180` (admin console) |
| Grafana, pgAdmin, Uptime Kuma | dashboards, database browser, uptime checks | `http://localhost:3001`, `:5050`, `:3002` |

The API reference is in [docs/API.md](docs/API.md).

## Requirements

- Linux or macOS with Docker Engine and the Docker Compose plugin (or Docker Desktop)
- Python 3 (`setup.sh` uses it to generate passwords)
- 4 CPU cores and 8 GB of memory for a small installation
- Free ports 80 and 443 (web interface and API) and 5433, 8086, 9000, 9001,
  8180, 3001, 3002 and 5050 (administration; see
  [Running on a server](#running-on-a-server))

## Installation

```bash
git clone https://github.com/SCAI-Lab/medwear.git
cd medwear
./setup.sh
```

`setup.sh` asks for the server's hostname and IP address (press Enter for
`localhost` and `127.0.0.1` on your own machine) and then:

1. generates random passwords and tokens for every service and writes them
   to `.env.medwear` (read by the services) and `.env` (read by Docker Compose);
2. builds and starts the stack;
3. initialises InfluxDB, the MinIO buckets and the API's MinIO account;
4. sets a password for the Keycloak user `global_admin`;
5. connects Grafana to InfluxDB and loads its dashboard;
6. writes every account and password to `.credentials.txt`.

The first run takes a few minutes because the images are built. `.env`,
`.env.medwear` and `.credentials.txt` are ignored by git; keep them private.
Running `setup.sh` again keeps the existing `.env.medwear` and its passwords.

## First steps

1. **Open the web interface** at `https://localhost` (or `https://<host>`).
   The certificate is self-signed, so the browser asks you once to accept it.
2. **Log in** as `global_admin` with the password from `.credentials.txt`.
3. **Create a project** in the Admin tab, for example `demo` (stored as
   `medwear_demo`). Each project gets its own database.
4. **Add users** in the Admin tab and give each a role: `researcher` to upload
   and analyse data, `collaborator` for read-only access, `device` for a
   sensor or gateway that only uploads, `project_admin` to manage the users
   of a project.
5. **Upload a test batch** and find it in the Query Data tab:

   ```bash
   TOKEN=$(curl -sk -X POST https://localhost/v1/data/auth/token \
     -H "Content-Type: application/json" \
     -d '{"username":"global_admin","password":"<password from .credentials.txt>"}' \
     | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

   curl -sk -X POST https://localhost/v1/data/batch \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"header": {"uuid": "'"$(python3 -c 'import uuid; print(uuid.uuid4())')"'",
                     "schema_id": "eda-v1", "source_creation_date_time": "2026-01-01T00:00:00Z",
                     "acquisition_rate": {"value": 4.0, "unit": "Hz", "number_of_times": 1}},
          "body": {"signal_data": [1.1, 1.2, 1.3, 1.4], "unit": "uS",
                   "effective_time_frame": {"time_interval": {
                     "start_date_time": "2026-01-01T00:00:00Z",
                     "end_date_time": "2026-01-01T00:00:01Z"}}},
          "signal_type": "EDA", "patient_id": "test-subject", "device_id": "test-device",
          "project_id": "medwear_demo"}'
   ```

   The answer contains `"status": "ingested"`. Batch formats, streams with
   per-session metadata, file uploads and queries are described in
   [docs/API.md](docs/API.md).

## Everyday operation

```bash
docker compose ps                   # status of all services
docker compose logs -f med-api      # API log
docker compose restart med-api      # restart one service
docker compose down                 # stop; the data stays in the Docker volumes
docker compose up -d                # start again
docker compose up -d --build        # after updating the code (git pull)
```

`docker compose down -v` deletes all data, including the Keycloak accounts.

**Backups.** `scripts/backup.sh <directory>` saves all project databases,
InfluxDB and the MinIO files into one archive and removes archives older than
30 days. It reads the passwords from the environment:

```bash
set -a; source .env.medwear; set +a
./scripts/backup.sh /path/to/backups
```

**Keycloak accounts across a reset.** Keycloak imports
`docker/medwear-realm.json` (the roles, the `med-api` client and the
`global_admin` user, without passwords) only when its database is empty.
Before deleting the volumes, export the realm with your accounts to a file
outside the repository with `scripts/export_realm.sh <file>`; that file
contains password hashes and the client secret.

## Running on a server

- Give `setup.sh` the server's hostname and IP address. They go into the
  nginx certificate and the API's allowed origins.
- For production, replace the self-signed certificate with one from your
  institution by mounting it over `/etc/nginx/certs/localhost.crt` and
  `/etc/nginx/certs/localhost.key` of the `medwear-nginx` service.
- Users only need ports 80 and 443. The administration ports (databases,
  MinIO, Keycloak, Grafana, pgAdmin, Uptime Kuma) are published on all
  interfaces: block them in the firewall, or publish them on `127.0.0.1`
  only, and reach them through an SSH tunnel, e.g.
  `ssh -L 8180:localhost:8180 -L 3001:localhost:3001 <user>@<host>`.
- Settings specific to your installation (certificates, extra services,
  host paths, resource limits, ports) belong in a
  `docker-compose.override.yml` next to `docker-compose.yml`. Docker Compose
  merges it automatically and git ignores it; see
  [deployments/README.md](deployments/README.md).

## Streaming from ROS 2

[`ros2_to_medwear/`](ros2_to_medwear/) forwards ROS 2 topics from a recording
computer to the API: every `healthcare_msgs` type that carries samples (ECG,
EEG, EMG, EOG, ICG, BCG, PPG, EDA, heart rate, HRV, RR interval, respiratory
rate, stress, blood volume pulse, cardiac output, stroke volume) and IMU data,
with the device metadata of their `…Info` messages. Multi-channel messages
become one stream per channel; high-rate topics are batched. See its
[README](ros2_to_medwear/README.md).

## Profiling sensors before a study

[`sensor_profiling/`](sensor_profiling/) measures how a BLE wearable holds up
under a real site's WiFi (e.g. a clinic's gym) before it is used with
MEDWEAR: link losses and recovery, delivery, channel use, and for sensors
that send their own sample timestamps, completeness, added delay and the
share of samples that miss a deadline. Sensors are described by profiles
(`sensors/<model>/sensor.yaml`, plus an optional decoder for the sensor's
own protocol); the Polar H10 and Corsano 287-2B are worked examples, and
`sensors/_template/` is the starting point for a new one. See its
[README](sensor_profiling/README.md).

## Tests

```bash
cd medwear_med_api
pip install -r requirements.txt
pytest tests/test_unit.py tests/test_property.py      # no services needed
```

The migration and integration tests need PostgreSQL and InfluxDB
(`docker-compose.test.yml`) and the environment variables that the CI
workflow sets (`.github/workflows/ci.yml`).
[MEDWEAR_TEST_GUIDE.md](MEDWEAR_TEST_GUIDE.md) shows how to inspect each
store of a running installation.

## Repository layout

```
setup.sh                 first-time setup
docker-compose.yml       the stack
docker/                  API Dockerfile, nginx, Keycloak realm template, Grafana provisioning
medwear_med_api/         API (FastAPI), migrations, tests, data-parsing helpers
frontend/                web interface
ros2_to_medwear/         ROS 2 bridge
sensor_profiling/        BLE sensor tests under a site's WiFi (sensor profiles in sensors/)
scripts/                 add_user.sh, backup.sh, export_realm.sh
deployments/             adding installation-specific configuration
docs/API.md              API reference
```

## Benchmark

The version benchmarked in *Connected but Late: Sample Age of Commercial BLE
Wearables under Wi-Fi Coexistence for Real-Time Clinical Applications* is the
tag `paper-2026-10`. The benchmark harness is in
[SCAI-Lab/medwear-benchmark](https://github.com/SCAI-Lab/medwear-benchmark).

## Acknowledgements

This work was partially funded by the Open Research Data program of the ETH
Board, and Schweizer Paraplegiker Stiftung (2021-HS-348) within the Digital
Transformation in Personalized Healthcare in SCI.

## License

See [LICENSE](LICENSE).
