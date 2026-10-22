#!/bin/sh
# One-shot dump of scanner (+ wine) container logs. The scheduled exporter is
# grok-log-export (/volume1/docker/grok-ssh/export-logs.sh, every 300s).
#
# Never deletes containers. Appends each slice onto a rolling file so a
# compose recreate does not wipe history already exported.
#
# Each container has its own cursor (.$name.last_run). A failed fetch does
# not advance that container's since, so one dead name cannot skip others.
set -eu
OUT_DIR=${OUT_DIR:-/volume1/docker/grok-logs}
FILTER=${LOG_FILTER:-wine}
SINCE=${LOG_SINCE:-72h}
MAX_BYTES=${LOG_MAX_BYTES:-25000000}
LAST_FILE="$OUT_DIR/.last_run"

mkdir -p "$OUT_DIR"
NOW=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

since_for() {
  c=$1
  if [ -f "$OUT_DIR/.$c.last_run" ]; then
    cat "$OUT_DIR/.$c.last_run"
  elif [ -f "$LAST_FILE" ]; then
    cat "$LAST_FILE"
  elif ls "$OUT_DIR"/*.log >/dev/null 2>&1; then
    # Existing dumps already cover history — don't append a 72h replay.
    printf '%s\n' "$NOW"
  else
    printf '%s\n' "$SINCE"
  fi
}

for c in $(docker ps -a --format '{{.Names}}' | awk -v f="$FILTER" 'index($0,f)'); do
  dest="$OUT_DIR/$c.log"
  tmp="$OUT_DIR/.$c.log.slice"
  since=$(since_for "$c")
  # docker logs writes container stderr to the client's stderr — that's where
  # Django, Celery, Postgres and the bot actually log. Do not discard it.
  if docker logs --timestamps --since "$since" "$c" > "$tmp" 2>&1; then
    if [ -s "$tmp" ]; then
      cat "$tmp" >> "$dest"
      chmod 644 "$dest" 2>/dev/null || true
    fi
    printf '%s\n' "$NOW" > "$OUT_DIR/.$c.last_run"
  fi
  rm -f "$tmp"
  if [ -f "$dest" ]; then
    size=$(wc -c < "$dest" | tr -d ' ')
    if [ "$size" -gt "$MAX_BYTES" ]; then
      tail -c "$MAX_BYTES" "$dest" > "$dest.trim" && mv "$dest.trim" "$dest"
      chmod 644 "$dest" 2>/dev/null || true
    fi
  fi
done

printf '%s\n' "$NOW" > "$LAST_FILE"
printf '%s\n' "$NOW" > "$OUT_DIR/last_updated.txt"
echo "appended $OUT_DIR"
