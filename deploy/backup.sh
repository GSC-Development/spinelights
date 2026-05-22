#!/usr/bin/env bash
# Nightly backup of the SQLite DB. Uses sqlite3's online backup so it's safe
# even while the app is running (WAL mode supports concurrent readers).
#
# Drop this in /etc/cron.daily/pharos-scheduler-backup (chmod +x). Edit paths.

set -euo pipefail

DB_PATH=/var/lib/pharos-scheduler/pharos.sqlite
BACKUP_DIR=/var/backups/pharos-scheduler
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/pharos-$TS.sqlite"

/usr/bin/sqlite3 "$DB_PATH" ".backup '$OUT'"
gzip -9 "$OUT"

# Prune old backups
find "$BACKUP_DIR" -name 'pharos-*.sqlite.gz' -mtime "+$KEEP_DAYS" -delete

echo "Backup complete: $OUT.gz"
