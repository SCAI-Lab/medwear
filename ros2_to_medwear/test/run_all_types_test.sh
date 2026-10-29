#!/bin/bash
# End-to-end test of every supported healthcare_msgs type: mock API + bridge
# (test/all_types_config.yaml) + publish_all_types.py, then check_all_types.py
# compares what the mock API received with what was published.
#
# Needs ROS 2 and healthcare_msgs sourced, e.g.
#   source /opt/ros/jazzy/setup.bash && source <ws>/install/setup.bash
#   ros2_to_medwear/test/run_all_types_test.sh [--no-sessions]
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="${BRIDGE_DIR:-$(dirname "$TEST_DIR")}"
OUT_DIR="$(mktemp -d)"
PORT="${MOCK_PORT:-18080}"

cleanup() { kill "${MOCK_PID:-}" "${SUB_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT

MOCK_OUT_DIR="$OUT_DIR" MOCK_PORT="$PORT" python3 "$TEST_DIR/mock_api.py" > "$OUT_DIR/mock.log" 2>&1 &
MOCK_PID=$!
sleep 1

sed "s#http://localhost:8080/v1#http://localhost:$PORT/v1#" "$TEST_DIR/all_types_config.yaml" > "$OUT_DIR/config.yaml"
API_BASE_URL="http://localhost:$PORT/v1" MEDWEAR_USERNAME=test MEDWEAR_PASSWORD=test \
    python3 "$BRIDGE_DIR/medwear_subscriber.py" --config "$OUT_DIR/config.yaml" \
    --device test-device --patient 1 --project medwear_test "$@" > "$OUT_DIR/bridge.log" 2>&1 &
SUB_PID=$!
sleep 5   # ROS 2 discovery and token

python3 "$TEST_DIR/publish_all_types.py"
sleep 4   # last batch windows (1 s) and uploads

kill -INT "$SUB_PID" 2>/dev/null || true   # flushes what is still buffered
wait "$SUB_PID" 2>/dev/null || true
sleep 1

python3 "$TEST_DIR/check_all_types.py" "$OUT_DIR" || {
    echo "kept for inspection: $OUT_DIR (bridge.log, mock.log, received_*.json)"; exit 1; }
rm -rf "$OUT_DIR"
