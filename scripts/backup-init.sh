#!/usr/bin/env bash
# One-time: validate the env file and initialize the restic repo on B2.
# Idempotent — safe to re-run if "init" already happened.

set -euo pipefail

ENV_FILE="$HOME/.binahealth-backup-env"
if [ ! -f "$ENV_FILE" ]; then
    cat <<EOF >&2
ERROR: $ENV_FILE not found.

  cp $(dirname "$0")/backup-env.example ~/.binahealth-backup-env
  chmod 600 ~/.binahealth-backup-env
  # Then edit the file and fill in B2_ACCOUNT_ID, B2_ACCOUNT_KEY,
  # RESTIC_REPOSITORY (bucket name), and RESTIC_PASSWORD.
EOF
    exit 1
fi

# Permissions sanity — env contains long-lived secrets.
PERM=$(stat -f '%A' "$ENV_FILE")
if [ "$PERM" != "600" ]; then
    echo "WARNING: $ENV_FILE permissions are $PERM, should be 600"
    echo "  fix:  chmod 600 $ENV_FILE"
fi

# shellcheck disable=SC1090
source "$ENV_FILE"
for v in B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_REPOSITORY RESTIC_PASSWORD; do
    if [ -z "${!v:-}" ] || [[ "${!v}" == REPLACE_WITH_* ]]; then
        echo "ERROR: $v not set in $ENV_FILE (or still has the REPLACE_WITH_ placeholder)." >&2
        exit 1
    fi
done

command -v restic > /dev/null || { echo "ERROR: restic not on PATH (brew install restic)"; exit 1; }

echo "=== Connecting to $RESTIC_REPOSITORY ==="
if restic snapshots > /dev/null 2>&1; then
    echo "  ✓ repo already initialized — existing snapshots:"
    restic snapshots --compact | tail -20
else
    echo "  repo not yet initialized — running restic init"
    restic init
    echo "  ✓ repo initialized"
fi

echo ""
echo "Next: run scripts/backup.sh to do the first backup. Expect ~5-15 min"
echo "for ~8 GB on a typical home connection. Subsequent backups upload only"
echo "changes (typically <100 MB nightly) and finish in under a minute."
