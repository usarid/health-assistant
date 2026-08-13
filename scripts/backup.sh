#!/usr/bin/env bash
# BinaHealth nightly backup runner.
#
# What gets backed up (everything truly irreplaceable from the audit):
#   - HAPI v2 postgres (logical dump, portable across HAPI versions)
#   - SQLite chat.db (logical dump)
#   - ~/usarid@gmail.com/Medical/ (raw AHR exports + portal scrape outputs)
#   - data/pillbox/user_photos/ (user-uploaded photos of actual pills)
#   - tools/v2/out/ + tools/v3/out/ (gitignored scrape outputs with PHI)
#   - tools/v2/patient_config/{org_mapping,patient_identity}.json (gitignored)
#
# Skipped (regenerable from other sources):
#   - OpenSearch (HAPI rebuilds index on demand from postgres)
#   - data/pillbox/ images (already a separate GitHub repo)
#   - data/pillbox/.venv (pip install from requirements)
#   - code under tools/ api/ web/ mobile/ (in git)
#
# Run nightly via launchd (see install-backup-schedule.sh).

set -euo pipefail

# launchd runs with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that
# includes neither Homebrew nor Docker Desktop. Prepending both keeps the
# script runnable from any low-PATH context (launchd, systemd, cron, an
# empty ssh session) without changing behaviour when run from a normal
# shell that already has these on PATH. Broke silently 2026-06-09 →
# 2026-08-10 (exit 127, docker: command not found).
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

ENV_FILE="$HOME/.binahealth-backup-env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found. Copy scripts/backup-env.example, fill in values, chmod 600." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"
for v in B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_REPOSITORY RESTIC_PASSWORD; do
    if [ -z "${!v:-}" ] || [[ "${!v}" == REPLACE_WITH_* ]]; then
        echo "ERROR: $v not set in $ENV_FILE (or still has the placeholder)." >&2
        exit 1
    fi
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_PREFIX="[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]"
echo "$LOG_PREFIX backup starting"

# ── Stage logical dumps in a temp dir ────────────────────────────────────
STAGE=$(mktemp -d -t binahealth-backup)
trap 'rm -rf "$STAGE"' EXIT

# Postgres dump. pg_dumpall captures all databases + roles, portable across
# HAPI/Postgres versions. The docker exec call uses the container's own
# postgres credentials (no host-side libpq needed). The v1 stack was
# retired 2026-08-11; its last dump lives in restic snapshot d4e69aaa
# (2026-08-10) if ever needed.
echo "$LOG_PREFIX dumping HAPI v2 postgres"
docker exec -i phv-postgres-v2 pg_dumpall -U hapi > "$STAGE/hapi-v2.sql"

# SQLite logical dump. .dump produces a portable SQL text; survives schema
# migrations across SQLite versions; trivially restored via sqlite3 .read.
# The phv-api container doesn't ship sqlite3, so we copy the .db file out
# and dump it with the host's sqlite3 (macOS ships /usr/bin/sqlite3).
echo "$LOG_PREFIX dumping app SQLite (chat.db)"
docker cp phv-api:/data/chat.db "$STAGE/chat.db"
sqlite3 "$STAGE/chat.db" .dump > "$STAGE/chat.sql"
rm "$STAGE/chat.db"

echo "$LOG_PREFIX dump sizes:"
du -sh "$STAGE"/*.sql | sed 's/^/  /'

# ── restic backup ────────────────────────────────────────────────────────
# Each --files-from-verbatim invocation accumulates paths into ONE snapshot.
# We compose the list here so a single snapshot represents "the full state."

INCLUDE_LIST="$STAGE/include.txt"
{
    echo "$STAGE"
    echo "$HOME/usarid@gmail.com/Medical"
    echo "$REPO_ROOT/data/pillbox/user_photos"
    [ -d "$REPO_ROOT/tools/v2/out" ] && echo "$REPO_ROOT/tools/v2/out"
    [ -d "$REPO_ROOT/tools/v3/out" ] && echo "$REPO_ROOT/tools/v3/out"
    [ -f "$REPO_ROOT/tools/v2/patient_config/org_mapping.json" ] && echo "$REPO_ROOT/tools/v2/patient_config/org_mapping.json"
    [ -f "$REPO_ROOT/tools/v2/patient_config/patient_identity.json" ] && echo "$REPO_ROOT/tools/v2/patient_config/patient_identity.json"
    [ -f "$REPO_ROOT/tools/portal_ingest_config.json" ] && echo "$REPO_ROOT/tools/portal_ingest_config.json"
    [ -f "$REPO_ROOT/.env" ] && echo "$REPO_ROOT/.env"
} > "$INCLUDE_LIST"

echo "$LOG_PREFIX backing up — paths:"
sed 's/^/  /' "$INCLUDE_LIST"

restic backup \
    --files-from-verbatim "$INCLUDE_LIST" \
    --tag binahealth-nightly \
    --exclude '.DS_Store' \
    --exclude '*.pyc' \
    --exclude '__pycache__' \
    --host "$(hostname -s)"

# ── Retention + prune ────────────────────────────────────────────────────
# Keep 7 daily, 4 weekly, 12 monthly. Older snapshots dropped + space reclaimed.
echo "$LOG_PREFIX applying retention"
restic forget --tag binahealth-nightly \
    --keep-daily 7 \
    --keep-weekly 4 \
    --keep-monthly 12 \
    --prune

# ── Integrity check (subset — fast) ──────────────────────────────────────
# Full check on Sundays only (~1 hr at our size); rest of the week, structural
# check only (~30s).
if [ "$(date +%u)" = "7" ]; then
    echo "$LOG_PREFIX weekly full integrity check"
    restic check --read-data-subset=10%
else
    echo "$LOG_PREFIX structural integrity check"
    restic check
fi

echo "$LOG_PREFIX backup complete"
