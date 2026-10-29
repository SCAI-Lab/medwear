# MEDWEAR API reference

All examples use `https://<host>`, the address of your installation
(`https://localhost` on the machine itself). The self-signed certificate that
nginx generates needs `-k` in curl. Interactive documentation of every
endpoint is at `https://<host>/docs`.

Project identifiers are the names of the project databases, e.g.
`medwear_demo` (see [Projects](#projects)).

## Authentication

Every endpoint except the health check needs a Keycloak access token.

```bash
TOKEN=$(curl -sk -X POST https://<host>/v1/data/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"<user>","password":"<password>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk -H "Authorization: Bearer $TOKEN" https://<host>/v1/auth/2fa/status   # any valid token
curl -sk https://<host>/v1/health                                             # no token needed
```

The API requests the token from Keycloak on the client's behalf, so the
Keycloak client secret stays on the server (`.env.medwear`). Users with
two-factor authentication enabled confirm a TOTP code after login
([2FA](#two-factor-authentication)).

## Roles

| Role | Access |
|---|---|
| `device` | upload data only |
| `collaborator` | read data of the assigned projects |
| `researcher` | read and upload data, download files |
| `project_admin` | manage the users of their project, delete and restore data |
| `global_admin` | everything: all projects, project and user management |

## Projects

Each project has its own PostgreSQL database (tables `measurement_batch`,
`audit_log`, `user_project`, `deleted_files`, ...); a `global_admin` creates
and deletes projects in the Admin tab of the web interface or with the API.
Grafana gets a data source per project at startup and every hour.

| Method | Endpoint | Role | Description |
|---|---|---|---|
| POST | `/v1/data/admin/projects` | global_admin | create a project |
| GET | `/v1/data/admin/projects` | project_admin+ | list projects |
| DELETE | `/v1/data/admin/projects/{name}` | global_admin | delete a project (soft) |
| POST | `/v1/data/admin/projects/{name}/restore` | global_admin | restore a deleted project |

```bash
curl -sk -X POST https://<host>/v1/data/admin/projects \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"project_name":"demo"}'          # creates the project medwear_demo
```

Names contain lowercase letters, digits and underscores; the prefix
`medwear_` is added when it is missing.

## Users

| Method | Endpoint | Role | Description |
|---|---|---|---|
| POST | `/v1/data/admin/users` | project_admin+ | create a user in Keycloak and assign them to a project |
| GET | `/v1/data/admin/users?project_id=...` | project_admin+ | list the users of a project |
| DELETE | `/v1/data/admin/user-project?username=...&project_id=...` | project_admin+ | remove a user from a project |
| GET | `/v1/data/admin/activities` | project_admin+ | audit log (filter by user or project) |
| POST | `/v1/data/auth/change-password` | any | change a temporary password |

```bash
curl -sk -X POST https://<host>/v1/data/admin/users \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"<initial password>","role":"researcher","project_id":"medwear_demo"}'
```

A `project_admin` can create users only in their own project and only with
the roles `collaborator`, `researcher` and `device`.

## Uploading data

### Structured batches: `POST /v1/data/batch`

Metadata goes to PostgreSQL, samples to InfluxDB. Role `researcher` or
`device`.

| Signal type | Samples field in `body` | Header block (optional) |
|---|---|---|
| `ECG` | `ecg_lead_data`, with `signal_quality` per sample | `ecg_info` |
| `BCG` | `bcg_sensor_readings` | `bcg_info` |
| `PPG`, `EEG`, `EDA`, `EMG`, `EOG`, `ICG` | `signal_data` | `ppg_info`, `eeg_info`, `eda_info`, `emg_info`, `eog_info`, `icg_info` |
| `IMU`, `HR`, `HRV`, `RR_INTERVAL`, `RESPIRATORY_RATE`, `STRESS`, `BVP`, `CARDIAC_OUTPUT`, `STROKE_VOLUME` | `signal_data` | – |

These cover every `healthcare_msgs` type that carries samples, so the ROS 2
bridge can forward all of them (see `ros2_to_medwear/README.md`).

```bash
curl -sk -X POST https://<host>/v1/data/batch \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
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
        "end_date_time": "2026-01-01T00:00:01Z"}}
    },
    "signal_type": "EDA",
    "patient_id": "subject-001",
    "device_id": "wristband-01",
    "project_id": "medwear_demo"
  }'
```

- `header.uuid` is a UUID v4 (`python3 -c "import uuid; print(uuid.uuid4())"`).
- Several batches can be sent at once as `{"batches": [ ... ]}`.
- `idempotency_key` (optional) identifies a batch across retries.

### Streams: metadata once per session

A device that sends many batches of one stream registers the metadata once
and then sends only the samples:

```bash
# once per stream; a client-chosen session_id makes retries safe
curl -sk -X POST https://<host>/v1/data/session \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"session_id": "770e8400-e29b-41d4-a716-446655440002",
       "patient_id": "subject-001", "device_id": "wristband-01", "signal_type": "EDA",
       "unit": "uS", "project_id": "medwear_demo",
       "header": {"schema_id": "eda-v1", "acquisition_rate": {"value": 4.0, "unit": "Hz"}}}'

# every batch
curl -sk -X POST https://<host>/v1/data/batch \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"session_id": "770e8400-e29b-41d4-a716-446655440002",
       "project_id": "medwear_demo", "idempotency_key": "<unique per batch>",
       "body": {"signal_data": [1.1, 1.2, 1.3, 1.4],
                "effective_time_frame": {"time_interval": {
                  "start_date_time": "2026-01-01T00:00:00Z",
                  "end_date_time": "2026-01-01T00:00:01Z"}}}}'
```

Session batches are validated like full batches. Registering a `session_id`
again with the same metadata answers `"status": "existing"`, with different
metadata 409 `SESSION_CONFLICT`; a batch with an unknown `session_id` answers
404 `UNKNOWN_SESSION`.

### Acknowledgement and retries

A batch is acknowledged (`"status": "ingested"`) only when both PostgreSQL and
InfluxDB hold it. If the InfluxDB write fails, the API answers 500
`PARTIAL_WRITE_FAILURE` and keeps the batch *pending*: it is not listed,
exported or counted. Sending the same batch again (same idempotency key and
samples) completes it and answers `ingested`; a batch that is already complete
answers `duplicate`. The same key with different samples while the batch is
pending answers 409 `IDEMPOTENCY_CONFLICT`.

### Raw files: `POST /v1/data/upload`

Stores `.cwa`, `.json` and `.csv` files (up to 1 GB) in MinIO under
`{project_id}/{username}/{filename}`.

```bash
curl -sk -X POST https://<host>/v1/data/upload \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/octet-stream" -H "X-Filename: recording.csv" \
  --data-binary @recording.csv
```

A `global_admin` chooses the project with `?project_id=...`.

## Querying data

| Method | Endpoint | Description |
|---|---|---|
| GET | `/v1/data` | list and filter batches |
| GET | `/v1/data/{uuid}` | one batch, metadata and samples |
| GET | `/v1/data/{uuid}/metadata` | metadata only |
| GET | `/v1/data/{uuid}/signals` | samples only |
| GET | `/v1/data/patients/{patient_id}` | all batches of a subject |
| GET | `/v1/data/signal-types?project_id=...` | signal types present in a project |
| GET | `/v1/data/files` | list stored files |
| GET | `/v1/data/files/{path}` | download a file |

Parameters of `/v1/data`: `project_id`, `patient_ids` (comma-separated; the
singular `patient_id` is ignored), `signal_type`, `device_id`, `start`,
`end`, `limit`, `offset`, `metadata_only`, `show_deleted`,
`marked_for_deletion`, `format` (`json` or `csv`), `downsample`.

```bash
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://<host>/v1/data?project_id=medwear_demo&signal_type=EDA&limit=10"
```

Research exports (WFDB, EDF+, CSV) are under `/v1/data/export`; see `/docs`.

## Deleting and restoring

All deletes are soft; the purge loop removes deleted items for good after 24
hours (it runs every hour). Until then they can be restored in the web
interface (Show Deleted → Restore) or with the API. Role `project_admin` or
higher.

| Resource | Delete | Restore | Purge removes |
|---|---|---|---|
| batch | `DELETE /v1/data/{uuid}` | `POST /v1/data/{uuid}/restore` | PostgreSQL row and InfluxDB samples |
| file | `DELETE /v1/data/files/{path}` | `POST /v1/data/files/{path}/restore` | the MinIO object |
| project | `DELETE /v1/data/admin/projects/{name}` | `POST /v1/data/admin/projects/{name}/restore` | the project database |

## Privacy (GDPR)

Write operations need `project_admin` or `global_admin`.

| Article | Endpoint | Description |
|---|---|---|
| Art. 7 | `POST /v1/data/gdpr/consent/{patient_id}` | record consent |
| Art. 7 | `GET /v1/data/gdpr/consent/{patient_id}` | list a subject's consents |
| Art. 7 §3 | `DELETE /v1/data/gdpr/consent/{patient_id}/{consent_type}` | revoke consent |
| Art. 17 | `DELETE /v1/data/gdpr/erasure/{patient_id}?project_id=...` | erase all data of a subject (PostgreSQL, InfluxDB, MinIO); irreversible |
| Art. 20 | `GET /v1/data/gdpr/export/{patient_id}` | export all data of a subject as JSON |

Consent types: `data_collection`, `data_sharing`, `research_use`,
`third_party_transfer`.

## Two-factor authentication

Optional TOTP (any authenticator app). Users switch it on and off in the
sidebar of the web interface; switching it off needs a valid code.

| Method | Endpoint | Description |
|---|---|---|
| GET | `/v1/auth/2fa/status` | is 2FA enabled |
| POST | `/v1/auth/2fa/setup` | new secret and QR code |
| POST | `/v1/auth/2fa/enable` | confirm a code and enable |
| POST | `/v1/auth/2fa/disable` | confirm a code and disable |
| POST | `/v1/auth/2fa/verify` | confirm a code after login |

## Errors

Errors have one format:

```json
{"detail": {"error": "UPPER_SNAKE_CODE", "detail": "message", "timestamp": "2026-01-01T00:00:00Z"}}
```

| HTTP | Code | Meaning |
|---|---|---|
| 400 | `INVALID_JSON` | malformed JSON |
| 400 | `SCHEMA_VALIDATION_FAILURE` | missing or invalid fields (including those detected while the request is parsed) |
| 400 | `CHECKSUM_MISMATCH` | SHA-256 checksum does not match |
| 400 | `UNSUPPORTED_FILE_TYPE` | file extension not accepted |
| 401 | `UNAUTHORIZED` | missing or invalid token |
| 403 | `FORBIDDEN` | role or project does not allow the request |
| 404 | `NOT_FOUND`, `UNKNOWN_SESSION` | resource or session does not exist |
| 409 | `SESSION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `DUPLICATE_FILE` | conflicts, see above |
| 413 | `PAYLOAD_TOO_LARGE` | batch request over 10 MB |
| 422 | `INVALID_TIMESTAMP_ORDER` | end of the time frame not after its start |
| 422 | `MISSING_SAMPLING_RATE` | no sampling rate given and none can be derived |
| 429 | `RATE_LIMIT_EXCEEDED` | rate limit of the endpoint exceeded |
| 500 | `PARTIAL_WRITE_FAILURE` | batch kept pending, send it again |
| 500 | `STORAGE_ERROR`, `INTERNAL_SERVER_ERROR` | storage or unexpected server error |

## Rate limits

Limits apply per user (Keycloak username).

| Endpoint | Limit |
|---|---|
| `POST /v1/data/batch` | 300 per minute (`BATCH_RATE_LIMIT` in `.env.medwear`) |
| `POST /v1/data/session` | 600 per minute (`SESSION_RATE_LIMIT`) |
| `POST /v1/data/upload` | 120 per minute |
| queries, exports, admin and GDPR endpoints | between 5 and 200 per minute, by endpoint |
| everything else | 100 per minute |

All simulated or real devices that share one account share its limit; give
each device its own `device` account.

## Storage and security

- MinIO buckets (`medwear-bronze`, `medwear-silver`, `medwear-gold`,
  `medwear-lakehouse`) are private. The API uses its own MinIO account
  (`MINIO_API_ACCESS_KEY`) with read, write, delete and list rights only;
  the root account is for the console.
- SQL statements take all user input as bound parameters.
- Every request that changes data is written to the audit log of its project;
  `global_admin` actions are logged in the project they act on.

## Code layout

```
medwear_med_api/
  scripts/run_med_api.py     FastAPI application, startup migrations, purge loop
  medwear_med_api/
    batch_router.py          POST /batch, /session, /upload
    query_router.py          collects the routers below
    routers/                 batches, files, patients, admin, export, gdpr, totp
    auth.py                  Keycloak token validation, roles
    db.py                    per-project databases
    influx_writer.py, influx_reader.py
    audit.py, rate_limit.py, errors.py, metrics.py, tracing.py
  alembic/                   schema migrations
  tests/                     unit, property, migration and integration tests
```
