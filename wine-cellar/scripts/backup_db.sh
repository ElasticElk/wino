#!/bin/sh
# Run this on the NAS. The output directory should be a shared folder you
# already back up, for example a Synology share:
#   /volume1/docker/wino/wine-cellar/scripts/backup_db.sh /volume1/backups/wino
set -eu
if [ "$#" -ne 1 ]; then
  echo "usage: backup_db.sh OUTPUT_DIR" >&2
  exit 1
fi
out=$1
mkdir -p "$out"
stamp=$(date +%Y%m%d-%H%M)
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
docker compose exec -T db pg_dump -U wino -d wino_dev -Fc > "$out/wino-$stamp.dump"
media=$root/mediafiles
if [ -d "$media" ]; then
  tar -C "$root" -cf "$out/wino-$stamp-media.tar" mediafiles
fi
find "$out" -type f \( -name 'wino-*.dump' -o -name 'wino-*-media.tar' \) -mtime +14 -delete
