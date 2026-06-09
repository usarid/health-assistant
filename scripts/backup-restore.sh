#!/usr/bin/env bash
# Disaster recovery: restore from the latest snapshot to a target directory.
#
# Usage:
#   scripts/backup-restore.sh /tmp/binahealth-restore
#   scripts/backup-restore.sh /tmp/binahealth-restore <snapshot-id>
#
# After restore, you'll find:
#   <target>/<staged-dir>/hapi-v1.sql  ← psql -U hapi -h localhost < this
#   <target>/<staged-dir>/hapi-v2.sql
#   <target>/<staged-dir>/chat.sql     ← sqlite3 chat.db < this
#   <target>/Users/.../Medical/         ← raw exports
#   <target>/Users/.../user_photos/     ← pill photos
#   <target>/Users/.../tools/v2/out/    ← scrape outputs
#   <target>/Users/.../tools/v3/out/
#
# Restic preserves full filesystem paths; restore puts everything under the
# target directory in its original tree, so you can rsync individual pieces
# back into place.

set -euo pipefail

ENV_FILE="$HOME/.binahealth-backup-env"
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found"; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

TARGET="${1:-}"
SNAPSHOT="${2:-latest}"

if [ -z "$TARGET" ]; then
    echo "Usage: $0 <target-dir> [snapshot-id|latest]"
    echo ""
    echo "Available snapshots:"
    restic snapshots --compact 2>&1 | tail -20
    exit 1
fi

mkdir -p "$TARGET"
echo "=== Restoring snapshot '$SNAPSHOT' to $TARGET ==="
restic restore "$SNAPSHOT" --target "$TARGET"
echo ""
echo "=== Restored. Top-level contents: ==="
find "$TARGET" -maxdepth 4 -type d 2>/dev/null | head -30
echo ""
echo "Next steps (per piece):"
echo "  HAPI v1 postgres:  docker exec -i phv-postgres    psql -U hapi    < <stage>/hapi-v1.sql"
echo "  HAPI v2 postgres:  docker exec -i phv-postgres-v2 psql -U hapi    < <stage>/hapi-v2.sql"
echo "  Chat SQLite:       docker exec -i phv-api sqlite3 /data/chat.db   < <stage>/chat.sql"
echo "  Raw exports:       rsync -a <stage>/.../Medical/  ~/usarid@gmail.com/Medical/"
echo "  Pill photos:       rsync -a <stage>/.../user_photos/  ~/Public/BinaHealth/data/pillbox/user_photos/"
echo "  tools/v3/out:      rsync -a <stage>/.../tools/v3/out/  ~/Public/BinaHealth/tools/v3/out/"
