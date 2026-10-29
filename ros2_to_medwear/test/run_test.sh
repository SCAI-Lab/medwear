#!/bin/bash
# Integration test: runs mock API + subscriber inside the container,
# publishes a fake ROS 2 message, verifies the batch was POSTed.
set -e

source /opt/ros/jazzy/setup.bash
source /ros2_ws/install/setup.bash

RECEIVED=/tmp/received_batches.json
PASS=0

cleanup() {
    kill "$MOCK_PID" "$SUB_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ── 1. Start mock API ──────────────────────────────────────────────────────────
python3 /test/mock_api.py &
MOCK_PID=$!
sleep 1
echo "[TEST] Mock API started (pid=$MOCK_PID)"

# ── 2. Start subscriber ────────────────────────────────────────────────────────
export API_BASE_URL=http://localhost:8080/v1
export MEDWEAR_USERNAME=test
export MEDWEAR_PASSWORD=test
python3 /bridge/medwear_subscriber.py \
    --config /test/test_config.yaml \
    --device test-device \
    --patient 1 \
    --project test-project &
SUB_PID=$!
echo "[TEST] Subscriber started (pid=$SUB_PID)"
sleep 4   # wait for ROS 2 discovery + token fetch

# ── 3. Publish test EEG message ───────────────────────────────────────────────
echo "[TEST] Publishing /test/eeg ..."
ros2 topic pub --once /test/eeg healthcare_msgs/msg/EEG \
    "{header: {stamp: {sec: 1700000000, nanosec: 0}}, session_id: 'test-session', eeg: [$(python3 -c "print(','.join(['1.5']*256))")]}"

echo "[TEST] Publishing /test/hr ..."
ros2 topic pub --once /test/hr healthcare_msgs/msg/HeartRate \
    "{header: {stamp: {sec: 1700000001, nanosec: 0}}, session_id: 'test-session', heart_rate: [72.0]}"

sleep 3   # let subscriber process and POST

# ── 4. Check results ───────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo " TEST RESULTS"
echo "════════════════════════════════════════"

if [ ! -f "$RECEIVED" ]; then
    echo "[FAIL] No batches received by mock API"
    exit 1
fi

COUNT=$(python3 -c "import json; d=json.load(open('$RECEIVED')); print(len(d))")
echo "[INFO] Batches received: $COUNT"

if [ "$COUNT" -ge 1 ]; then
    echo "[PASS] Subscriber successfully POSTed to the API"
    python3 -c "
import json
batches = json.load(open('$RECEIVED'))
for b in batches:
    sig = b.get('signal_type', '?')
    body = b.get('body', {})
    samples = body.get('signal_data') or body.get('ecg_lead_data') or []
    tf = body.get('effective_time_frame', {}).get('time_interval', {})
    print(f'  signal={sig}  samples={len(samples)}  start={tf.get(\"start_date_time\",\"?\")}')
"
    PASS=1
else
    echo "[FAIL] Mock API received 0 batches"
fi

echo "════════════════════════════════════════"
[ "$PASS" -eq 1 ] && exit 0 || exit 1