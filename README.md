# MEDWEAR Minimal - MED-API

Single-server deployment with MinIO, InfluxDB, PostgreSQL, Keycloak, MED-API, Grafana, pgAdmin.

## Quick Setup
```bash
./setup.sh
```

This will automatically:
- Generate **random secure credentials** for all services (PostgreSQL, MinIO, Grafana, Keycloak, InfluxDB)
- Create `.env` and `.env.medwear` files
- Start all Docker services
- Initialize InfluxDB and PostgreSQL tables
- Save all generated credentials to `.credentials.txt`

> **First run:** Credentials are auto-generated and saved to `.credentials.txt`. Keep this file secure — it is excluded from version control.
> **Subsequent runs:** If `.env.medwear` already exists, setup skips credential generation and reuses existing passwords.

### Viewing your credentials

After setup, all generated passwords are stored in `.credentials.txt` in the project root:

```bash
cat .credentials.txt
```

Example output:
```
PostgreSQL     : medwear / xK7mP2qR9nL4wY8j
MinIO root     : minioadmin / hT3vB6cN1mX5pQ2r
MinIO API key  : medwear-api / dF8yA4kW7nJ2mR6s...
Keycloak admin : admin / hJ9nQ3vB6cN1mX5p
InfluxDB       : admin / wY8jxK7mP2qR9nL4
Grafana        : admin / T3vB6cN1mX5pQ2rd
pgAdmin        : admin@medwear.local / F8yA4kW7nJ2mR6s
```

> Do not commit `.credentials.txt` to version control. It is already listed in `.gitignore`.

---

## Start

```bash
# First time or after code changes
docker compose -f docker-compose.medwear-minimal.yml build
docker compose -f docker-compose.medwear-minimal.yml up -d

# Subsequent runs (no code changes)
docker compose -f docker-compose.medwear-minimal.yml up -d
```

> After starting, Keycloak takes ~30 seconds to be ready.

### Restart a single service (e.g. after API code change)
```bash
docker compose -f docker-compose.medwear-minimal.yml restart med-api
```

---

## Stop

```bash
docker compose -f docker-compose.medwear-minimal.yml down
```

---

## Access

| Service | URL | Credentials |
|---------|-----|-------------|
| MED-API | https://localhost/v1/health (or https://<server>/v1/health) | Token required |
| API Docs | https://localhost/docs (or https://<server>/docs) | — |
| Frontend | https://localhost (or https://<server>) | Keycloak login |
| Keycloak | http://localhost:8180/admin | admin / see `.credentials.txt` |
| pgAdmin | http://localhost:5050 | admin@medwear.com / see `.credentials.txt` |
| InfluxDB | http://localhost:8086 | admin / see `.credentials.txt` |
| MinIO | http://localhost:9001 | minioadmin / see `.credentials.txt` |
| Grafana | http://localhost:3001 | admin / see `.credentials.txt` |

> **Production server:** The MedWear platform is deployed at `https://<server>`. Frontend and API are accessible directly without SSH tunnel. Admin services (Keycloak, pgAdmin, MinIO, Grafana) require SSH tunnel or direct server access.

> **HTTPS:** The frontend and API are served at `https://localhost` with a self-signed certificate, so the browser shows a warning; choose Advanced → Proceed to localhost.

### Remote access via SSH tunnel
```bash
ssh -L 443:localhost:443 \
    -L 80:localhost:80 \
    -L 8180:localhost:8180 \
    -L 9001:localhost:9001 \
    -L 8086:localhost:8086 \
    -L 3001:localhost:3001 \
    -L 5050:localhost:5050 \
    user@your-server.com
```

---

## All System Credentials

Credentials are **auto-generated** on first run of `setup.sh` and saved to `.credentials.txt`.

```bash
cat .credentials.txt
```

| Service | Username / Key | Password |
|---------|---------------|----------|
| **Keycloak** admin console | `admin` | see `.credentials.txt` |
| **Keycloak** client | `med-api` (client_id) | see `.env.medwear` (auto-generated, server-side only) |
| **PostgreSQL** | `medwear` | see `.credentials.txt` |
| **MinIO** root (console) | `minioadmin` | see `.credentials.txt` |
| **MinIO** service account (API) | `medwear-api` | see `.credentials.txt` |
| **InfluxDB** UI login | `admin` | see `.credentials.txt` |
| **InfluxDB** API token | — | see `.credentials.txt` |
| **Grafana** | `admin` | see `.credentials.txt` |
| **pgAdmin** | `admin@medwear.com` | see `.credentials.txt` |

> **Never commit `.credentials.txt`, `.env`, or `.env.medwear` to version control.** These files are already in `.gitignore`.
>
> **For new deployments:** Delete any existing `.env.medwear` before running `setup.sh` so fresh random credentials are generated.

---

## Authentication

All endpoints require a Keycloak JWT token.

### Get a token (via API proxy — recommended)
The client secret never leaves the server:
```bash
curl -k -X POST https://localhost/v1/data/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"<user>","password":"<pass>"}'
```

### Get a token (direct Keycloak — for internal/testing use only)
```bash
curl -X POST http://localhost:8180/realms/medwear/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password&client_id=med-api&client_secret=<secret>&username=<user>&password=<pass>"
```

Client secret is stored server-side in `.env.medwear` — do not embed in frontend code.

### Use the token
```bash
curl -k -H "Authorization: Bearer <access_token>" https://localhost/v1/me
```

### Health check
```bash
curl -k https://localhost/v1/health
```

> `-k` flag accepts the self-signed certificate. Always include it when using `https://localhost` or `https://<server>`.

---

## Step-by-Step: Common API Operations

> **Where to run these commands:**
> - **From the server itself** (SSH into `<server>`): use `https://localhost`
> - **From your laptop** (external): use `https://<server>`
> - Always add `-k` to accept the self-signed certificate.

### Step 1 — Get a token

```bash
# From the server:
TOKEN=$(curl -s -k -X POST https://localhost/v1/data/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"<your-username>","password":"<your-password>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# From your laptop (external):
TOKEN=$(curl -s -k -X POST https://<server>/v1/data/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"<your-username>","password":"<your-password>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

Verify it worked:
```bash
echo $TOKEN
```
You should see a long JWT string. If empty or "null", check your username and password.

---

### Step 2 — GET: Query batches

```bash
# List recent batches (all signal types)
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://<server>/v1/data?project_id=medwear_project1&limit=10" | python3 -m json.tool

# Filter by signal type
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://<server>/v1/data?project_id=medwear_project1&signal_type=ECG&limit=5" | python3 -m json.tool

# Get a single batch by UUID
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://<server>/v1/data/<uuid>?project_id=medwear_project1" | python3 -m json.tool

# Filter by patient ID (use patient_ids — plural, comma-separated for multiple)
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://<server>/v1/data?project_id=medwear_project1&patient_ids=100&limit=10000" | python3 -m json.tool

# Filter by multiple patients
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://<server>/v1/data?project_id=medwear_project1&patient_ids=100,200,300&limit=10000" | python3 -m json.tool

# NOTE: ?patient_id= (singular) is silently ignored — always use ?patient_ids= (plural)

# Get all batches for a patient (legacy endpoint)
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://<server>/v1/data/patients/<patient_id>?project_id=medwear_project1" | python3 -m json.tool

# List available signal types in a project
curl -s -k -H "Authorization: Bearer $TOKEN" \
  "https://<server>/v1/data/signal-types?project_id=medwear_project1" | python3 -m json.tool
```

**Required role:** `collaborator` or higher

---

### Step 3 — POST: Upload a batch

**EDA example:**
```bash
curl -s -k -X POST "https://<server>/v1/data/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "header": {
      "uuid": "550e8400-e29b-41d4-a716-446655440000",
      "schema_id": "eda-v1",
      "source_creation_date_time": "2026-01-01T00:00:00Z",
      "acquisition_rate": {"value": 4.0, "unit": "Hz", "number_of_times": 1}
    },
    "body": {
      "signal_data": [1.1, 1.2, 1.3, 1.4],
      "unit": "uS",
      "effective_time_frame": {"time_interval": {
        "start_date_time": "2026-01-01T00:00:00Z",
        "end_date_time": "2026-01-01T00:00:01Z"
      }}
    },
    "signal_type": "EDA",
    "patient_id": "123456",
    "device_id": "corsano-cw",
    "project_id": "medwear_project1"
  }' | python3 -m json.tool
```

**ECG example** (requires `ecg_lead_data` and `signal_quality`):
```bash
curl -s -k -X POST "https://<server>/v1/data/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "header": {
      "uuid": "660e8400-e29b-41d4-a716-446655440001",
      "schema_id": "ecg-v1",
      "source_creation_date_time": "2026-01-01T00:00:00Z",
      "acquisition_rate": {"value": 128.0, "unit": "Hz", "number_of_times": 1}
    },
    "body": {
      "ecg_lead_data": [0.12, 0.15, 0.11],
      "signal_quality": [1.0, 1.0, 1.0],
      "unit": "mV",
      "effective_time_frame": {"time_interval": {
        "start_date_time": "2026-01-01T00:00:00Z",
        "end_date_time": "2026-01-01T00:00:01Z"
      }}
    },
    "signal_type": "ECG",
    "patient_id": "123456",
    "device_id": "corsano-cw",
    "project_id": "medwear_project1"
  }' | python3 -m json.tool
```

**Required role:** `researcher` or higher

> **Note:** `uuid` must be a valid UUID v4. Generate one with `python3 -c "import uuid; print(uuid.uuid4())"`.

**Streams: send metadata once per session.** A device that uploads many
batches of the same stream registers its metadata once and then sends only
the samples:

```bash
# once per stream (session_id optional; a client-chosen UUID makes retries safe)
curl -s -k -X POST "https://<server>/v1/data/session" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"session_id": "770e8400-e29b-41d4-a716-446655440002",
       "patient_id": "123456", "device_id": "corsano-cw", "signal_type": "EDA",
       "unit": "uS", "project_id": "medwear_<project>",
       "header": {"schema_id": "eda-v1", "acquisition_rate": {"value": 4.0, "unit": "Hz"}}}'

# every batch
curl -s -k -X POST "https://<server>/v1/data/batch" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"session_id": "770e8400-e29b-41d4-a716-446655440002",
       "project_id": "medwear_<project>", "idempotency_key": "<unique per batch>",
       "body": {"signal_data": [1.1, 1.2, 1.3, 1.4],
                "effective_time_frame": {"time_interval": {
                  "start_date_time": "2026-01-01T00:00:00Z",
                  "end_date_time": "2026-01-01T00:00:01Z"}}}}'
```

Session batches are validated exactly like full batches. Registering the same
`session_id` again with the same metadata returns `"status": "existing"`;
different metadata returns 409 `SESSION_CONFLICT`. A batch with an unknown
`session_id` returns 404 `UNKNOWN_SESSION`. Full batches keep working.

**Retries and partial writes.** A batch is acknowledged (`"status": "ingested"`)
only after both PostgreSQL and InfluxDB hold it. If InfluxDB fails, the API
answers 500 `PARTIAL_WRITE_FAILURE` and keeps the batch as *pending*: it is not
listed, exported or counted. Resubmitting the same batch (same idempotency key
and samples) resumes the write and answers `ingested`; a batch that is already
complete answers `duplicate`. The same key with different samples while
pending answers 409 `IDEMPOTENCY_CONFLICT`.

---

### Step 4 — DELETE: Soft delete a batch

```bash
curl -s -k -X DELETE \
  "https://<server>/v1/data/<uuid>?project_id=medwear_project1" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Deleted batches are permanently purged after 24 hours. To restore before that:
```bash
curl -s -k -X POST \
  "https://<server>/v1/data/<uuid>/restore?project_id=medwear_project1" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Required role:** `project_admin` or higher

---

## User Management

### Add a user (global_admin — can specify any project)

```bash
# Get admin token first
ADMIN_TOKEN=$(curl -s -k -X POST https://<server>/v1/data/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"<admin-username>","password":"<admin-password>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Create user and assign to project
curl -s -k -X POST "https://<server>/v1/data/admin/users" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "newuser",
    "password": "SecurePassword123!",
    "role": "researcher",
    "project_id": "medwear_project1"
  }' | python3 -m json.tool
```

Available roles: `device`, `collaborator`, `researcher`, `project_admin`, `global_admin`

### Add a user (project_admin — can only add to their own project)

Same command as above — project_admin can only set `role` to `collaborator`, `researcher`, or `device`. The `project_id` must match their own project.

### List users in a project

```bash
curl -s -k -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://<server>/v1/data/admin/users?project_id=medwear_project1" | python3 -m json.tool
```

### Remove a user from a project

```bash
curl -s -k -X DELETE \
  "https://<server>/v1/data/admin/user-project?username=<username>&project_id=medwear_project1" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -m json.tool
```

---

## Roles

| Role | Access |
|------|--------|
| `device` | Ingest data only |
| `collaborator` | Read data from assigned projects |
| `researcher` | Read data, upload data, download files |
| `project_admin` | Manage users within their project |
| `global_admin` | Full access: all projects, user/project management |

---

## Web Interface

Available at `https://localhost`. Login with Keycloak credentials.

| Tab | Access | Description |
|-----|--------|-------------|
| Dashboard | All roles | Batch statistics, recent uploads |
| Upload | collaborator+ | JSON, CSV, raw file upload (.cwa / .json / .csv) |
| Query Data | collaborator+ | Filter and search batches, view/download signals |
| File Manager | collaborator+ | MinIO files: list, download, soft-delete, restore |
| Activities | project_admin, global_admin | Audit log — All Activities + File Activities tabs |
| Admin | project_admin, global_admin | User and project management |
| Health Check | All roles | Real-time service status |

---

## Multi-Project Architecture

Each project has its own PostgreSQL database (`medwear_project1`, `medwear_project2`, etc.).

- Tables per project: `measurement_batch`, `audit_log`, `user_project`, `deleted_files`
- Central postgres DB tracks soft-deleted projects in `deleted_projects`
- `global_admin` can create and delete projects via the Admin tab or API
- Grafana datasources are auto-provisioned for each project on startup and sync hourly

---

## Soft Delete & Purge

All deletes are soft — data is marked deleted and permanently removed after 24 hours.

| Resource | Soft delete | Hard delete |
|----------|-------------|-------------|
| Batch (metadata + signals) | `is_deleted=true` in PostgreSQL | Purge removes PostgreSQL row + InfluxDB data |
| File | Entry in `deleted_files` table | Purge removes from MinIO + table |
| Project | Entry in `deleted_projects` table | Purge drops database |

The purge loop runs every hour (first run 60 seconds after startup).

Deleted items can be restored before the 24h window via the frontend (Show Deleted → Restore) or API.

---

## Error Responses

All API errors return a consistent JSON format:

```json
{
  "detail": {
    "error": "UPPER_SNAKE_CODE",
    "detail": "Human-readable message",
    "timestamp": "2026-03-31T20:00:00Z"
  }
}
```

| HTTP | Error Code | Meaning |
|------|-----------|---------|
| 400 | `INVALID_JSON` | Malformed JSON body |
| 400 | `SCHEMA_VALIDATION_FAILURE` | Missing or invalid fields |
| 400 | `CHECKSUM_MISMATCH` | SHA-256 checksum did not match |
| 400 | `CSV_PARSE_ERROR` | CSV could not be parsed |
| 400 | `UNSUPPORTED_FILE_TYPE` | File extension not allowed |
| 401 | `UNAUTHORIZED` | Missing or invalid token |
| 403 | `FORBIDDEN` | Insufficient role |
| 404 | `NOT_FOUND` | Resource does not exist |
| 409 | `DUPLICATE_BATCH` | Idempotency key already used |
| 409 | `DUPLICATE_FILE` | File already exists in MinIO |
| 413 | `PAYLOAD_TOO_LARGE` | Batch exceeds 10 MB limit |
| 422 | `INVALID_TIMESTAMP_ORDER` | end_date_time ≤ start_date_time |
| 422 | `MISSING_SAMPLING_RATE` | sampling_rate = 0 and cannot be inferred |
| 422 | `MISSING_SIGNAL_QUALITY` | ECG signal_quality array missing |
| 429 | `RATE_LIMIT_EXCEEDED` | 100 requests/minute per token exceeded |
| 500 | `STORAGE_ERROR` | MinIO / InfluxDB write failure |
| 500 | `INTERNAL_SERVER_ERROR` | Unexpected server error |

---

## Rate Limiting

Per-token limits (identified by Keycloak username). Uses `SlowAPIMiddleware` applied globally.

| Endpoint | Limit |
|----------|-------|
| `POST /v1/data/batch` | 50 / minute |
| `POST /v1/data/upload` | 20 / minute |
| All other endpoints | 100 / minute |

Exceeded requests return HTTP `429` with:
```json
{
  "detail": {
    "error": "RATE_LIMIT_EXCEEDED",
    "detail": "Rate limit exceeded: 100 per 1 minute",
    "timestamp": "2026-03-31T20:00:00Z"
  }
}
```

---

## MinIO Security

All buckets (`medwear-bronze`, `medwear-lakehouse`, `medwear-silver`, `medwear-gold`) are set to **private** — anonymous S3 access is disabled on startup.

The API uses a dedicated **service account** (`medwear-api`) instead of the MinIO root user. The service account has only the required permissions: `GetObject`, `PutObject`, `DeleteObject`, `ListBucket`.

Credentials are configured via `.env.medwear`:

| Variable | Purpose |
|----------|---------|
| `MINIO_ROOT_USER` | MinIO admin (console only) |
| `MINIO_ROOT_PASSWORD` | MinIO admin password — change before deploying |
| `MINIO_API_ACCESS_KEY` | Service account used by the API |
| `MINIO_API_SECRET_KEY` | Service account password — change before deploying |

> On first `docker compose up`, `minio-setup` container automatically creates the buckets, disables anonymous access, creates the service account, and attaches the policy.

---

## Data Ingestion

### POST /v1/data/batch — Structured sensor data

Stores metadata in PostgreSQL and signals in InfluxDB. Supports JSON and CSV.

**Supported signal types:** ECG, BCG, PPG, IMU, EEG, EDA, HR, HRV

```bash
curl -k -X POST https://localhost/v1/data/batch \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "batches": [{
      "header": {
        "uuid": "your-uuid",
        "schema_id": "ecg-lead-1.x",
        "source_creation_date_time": "2026-03-12T10:00:00Z",
        "acquisition_rate": {"value": 250.0, "unit": "Hz", "number_of_times": 1}
      },
      "body": {
        "ecg_lead_data": [0.12, 0.15, 0.11],
        "unit": "mV",
        "effective_time_frame": {"time_interval": {
          "start_date_time": "2026-03-12T10:00:00Z",
          "end_date_time": "2026-03-12T10:00:03Z"
        }}
      },
      "signal_type": "ECG",
      "patient_id": "123456",
      "device_id": "polar-h10-01",
      "idempotency_key": "polar-h10-01-20260312-001"
    }]
  }'
```

### POST /v1/data/upload — Raw file upload

Stores raw files (`.cwa`, `.json`, `.csv`) in MinIO under `{project_id}/{username}/{filename}`. Max 1 GB.

```bash
curl -k -X POST https://localhost/v1/data/upload \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: ecg_data.csv" \
  --data-binary @ecg_data.csv
```

`global_admin` can specify a target project via query parameter:
```bash
curl -k -X POST "https://localhost/v1/data/upload?project_id=medwear_project2" \
  -H "Authorization: Bearer <token>" \
  -H "X-Filename: sensor.csv" \
  --data-binary @sensor.csv
```

---

## Query Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/data` | List/filter batches |
| GET | `/v1/data/{uuid}` | Single batch (metadata + signals) |
| GET | `/v1/data/{uuid}/metadata` | Metadata only |
| GET | `/v1/data/{uuid}/signals` | Signals only |
| GET | `/v1/data/patients/{patient_id}` | All batches for a patient |
| DELETE | `/v1/data/{uuid}` | Soft delete batch |
| POST | `/v1/data/{uuid}/restore` | Restore soft-deleted batch |
| GET | `/v1/data/files` | List MinIO files |
| GET | `/v1/data/files/{path}` | Download file |
| DELETE | `/v1/data/files/{path}` | Soft delete file |
| POST | `/v1/data/files/{path}/restore` | Restore deleted file |

Query parameters for `/v1/data`: `patient_ids`, `signal_type`, `start`, `end`, `device_id`, `project_id`, `limit`, `offset`, `metadata_only`, `show_deleted`, `marked_for_deletion`, `format` (json/csv), `downsample`

---

## Admin Endpoints

| Method | Endpoint | Role required | Description |
|--------|----------|---------------|-------------|
| POST | `/v1/data/admin/users` | project_admin+ | Create user in Keycloak and assign to project |
| GET | `/v1/data/admin/users` | project_admin+ | List users |
| DELETE | `/v1/data/admin/user-project` | project_admin+ | Remove user from project |
| POST | `/v1/data/admin/projects` | global_admin | Create new project |
| GET | `/v1/data/admin/projects` | project_admin+ | List projects |
| DELETE | `/v1/data/admin/projects/{name}` | global_admin | Soft delete project |
| POST | `/v1/data/admin/projects/{name}/restore` | global_admin | Restore deleted project |
| GET | `/v1/data/admin/activities` | project_admin+ | Audit log (filterable by username, project) |
| POST | `/v1/data/auth/change-password` | any | Change temporary password |

---

## PostgreSQL — Connect via pgAdmin

Open `http://localhost:5050`, add a new server:

```
General → Name: medwear
Connection:
  Host:     medwear-postgres
  Port:     5432
  Database: medwear_project1
  Username: medwear
  Password: see .credentials.txt
```

Each project is a separate database. Switch databases to query different projects.

---

## Running Tests

```bash
cd medwear_med_api
pip install -r requirements.txt pytest
pytest tests/test_unit.py tests/test_property.py      # no services needed
pytest tests/test_migrations.py tests/test_integration.py   # needs PostgreSQL and InfluxDB (see docker-compose.test.yml)
```

---

## Two-Factor Authentication (2FA)

Optional TOTP-based 2FA using any authenticator app (e.g. Google Authenticator).

- Users enable/disable 2FA from the sidebar in the frontend
- On enable: a QR code is shown — scan with the authenticator app, confirm with a 6-digit code
- On next login: a TOTP code is required before the app loads
- Disabling 2FA also requires a valid TOTP code

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/auth/2fa/status` | Check if 2FA is enabled |
| POST | `/v1/auth/2fa/setup` | Generate secret and QR code |
| POST | `/v1/auth/2fa/enable` | Verify code and activate 2FA |
| POST | `/v1/auth/2fa/disable` | Verify code and deactivate 2FA |
| POST | `/v1/auth/2fa/verify` | Verify a TOTP code after login |

---

## Keycloak Realm

The file `docker/medwear-realm.json` is auto-imported on first startup (`--import-realm`). It includes:

- All 5 roles pre-defined on `med-api` client: `collaborator`, `researcher`, `project_admin`, `global_admin`, `device`
- Default users: one `global_admin` user without a password; set it in the Keycloak admin console (Users → global_admin → Credentials) after the first start
- TOTP policy configured (30s, 6 digits, SHA1 — compatible with Google Authenticator)
- `redirectUris` and `webOrigins` of the `med-api` client cover localhost only; add `https://<your-server>/*` and `https://<your-server>` for a remote deployment
- The `med-api` client secret is the placeholder `${KEYCLOAK_CLIENT_SECRET}`, filled on import from the value `setup.sh` generates; the realm signing keys are generated by Keycloak on first start

> **Adding users:** Use the Admin tab in the frontend or `POST /v1/data/admin/users`. Users added via the API are created in Keycloak automatically.
> **Before wiping volumes:** Run `./scripts/export_realm.sh` to save all current Keycloak users to `docker/medwear-realm.json`.

---

## Security Notes

- **SQL injection** prevention: all query parameters are passed as bound parameters (no string interpolation in SQL)
- **Audit log**: all non-GET requests are logged per project; `global_admin` actions are logged to the target project
- **File Activities**: the Activities tab has a dedicated "File Activities" view showing uploads, deletes, and restores with filenames

---

## Code Structure

```
medwear_med_api/
  medwear_med_api/
    routers/
      utils.py       — shared helpers (find_batch, stream_csv, minio client)
      batches.py     — batch query/delete/restore endpoints
      files.py       — MinIO file endpoints
      patients.py    — patient timeline endpoint
      admin.py       — user/project/activity management
      totp.py        — 2FA (TOTP) setup and verification endpoints
    query_router.py  — aggregates all routers (imported by run_med_api.py)
    batch_router.py  — data ingestion endpoints (POST /batch, POST /upload)
    auth.py          — Keycloak JWT validation, role dependencies
    audit.py         — request audit logging middleware
    db.py            — per-project session factory, project management
    models.py        — SQLModel table definitions
    grafana_utils.py — Grafana datasource auto-provisioning
    influx_reader.py — InfluxDB signal queries
    influx_writer.py — InfluxDB signal writes
  scripts/
    run_med_api.py          — FastAPI app, startup tasks, purge loop
    create_tables.py        — DB migration script
```

---

## ROS 2 Real-Time Bridge

Streams live biosignal data from ROS 2 topics directly into the MedWear API.

**Location:** [`ros2_to_medwear/`](ros2_to_medwear/)

### Important: topic names are project-specific

Different projects use different ROS 2 topic names. The bridge reads topic names from [`ros2_to_medwear/config.yaml`](ros2_to_medwear/config.yaml) — **you must edit this file before running the bridge.**

The message types are standardized across all projects:

| Signal | Message type | Unit |
|--------|-------------|------|
| EEG | `healthcare_msgs/msg/EEG` | uV |
| EDA | `healthcare_msgs/msg/EDA` | uS |
| HR | `healthcare_msgs/msg/HeartRate` | bpm |
| HRV | `healthcare_msgs/msg/HeartRateVariability` | ms |

Message definitions: [SCAI-Lab/healthcare_msgs](https://github.com/SCAI-Lab/healthcare_msgs)

### Step 1 — Discover your topic names

Run these commands on the recording computer **while the sensors are publishing:**

```bash
# List all active topics
ros2 topic list

# Confirm the actual sampling rate of a topic
ros2 topic hz /your_eeg_topic
```

### Step 2 — Edit config.yaml

Open [`ros2_to_medwear/config.yaml`](ros2_to_medwear/config.yaml) and replace each `FILL_IN_YOUR_*_TOPIC` placeholder with the actual topic name, and update `sampling_rate_hz` to match the measured rate.

Each project has its own config — copy and rename the file if you run multiple projects on the same machine.

### Step 3 — Run on the recording computer (Docker)

```bash
# Build once
docker build -t medwear-ros2-bridge ./ros2_to_medwear

# Run — token is fetched and refreshed automatically
docker run --rm \
    --network host \
    -e MEDWEAR_USERNAME=<keycloak_user> \
    -e MEDWEAR_PASSWORD=<keycloak_password> \
    -e API_BASE_URL=https://<server-ip>/v1 \
    medwear-ros2-bridge \
    --config config.yaml \
    --device corsano-001 \
    --patient <patient_id> \
    --project <project_id>
```

`--network host` is required so the container can see ROS 2 DDS multicast traffic.

### Run locally (ROS 2 Jazzy installed)

```bash
source /opt/ros/jazzy/setup.bash
source /ros2_ws/install/setup.bash  # after building healthcare_msgs

cd ros2_to_medwear
export MEDWEAR_USERNAME=<user>
export MEDWEAR_PASSWORD=<pass>
export API_BASE_URL=https://<server>/v1
python3 medwear_subscriber.py \
    --config config.yaml \
    --device corsano-001 \
    --patient sub-001 \
    --project <project_id>
```

### How it works

Each ROS 2 message → one batch POST to `POST /v1/data/batch`. The batch contains:
- All samples from that message (e.g. one EEG frame = 256 samples at 256 Hz)
- ROS timestamp as `start_date_time` / `end_date_time`
- SHA-256 idempotency key (prevents duplicates on reconnect)

Topic → signal type mapping is configured in [`ros2_to_medwear/config.yaml`](ros2_to_medwear/config.yaml).

---

## GDPR / Privacy

MedWear implements GDPR compliance via `/v1/data/gdpr/*` endpoints. All write operations require `project_admin` or `global_admin` role.

| Article | Endpoint | Description |
|---------|----------|-------------|
| Art. 7 | `POST /v1/data/gdpr/consent/{patient_id}` | Grant consent |
| Art. 7 §3 | `DELETE /v1/data/gdpr/consent/{patient_id}/{consent_type}` | Revoke consent |
| Art. 7 | `GET /v1/data/gdpr/consent/{patient_id}` | List all consents for a patient |
| Art. 17 | `DELETE /v1/data/gdpr/erasure/{patient_id}` | Right to erasure — cascades through PostgreSQL → InfluxDB → MinIO |
| Art. 20 | `GET /v1/data/gdpr/export/{patient_id}` | Data portability — exports all patient data as a JSON archive |

Consent types: `data_collection`, `data_sharing`, `research_use`, `third_party_transfer`

```bash
# Grant consent
curl -k -X POST https://<server>/v1/data/gdpr/consent/1 \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"consent_type": "data_collection", "consent_version": "1.0"}'

# Right to erasure (irreversible — deletes ALL data for this patient)
curl -k -X DELETE "https://<server>/v1/data/gdpr/erasure/1?project_id=medwear_project1" \
  -H "Authorization: Bearer <token>"

# Export patient data (Art. 20)
curl -k -H "Authorization: Bearer <token>" \
  "https://<server>/v1/data/gdpr/export/1" --output patient_1_gdpr.json
```

---

## Testing

Full system test guide (credentials, container list, database structure, API examples, InfluxDB queries):

**[→ MEDWEAR_TEST_GUIDE.md](MEDWEAR_TEST_GUIDE.md)**

Quick smoke test:

```bash
# 1. Get a token
TOKEN=$(curl -sk -X POST https://localhost/v1/data/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"<your-username>","password":"<your-password>"}' | jq -r .access_token)

# 2. Check health
curl -sk https://localhost/v1/health | jq .

# 3. Query recent batches
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://localhost/v1/data?limit=5" | jq .
```

---

## Backup

Backs up PostgreSQL (all `medwear_*` databases), InfluxDB, and MinIO raw files to a compressed archive.

```bash
# Run manually
./scripts/backup.sh /opt/medwear_backups

# Add to crontab (daily at 2 AM)
crontab -e
# Add this line:
0 2 * * * /path/to/med-api/scripts/backup.sh /opt/medwear_backups >> /var/log/medwear_backup.log 2>&1
```

Backups older than 30 days are deleted automatically. Each backup is saved as `medwear_backup_YYYYMMDD_HHMMSS.tar.gz`.

---

## Project Status

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 0 | Background study, schema exploration | Done |
| Phase 1 | Database architecture (PostgreSQL + InfluxDB) | Done |
| Phase 2 | Authentication (Keycloak, JWT, RBAC, audit logging) | Done |
| Phase 3 | Upload endpoints (batch + raw file) | Done |
| Phase 4 | Query, delete, restore, admin, multi-project, Grafana sync | Done |
| Phase 5 | Error handling & validation: standardized errors, rate limiting, frontend error display, 46/46 tests | Done |
| Phase 5.5 | MinIO security: private buckets, service account, root credential hardening | Done |
| Phase 5.6 | Credential hardening: all passwords strengthened, granular rate limits, Keycloak client_secret backend proxy, security headers | Done |
| Phase 6 | API versioning (`/v1/`), HTTPS/TLS via nginx (self-signed cert, HTTP→HTTPS redirect) | Done |
| Post-Phase 6 | SQL injection fix, File Activities tab, CSV raw upload, global_admin audit logging, filename in activity log | Done |
| Phase 7 | Optional 2FA (TOTP / Google Authenticator) | Done |
| Phase 8 | Research data export (WFDB, EDF+, CSV), bulk patient export, ROS 2 real-time bridge (EEG, EDA, HR, HRV) | Done |


## Acknowledgements
This work was partially funded by the Open Research Data program of the ETH Board, and Schweizer Paraplegiker Stiftung (2021-HS-348) within the Digital Transformation in Personalized Healthcare in SCI.

