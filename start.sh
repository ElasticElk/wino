#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "Starting Wine Cellar..."
docker compose -f wine-cellar/docker-compose.yml up -d

echo "Waiting for Wine Cellar to become healthy..."
timeout 60 bash -c '
  until docker compose -f wine-cellar/docker-compose.yml ps web --format json 2>/dev/null \
    | grep -q '"'"'"healthy"'"'"'; do
    sleep 2
  done
' && echo "Wine Cellar is healthy." || echo "Warning: timed out waiting for health check, starting scanner anyway..."

echo "Giving Wine Cellar a few more seconds to settle..."
sleep 5

echo "Starting Scanner Service..."
docker compose -f scanner-service/docker-compose.yml up -d

echo "All services started."
