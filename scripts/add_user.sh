#!/bin/bash
# Usage: ./add_user.sh <new_username> <new_password> <role> <project_id>
# Roles: researcher, collaborator, device, project_admin
# Example: ./add_user.sh test2 test1234 researcher medwear_project1

set -e

NEW_USERNAME="$1"
NEW_PASSWORD="$2"
NEW_ROLE="$3"
PROJECT_ID="$4"

KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8180}"
REALM="medwear"
CLIENT_ID="med-api"

# --- Validate args ---
if [ -z "$NEW_USERNAME" ] || [ -z "$NEW_PASSWORD" ] || [ -z "$NEW_ROLE" ] || [ -z "$PROJECT_ID" ]; then
  echo "Usage: $0 <new_username> <new_password> <role> <project_id>"
  echo "Roles: researcher, collaborator, device, project_admin"
  exit 1
fi

VALID_ROLES="researcher collaborator device project_admin"
if ! echo "$VALID_ROLES" | grep -qw "$NEW_ROLE"; then
  echo "ERROR: Invalid role '$NEW_ROLE'. Choose from: $VALID_ROLES"
  exit 1
fi

# --- Authenticate caller ---
echo -n "Your username: "
read CALLER_USER
echo -n "Your password: "
read -s CALLER_PASS
echo

echo "Authenticating..."
TOKEN_RESPONSE=$(curl -s -X POST "$KEYCLOAK_URL/realms/$REALM/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=$CLIENT_ID" \
  -d "username=$CALLER_USER" \
  -d "password=$CALLER_PASS")

ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$ACCESS_TOKEN" ]; then
  echo "ERROR: Authentication failed. Check your username/password."
  exit 1
fi

# --- Decode roles from JWT ---
PAYLOAD=$(echo "$ACCESS_TOKEN" | cut -d'.' -f2 | sed 's/-/+/g; s/_/\//g')
# Add padding
PAD=$(( ${#PAYLOAD} % 4 ))
if [ $PAD -ne 0 ]; then PAYLOAD="${PAYLOAD}$(printf '=%.0s' $(seq 1 $((4-PAD))))"; fi
DECODED=$(echo "$PAYLOAD" | base64 -d 2>/dev/null)

HAS_GLOBAL=$(echo "$DECODED" | grep -o '"global_admin"' | head -1)
HAS_PROJECT_ADMIN=$(echo "$DECODED" | grep -o '"project_admin"' | head -1)

if [ -z "$HAS_GLOBAL" ] && [ -z "$HAS_PROJECT_ADMIN" ]; then
  echo "ERROR: Permission denied. Only global_admin or project_admin can add users."
  exit 1
fi

# project_admin can only add to their own project
if [ -z "$HAS_GLOBAL" ] && [ -n "$HAS_PROJECT_ADMIN" ]; then
  CALLER_PROJECT=$(echo "$DECODED" | grep -o '"project_id":"[^"]*"' | cut -d'"' -f4)
  if [ "$CALLER_PROJECT" != "$PROJECT_ID" ]; then
    echo "ERROR: project_admin can only add users to their own project ($CALLER_PROJECT)."
    exit 1
  fi
  # project_admin cannot create other project_admins or global_admins
  if [ "$NEW_ROLE" = "project_admin" ]; then
    echo "ERROR: project_admin cannot create another project_admin. Contact global_admin."
    exit 1
  fi
fi

echo "✓ Authorization OK"

# --- Get Keycloak admin token ---
ADMIN_PASS=$(docker exec medwear-keycloak env | grep KEYCLOAK_ADMIN_PASSWORD | cut -d'=' -f2)
ADMIN_USER=$(docker exec medwear-keycloak env | grep KEYCLOAK_ADMIN= | cut -d'=' -f2)

ADMIN_TOKEN=$(curl -s -X POST "http://localhost:8180/realms/master/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=admin-cli" \
  -d "username=$ADMIN_USER" \
  -d "password=$ADMIN_PASS" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$ADMIN_TOKEN" ]; then
  echo "ERROR: Could not get Keycloak admin token."
  exit 1
fi

# --- Create user in Keycloak ---
echo "Creating user '$NEW_USERNAME' in Keycloak..."
CREATE_RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "http://localhost:8180/realms/$REALM/users" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$NEW_USERNAME\",\"enabled\":true,\"email\":\"$NEW_USERNAME@medwear.local\"}")

if [ "$CREATE_RESP" = "409" ]; then
  echo "ERROR: User '$NEW_USERNAME' already exists."
  exit 1
elif [ "$CREATE_RESP" != "201" ]; then
  echo "ERROR: Failed to create user (HTTP $CREATE_RESP)."
  exit 1
fi

# --- Set password ---
USER_ID=$(curl -s \
  "http://localhost:8180/realms/$REALM/users?username=$NEW_USERNAME" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)

curl -s -o /dev/null -X PUT \
  "http://localhost:8180/realms/$REALM/users/$USER_ID/reset-password" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"type\":\"password\",\"value\":\"$NEW_PASSWORD\",\"temporary\":false}"

# --- Assign client role ---
CLIENT_UUID=$(curl -s \
  "http://localhost:8180/realms/$REALM/clients?clientId=$CLIENT_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)

ROLE_OBJ=$(curl -s \
  "http://localhost:8180/realms/$REALM/clients/$CLIENT_UUID/roles/$NEW_ROLE" \
  -H "Authorization: Bearer $ADMIN_TOKEN")

ROLE_ID=$(echo "$ROLE_OBJ" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)

curl -s -o /dev/null -X POST \
  "http://localhost:8180/realms/$REALM/users/$USER_ID/role-mappings/clients/$CLIENT_UUID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "[{\"id\":\"$ROLE_ID\",\"name\":\"$NEW_ROLE\"}]"

# --- Add to DB ---
echo "Adding '$NEW_USERNAME' to project '$PROJECT_ID' in database..."
docker exec medwear-postgres psql -U medwear -d "$PROJECT_ID" \
  -c "INSERT INTO user_project (username, project_id, role) VALUES ('$NEW_USERNAME', '$PROJECT_ID', '$NEW_ROLE') ON CONFLICT DO NOTHING;"

echo ""
echo "✓ User '$NEW_USERNAME' created successfully!"
echo "  Role    : $NEW_ROLE"
echo "  Project : $PROJECT_ID"
echo "  Password: $NEW_PASSWORD"
