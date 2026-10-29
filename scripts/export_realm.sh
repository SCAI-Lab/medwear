#!/bin/bash
# Exports the current Keycloak realm (including all users, credentials and the
# client secret) to a file of the deployment, outside this repository.
# Run this BEFORE doing `docker compose down -v` to preserve all users; the
# deployment's override mounts that file in place of docker/medwear-realm.json.
#
# Usage: ./scripts/export_realm.sh <output file>
#   e.g. ./scripts/export_realm.sh ../<your-config>/keycloak/medwear-realm.json

set -euo pipefail

REALM="medwear"
CONTAINER="medwear-keycloak"
EXPORT_PATH="${1:?usage: $0 <output file outside this repository>}"

# the tracked realm is the generic template and must not receive users or secrets
if git ls-files --error-unmatch "$EXPORT_PATH" >/dev/null 2>&1; then
  echo "refusing to write $EXPORT_PATH: it is tracked in this repository" >&2
  exit 1
fi

echo "Exporting Keycloak realm '$REALM' (including all users)..."

# Export inside the container
docker exec "$CONTAINER" \
  /opt/keycloak/bin/kc.sh export \
  --dir /tmp/realm-export \
  --realm "$REALM" \
  --users realm_file 2>/dev/null

# Copy out of the container
docker cp "$CONTAINER:/tmp/realm-export/${REALM}-realm.json" "$EXPORT_PATH"

# Cleanup inside container
docker exec "$CONTAINER" rm -rf /tmp/realm-export 2>/dev/null || true

echo "✓ Saved to $EXPORT_PATH"
echo ""
echo "You can now safely run:"
echo "  docker compose down -v"
echo "  rm .env.medwear"
echo "  ./setup.sh"
