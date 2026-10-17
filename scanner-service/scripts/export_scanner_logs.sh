#!/bin/sh
# One-shot dump of scanner (+ wine) container logs. The scheduled exporter is
# grok-log-export (/volume1/docker/grok-ssh/export-logs.sh, every 300s).
set -eu
OUT_DIR=${OUT_DIR:-/volume1/docker/grok-logs}
FILTER=${LOG_FILTER:-wine}
SINCE=${LOG_SINCE:-72h}

mkdir -p "$OUT_DIR"
for c in $(docker ps -a --format '{{.Names}} {{.Status}}' | awk -v f="$FILTER" 'index($1,f){print $1}'); do
  status=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  case "$status" in
    running) ;;
    missing) ;;
    *) docker rm -f "$c" >/dev/null 2>&1 || true ;;
  esac
done
# Drop leftover id-prefixed files from older exporters / recreates.
for stale in "$OUT_DIR"/*_wine-*.log; do
  [ -e "$stale" ] || continue
  rm -f "$stale"
done
for c in $(docker ps --format '{{.Names}}' | awk -v f="$FILTER" 'index($0,f)'); do
  docker logs --timestamps --since "$SINCE" "$c" > "$OUT_DIR/.$c.log.tmp" 2>&1
  chmod 644 "$OUT_DIR/.$c.log.tmp"
  mv -f "$OUT_DIR/.$c.log.tmp" "$OUT_DIR/$c.log"
done
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$OUT_DIR/last_updated.txt"
echo "wrote $OUT_DIR (filter=$FILTER since=$SINCE)"
