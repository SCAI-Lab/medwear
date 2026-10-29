#!/bin/bash
# Exports the current Keycloak realm (including all users) to docker/medwear-realm.json.
# Run this BEFORE doing `docker compose down -v` to preserve all users.
#
# Usage: ./scripts/export_realm.sh

set -euo pipefail

REALM="medwear"
CONTAINER="medwear-keycloak"
EXPORT_PATH="docker/medwear-realm.json"

echo "Exporting Keycloak realm '$REALM' (including all users)..."

# Export inside the container
docker exec "$CONTAINER" \
  /opt/keycloak/bin/kc.sh export \
  --dir /tmp/realm-export \
  --realm "$REALM" \
  --users realm_file 2>/dev/null

# Copy out to repo
docker cp "$CONTAINER:/tmp/realm-export/${REALM}-realm.json" "$EXPORT_PATH"

# Cleanup inside container
docker exec "$CONTAINER" rm -rf /tmp/realm-export 2>/dev/null || true

echo "✓ Saved to $EXPORT_PATH"
echo ""
echo "You can now safely run:"
echo "  docker compose -f docker-compose.medwear-minimal.yml down -v"
echo "  rm .env.medwear"
echo "  ./setup.sh"
