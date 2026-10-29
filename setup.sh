#!/bin/bash
set -e

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
BLUE="\033[0;34m"
BOLD="\033[1m"
NC="\033[0m"

ok()  { echo -e "  ${GREEN}✓${NC} $1"; }
run() { echo -e "  ${YELLOW}→${NC} $1"; }
err() { echo -e "  ${RED}✗${NC} $1"; }
box() {
    echo -e "${BLUE}${BOLD}"
    echo "  ╔══════════════════════════════════════════╗"
    printf "  ║  %-40s║\n" "$1"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"
}

box "MED-API Setup"

# ── Server hostname (skip if already set or .env.medwear exists) ─────────────
if [ ! -f ".env.medwear" ]; then
  if [ -z "$SERVER_HOST" ]; then
    echo -e "  ${YELLOW}?${NC} Server hostname (e.g. medwear.example.org) [default: localhost]:"
    read -r _input_host
    SERVER_HOST="${_input_host:-localhost}"
  fi
  if [ -z "$SERVER_IP" ]; then
    echo -e "  ${YELLOW}?${NC} Server IP address [default: 127.0.0.1]:"
    read -r _input_ip
    SERVER_IP="${_input_ip:-127.0.0.1}"
  fi
  ok "Server: ${SERVER_HOST} (${SERVER_IP})"
fi
# ─────────────────────────────────────────────────────────────────────────────

if ! command -v python3 &> /dev/null; then
    err "Python 3 not found (used to generate the credentials)!"
    echo "     Install: brew install python3 (Mac) or apt install python3 (Linux)"
    exit 1
fi
ok "Python 3 found"

if ! command -v docker &> /dev/null; then
    err "Docker not found!"
    echo "     Install Docker Desktop: https://www.docker.com/products/docker-desktop"
    exit 1
fi
ok "Docker found"

if ! docker info &> /dev/null; then
    err "Docker is not running!"
    echo "     Mac: start Docker Desktop"
    echo "     Linux: sudo systemctl start docker"
    # On Linux, try starting docker automatically
    if [[ "$(uname)" == "Linux" ]]; then
        sudo systemctl start docker &> /dev/null || true
        sleep 2
    fi
    if ! docker info &> /dev/null; then
        exit 1
    fi
fi
ok "Docker is running"

# ── Credential variables ────────────────────────────────────────────────────
gen() { python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range($1)))"; }
urlencode() { python3 -c "import urllib.parse; print(urllib.parse.quote('$1', safe=''))"; }

if [ -f ".env.medwear" ]; then
  ok ".env.medwear already exists — loading credentials"
  source <(grep -v '^#' .env.medwear | sed 's/^/export /')
  PG_PASSWORD="${POSTGRES_PASSWORD:-$(gen 24)}"
  PG_PASSWORD_ENCODED=$(urlencode "$PG_PASSWORD")
  MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
  MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-$(gen 24)}"
  MINIO_API_KEY="${MINIO_API_ACCESS_KEY:-medwear-api}"
  MINIO_API_SECRET="${MINIO_API_SECRET_KEY:-$(gen 24)}"
  KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-$(gen 24)}"
  KEYCLOAK_CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-$(gen 32)}"
  INFLUX_TOKEN="${INFLUX_TOKEN:-$(gen 32)}"
  INFLUX_PASSWORD="${INFLUX_PASSWORD:-$(gen 24)}"
  GF_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD:-$(gen 24)}"
  KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}"
  GF_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-admin}"
  PGADMIN_PASSWORD="${PGADMIN_DEFAULT_PASSWORD:-$(gen 24)}"
  GLOBAL_ADMIN_PASSWORD="${GLOBAL_ADMIN_PASSWORD:-}"
else
  run "Generating secure random credentials..."
  PG_PASSWORD=$(gen 24)
  PG_PASSWORD_ENCODED=$(urlencode "$PG_PASSWORD")
  MINIO_ROOT_USER="minioadmin"
  MINIO_ROOT_PASSWORD=$(gen 24)
  MINIO_API_KEY="medwear-api"
  MINIO_API_SECRET=$(gen 32)
  KEYCLOAK_ADMIN="admin"
  KEYCLOAK_ADMIN_PASSWORD=$(gen 24)
  KEYCLOAK_CLIENT_SECRET=$(gen 32)
  INFLUX_TOKEN=$(gen 48)
  INFLUX_PASSWORD=$(gen 24)
  GF_ADMIN_USER="admin"
  GF_ADMIN_PASSWORD=$(gen 24)
  PGADMIN_PASSWORD=$(gen 24)
  GLOBAL_ADMIN_PASSWORD=$(gen 20)
  ok "Credentials generated"
fi
# ────────────────────────────────────────────────────────────────────────────

echo ""
run "Creating .env files..."
[ -f ".env.medwear" ] && { ok ".env files already exist — skipping"; } || cat > .env.medwear << ENVEOF
DATABASE_URL=postgresql+asyncpg://medwear:${PG_PASSWORD_ENCODED}@medwear-postgres:5432/medwear_processed
KEYCLOAK_URL=http://medwear-keycloak:8080
KEYCLOAK_REALM=medwear
KEYCLOAK_CLIENT_ID=med-api
KEYCLOAK_CLIENT_SECRET=${KEYCLOAK_CLIENT_SECRET}
# Keycloak admin account; the API uses it to create users
KEYCLOAK_ADMIN=${KEYCLOAK_ADMIN}
KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD}
INFLUX_URL=http://medwear-influxdb:8086
INFLUX_TOKEN=${INFLUX_TOKEN}
INFLUX_ORG=medwear
INFLUX_BUCKET=wearables
POSTGRES_USER=medwear
POSTGRES_PASSWORD=${PG_PASSWORD}
POSTGRES_HOST=medwear-postgres
POSTGRES_PORT=5432
POSTGRES_DB=medwear_processed
MINIO_ROOT_USER=${MINIO_ROOT_USER}
MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
MINIO_ENDPOINT=medwear-minio:9000
MINIO_API_ACCESS_KEY=${MINIO_API_KEY}
MINIO_API_SECRET_KEY=${MINIO_API_SECRET}
GF_SECURITY_ADMIN_USER=${GF_ADMIN_USER}
GF_SECURITY_ADMIN_PASSWORD=${GF_ADMIN_PASSWORD}
PGADMIN_DEFAULT_EMAIL=admin@medwear.com
PGADMIN_DEFAULT_PASSWORD=${PGADMIN_PASSWORD}
CORS_ALLOWED_ORIGINS=https://${SERVER_HOST},https://localhost
SERVER_HOST=${SERVER_HOST}
SERVER_IP=${SERVER_IP}
ENVEOF

# .env is read by docker compose itself (Keycloak settings, nginx build arguments)
cat > .env << ENVEOF
KEYCLOAK_ADMIN=${KEYCLOAK_ADMIN}
KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD}
KEYCLOAK_CLIENT_SECRET=${KEYCLOAK_CLIENT_SECRET}
POSTGRES_USER=medwear
POSTGRES_PASSWORD=${PG_PASSWORD}
SERVER_HOST=${SERVER_HOST:-localhost}
SERVER_IP=${SERVER_IP:-127.0.0.1}
ENVEOF
ok ".env files created"

# Save credentials summary for reference (keeps the global_admin line of an earlier run)
_ga_line=$(grep "^global_admin" .credentials.txt 2>/dev/null || true)
cat > .credentials.txt << CREDSEOF
# MedWear Credentials — generated by setup.sh
# Keep this file secure and do not commit to version control.
PostgreSQL     : medwear / ${PG_PASSWORD}
MinIO root     : ${MINIO_ROOT_USER} / ${MINIO_ROOT_PASSWORD}
MinIO API key  : ${MINIO_API_KEY} / ${MINIO_API_SECRET}
Keycloak admin : ${KEYCLOAK_ADMIN} / ${KEYCLOAK_ADMIN_PASSWORD}
InfluxDB       : admin / ${INFLUX_PASSWORD}
InfluxDB token : ${INFLUX_TOKEN}
Grafana        : ${GF_ADMIN_USER} / ${GF_ADMIN_PASSWORD}
pgAdmin        : admin@medwear.com / ${PGADMIN_PASSWORD}
CREDSEOF
[ -n "$_ga_line" ] && echo "$_ga_line" >> .credentials.txt
ok "Credentials saved to .credentials.txt"

echo ""
run "Building and starting Docker services (first run: a few minutes)..."
if docker compose version &> /dev/null; then COMPOSE="docker compose"; else COMPOSE="docker-compose"; fi
if ! $COMPOSE up -d > .setup-compose.log 2>&1; then
    err "docker compose up failed; last lines of .setup-compose.log:"
    tail -n 20 .setup-compose.log
    exit 1
fi
ok "All services started"

echo ""
run "Waiting 15 seconds for services to be ready..."
sleep 15

run "Initializing InfluxDB..."
docker exec medwear-influxdb influx setup \
    --username admin --password "${INFLUX_PASSWORD}" \
    --org medwear --bucket wearables \
    --token "${INFLUX_TOKEN}" --force > /dev/null 2>&1 \
    && ok "InfluxDB initialized" || ok "InfluxDB already initialized"

run "Setting up MinIO buckets and service account..."
sleep 5
docker exec medwear-minio mc alias set myminio http://localhost:9000 \
    "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" > /dev/null 2>&1 || true

for bucket in medwear-bronze medwear-silver medwear-gold medwear-lakehouse; do
    docker exec medwear-minio mc mb "myminio/${bucket}" --ignore-existing > /dev/null 2>&1 || true
    docker exec medwear-minio mc anonymous set none "myminio/${bucket}" > /dev/null 2>&1 || true
done

docker exec medwear-minio mc admin user add myminio \
    "${MINIO_API_KEY}" "${MINIO_API_SECRET}" > /dev/null 2>&1 || true

docker exec medwear-minio sh -c "cat > /tmp/medwear-policy.json << 'POLICYEOF'
{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Action\": [\"s3:GetObject\",\"s3:PutObject\",\"s3:DeleteObject\",\"s3:ListBucket\",\"s3:GetBucketLocation\"],
    \"Resource\": [\"arn:aws:s3:::medwear-*\",\"arn:aws:s3:::medwear-*/*\"]
  }]
}
POLICYEOF
mc admin policy create myminio medwear-api-policy /tmp/medwear-policy.json && \
mc admin policy attach myminio medwear-api-policy --user ${MINIO_API_KEY}" > /dev/null 2>&1 || true

ok "MinIO buckets and service account ready"

# Tables: the API runs its schema migrations at startup and creates the
# database of each project when the project is created.

run "Waiting for Keycloak..."
KCADM="/opt/keycloak/bin/kcadm.sh"
for _ in $(seq 1 60); do
    docker exec medwear-keycloak $KCADM config credentials --server http://localhost:8080 \
        --realm master --user "${KEYCLOAK_ADMIN}" --password "${KEYCLOAK_ADMIN_PASSWORD}" > /dev/null 2>&1 && break
    sleep 5
done
if [ -n "${GLOBAL_ADMIN_PASSWORD}" ]; then
    if docker exec medwear-keycloak $KCADM set-password -r medwear --username global_admin \
            --new-password "${GLOBAL_ADMIN_PASSWORD}" > /dev/null 2>&1; then
        uid=$(docker exec medwear-keycloak $KCADM get users -r medwear -q username=global_admin \
            --fields id --format csv --noquotes 2>/dev/null | head -1)
        [ -n "$uid" ] && docker exec medwear-keycloak $KCADM update "users/$uid" -r medwear \
            -s 'requiredActions=[]' > /dev/null 2>&1 || true
        echo "global_admin   : global_admin / ${GLOBAL_ADMIN_PASSWORD}" >> .credentials.txt
        ok "Keycloak user global_admin ready (password in .credentials.txt)"
    else
        err "Could not set the global_admin password; set it in the Keycloak console (Users -> global_admin -> Credentials)"
    fi
else
    ok "Keycloak running (existing installation: global_admin password unchanged)"
fi

run "Setting up Grafana..."
sleep 10

curl -s -m 10 -X POST http://localhost:3001/api/datasources \
    -H "Content-Type: application/json" \
    -u "${GF_ADMIN_USER}:${GF_ADMIN_PASSWORD}" \
    -d "{\"name\":\"InfluxDB\",\"type\":\"influxdb\",\"access\":\"proxy\",\"url\":\"http://medwear-influxdb:8086\",\"isDefault\":true,\"jsonData\":{\"version\":\"Flux\",\"organization\":\"medwear\",\"defaultBucket\":\"wearables\"},\"secureJsonData\":{\"token\":\"${INFLUX_TOKEN}\"}}" \
    > /dev/null 2>&1 || true

curl -s -m 10 -X POST http://localhost:3001/api/datasources \
    -H "Content-Type: application/json" \
    -u "${GF_ADMIN_USER}:${GF_ADMIN_PASSWORD}" \
    -d "{\"name\":\"PostgreSQL\",\"type\":\"grafana-postgresql-datasource\",\"access\":\"proxy\",\"url\":\"medwear-postgres:5432\",\"database\":\"medwear_processed\",\"user\":\"medwear\",\"jsonData\":{\"sslmode\":\"disable\"},\"secureJsonData\":{\"password\":\"${PG_PASSWORD}\"}}" \
    > /dev/null 2>&1 || true

if [ -f "docker/grafana/dashboards/medapi-overview.json" ]; then
    python3 -c "
import json, sys
with open('docker/grafana/dashboards/medapi-overview.json') as f:
    d = json.load(f)
d.pop('id', None)
d.pop('uid', None)
payload = {'dashboard': d, 'folderUid': '', 'overwrite': True}
print(json.dumps(payload))
" | curl -s -m 10 -X POST http://localhost:3001/api/dashboards/db \
        -H "Content-Type: application/json" \
        -u "${GF_ADMIN_USER}:${GF_ADMIN_PASSWORD}" \
        -d @- > /dev/null 2>&1 || true
fi
ok "Grafana configured"

echo ""
box "Setup Complete!"

echo -e "  ${GREEN}${BOLD}Services:${NC}"
echo -e "  Frontend ${YELLOW}→${NC} https://${SERVER_HOST:-localhost}"
echo -e "  API Docs ${YELLOW}→${NC} https://${SERVER_HOST:-localhost}/docs"
echo -e "  Keycloak ${YELLOW}→${NC} http://localhost:8180  ${BLUE}(${KEYCLOAK_ADMIN} / ${KEYCLOAK_ADMIN_PASSWORD})${NC}"
echo -e "  InfluxDB ${YELLOW}→${NC} http://localhost:8086  ${BLUE}(admin / ${INFLUX_PASSWORD})${NC}"
echo -e "  Grafana  ${YELLOW}→${NC} http://localhost:3001  ${BLUE}(${GF_ADMIN_USER} / ${GF_ADMIN_PASSWORD})${NC}"
echo -e "  MinIO    ${YELLOW}→${NC} http://localhost:9001  ${BLUE}(${MINIO_ROOT_USER} / ${MINIO_ROOT_PASSWORD})${NC}"
echo -e "  pgAdmin  ${YELLOW}→${NC} http://localhost:5050  ${BLUE}(admin@medwear.com / ${PGADMIN_PASSWORD})${NC}"
echo ""
echo -e "  ${YELLOW}All credentials saved to${NC} .credentials.txt"
echo ""
echo -e "  ${GREEN}${BOLD}Next:${NC} log in at https://${SERVER_HOST:-localhost} as global_admin,"
echo -e "  create a project and add users (see README.md, \"First steps\")."
echo ""
