#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "════════════════════════════════════════"
echo " MedWear ROS 2 Integration Test"
echo "════════════════════════════════════════"

echo "[1/3] Building bridge image..."
docker build -t medwear-ros2-to-medwear:latest .

echo "[2/3] Building test image..."
docker build -t medwear-ros2-test:latest -f test/Dockerfile.test .

echo "[3/3] Running integration test..."
docker run --rm medwear-ros2-test:latest