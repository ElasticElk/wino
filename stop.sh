#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "Stopping Scanner Service..."
docker compose -f scanner-service/docker-compose.yml down

echo "Stopping Wine Cellar..."
docker compose -f wine-cellar/docker-compose.yml down

echo "All services stopped."
