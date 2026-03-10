#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "=== Stopping services ==="
./stop.sh

echo ""
echo "=== Starting services ==="
./start.sh
