#!/bin/bash
# MedWear backup script
# Backs up PostgreSQL databases, InfluxDB, and MinIO to a local directory.
# Usage: ./backup.sh [backup_dir]
# Cron example (daily at 2am): 0 2 * * * /path/to/backup.sh >> /var/log/medwear_backup.log 2>&1

set -euo pipefail

BACKUP_ROOT="${1:-/opt/medwear_backups}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"
RETENTION_DAYS=30

# Load env if available
ENV_FILE="$(dirname "$0")/../.env.medwear"
if [ -f "$ENV_FILE" ]; then
  export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

PG_USER="${POSTGRES_USER:-medwear}"
PG_PASS="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD not set (see .env.medwear)}"
INFLUX_TOKEN="${INFLUX_TOKEN:?INFLUX_TOKEN not set (see .env.medwear)}"
INFLUX_ORG="${DOCKER_INFLUXDB_INIT_ORG:-medwear}"
MINIO_ALIAS="medwear_backup"

echo "===== MedWear Backup ====="
echo "Timestamp : $TIMESTAMP"
echo "Output    : $BACKUP_DIR"

mkdir -p "$BACKUP_DIR/postgres"
mkdir -p "$BACKUP_DIR/influxdb"
mkdir -p "$BACKUP_DIR/minio"

# ── PostgreSQL ────────────────────────────────────────────────────────────────
echo ""
echo "[1/3] PostgreSQL..."
DATABASES=$(docker exec medwear-postgres psql -U "$PG_USER" -t -c \
  "SELECT datname FROM pg_database WHERE datname LIKE 'medwear%' AND datistemplate=false;" \
  2>/dev/null | tr -d ' ' | grep -v '^$')

for DB in $DATABASES; do
  echo "  Dumping $DB..."
  docker exec medwear-postgres pg_dump -U "$PG_USER" "$DB" \
    | gzip > "$BACKUP_DIR/postgres/${DB}.sql.gz"
  echo "  ✓ $DB"
done

# ── InfluxDB ──────────────────────────────────────────────────────────────────
echo ""
echo "[2/3] InfluxDB..."
docker exec medwear-influxdb influx backup /tmp/influx_backup \
  --token "$INFLUX_TOKEN" --org "$INFLUX_ORG" 2>/dev/null || true
docker cp medwear-influxdb:/tmp/influx_backup "$BACKUP_DIR/influxdb/"
docker exec medwear-influxdb rm -rf /tmp/influx_backup 2>/dev/null || true
echo "  ✓ InfluxDB"

# ── MinIO ─────────────────────────────────────────────────────────────────────
echo ""
echo "[3/3] MinIO..."
MINIO_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD not set (see .env.medwear)}"
docker exec medwear-minio sh -c \
  "mc alias set bk http://localhost:9000 '${MINIO_USER}' '${MINIO_PASS}' --quiet 2>/dev/null && \
   mc mirror bk/medwear-bronze /tmp/minio_backup --quiet 2>/dev/null || true" 2>/dev/null || true
docker cp medwear-minio:/tmp/minio_backup "$BACKUP_DIR/minio/" 2>/dev/null || \
  echo "  ⚠ MinIO backup skipped (container not running or no data)"
docker exec medwear-minio rm -rf /tmp/minio_backup 2>/dev/null || true
echo "  ✓ MinIO"

# ── Compress everything ───────────────────────────────────────────────────────
echo ""
echo "Compressing..."
tar -czf "${BACKUP_ROOT}/medwear_backup_${TIMESTAMP}.tar.gz" -C "$BACKUP_ROOT" "$TIMESTAMP"
rm -rf "$BACKUP_DIR"
echo "✓ Archive: ${BACKUP_ROOT}/medwear_backup_${TIMESTAMP}.tar.gz"

# ── Retention: delete backups older than RETENTION_DAYS ──────────────────────
echo ""
echo "Cleaning old backups (older than ${RETENTION_DAYS} days)..."
find "$BACKUP_ROOT" -name "medwear_backup_*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete
echo "✓ Done"

echo ""
echo "===== Backup complete: ${TIMESTAMP} ====="
