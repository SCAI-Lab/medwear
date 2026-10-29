#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "════════════════════════════════════════"
echo " MedWear ROS 2 Integration Test"
echo "════════════════════════════════════════"

echo "[1/4] Building bridge image..."
docker build -t medwear-ros2-to-medwear:latest .

echo "[2/4] Building test image..."
docker build -t medwear-ros2-test:latest -f test/Dockerfile.test .

echo "[3/4] Running integration test (EEG and HR)..."
docker run --rm medwear-ros2-test:latest

echo "[4/4] Running the test of every supported message type..."
docker run --rm medwear-ros2-test:latest /test/run_all_types_test.sh
