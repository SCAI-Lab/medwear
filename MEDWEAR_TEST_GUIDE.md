# MEDWEAR System Test Guide

## Credentials

| Service | User / Key | Password / Token |
|---|---|---|
| PostgreSQL | `medwear` | see `.credentials.txt` |
| InfluxDB | token | see `.credentials.txt` |
| MinIO (root) | `minioadmin` | see `.credentials.txt` |
| MinIO (API user) | `medwear-api` | see `.credentials.txt` |
| Keycloak admin | `admin` | see `.credentials.txt` |
| Keycloak users | `global_admin` (password set by `setup.sh`) and users added in the Admin tab or via the API | see `.credentials.txt` / set when the user is created |

> InfluxDB org: `medwear` · bucket: `wearables`

---

## Running Containers

| Container | Purpose |
|---|---|
| medwear-api | FastAPI backend (port 8080) |
| medwear-frontend | Web interface (served through medwear-nginx) |
| medwear-nginx | HTTPS entry point (ports 80, 443) |
| medwear-postgres | PostgreSQL — project & processed data |
| medwear-influxdb | InfluxDB — time-series sensor data |
| medwear-minio | Object storage — raw files |
| medwear-keycloak | Authentication (port 8180) |
| medwear-grafana | Dashboards (port 3001) |
| medwear-pgadmin | PostgreSQL UI (port 5050) |

---

## System Start

```bash
cd <project-root>
docker compose up -d
```

---

## PostgreSQL

**Connection:**
- Host: `localhost:5433` (inside the stack: `medwear-postgres:5432`)
- User: `medwear`
- Password: see `.credentials.txt`

**Databases:**

| Database | Purpose |
|---|---|
| `medwear_processed` | API bookkeeping (deleted projects, users) |
| `medwear_<project>` | one database per project, e.g. `medwear_demo` |

**Tables per project database:**
- `measurement_batch` — uploaded sensor batches
- `user_project` — user-project role assignments
- `audit_log` — action history
- `project_settings` — data retention config
- `deleted_files` — soft-deleted file records
- `user_totp` — 2FA secrets

```bash
# Connect via docker
docker exec -it medwear-postgres psql -U medwear -d medwear_demo

# Check uploaded batches
docker exec medwear-postgres psql -U medwear -d medwear_demo \
  -c "SELECT COUNT(*) FROM measurement_batch;"
```

---

## InfluxDB

**Connection:**
- URL: `http://localhost:8086`
- Org: `medwear`
- Bucket: `wearables`
- Token: see `.credentials.txt`

All projects share the single `wearables` bucket. Data is filtered by `project_id` tag.

```bash
# Query recent data for a project
docker exec medwear-influxdb influx query \
  'from(bucket:"wearables") |> range(start:-1h) |> filter(fn:(r) => r.project_id == "medwear_demo")'
```

---

## MinIO (Object Storage)

**Console:** `http://localhost:9001`

**Buckets:**
- `medwear-bronze` — raw uploaded files
- `medwear-silver` — processed files
- `medwear-gold` — export-ready files
- `medwear-lakehouse` — long-term storage

```bash
# List files in bronze bucket
docker exec medwear-minio mc ls myminio/medwear-bronze --recursive
```

---

## Keycloak (Auth)

**Admin console:** `http://localhost:8180`
- Admin user: `admin` / see `.credentials.txt`
- Realm: `medwear`

**Roles:** `global_admin`, `project_admin`, `researcher`, `collaborator`, `device`

---

## API

**Base URL:** `https://localhost/v1`

**Batch Upload (single):**
```bash
curl -k -X POST https://localhost/v1/data/batch \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "header": {
      "uuid": "123e4567-e89b-12d3-a456-426614174000",
      "schema_id": "v1",
      "source_creation_date_time": "2025-03-11T10:00:00Z"
    },
    "body": {
      "ecg_lead_data": [0.1, 0.2, 0.3],
      "signal_quality": [1.0, 1.0, 1.0],
      "unit": "mV",
      "effective_time_frame": {
        "time_interval": {
          "start_date_time": "2025-03-11T10:00:00Z",
          "end_date_time": "2025-03-11T10:00:01Z"
        }
      }
    },
    "signal_type": "ECG",
    "patient_id": 1,
    "device_id": "polar-h10-test"
  }'
```

**Note:** Sample count coherence check is skipped for uploads with fewer than 50 samples (useful for quick tests).

**Supported signal types:** `ECG`, `BCG`, `PPG`, `IMU`, `EEG`, `EDA`, `HR`, `HRV`

---

## Logs

```bash
# API logs
docker compose logs med-api --tail=50

# All logs
docker compose logs --tail=20
```
