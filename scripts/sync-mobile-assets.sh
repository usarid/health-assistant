#!/usr/bin/env bash
# Copies the Stanford CSN list from tools/v3/out/ into mobile/assets/ so
# the Flutter build can bundle it. Both source and destination are
# gitignored (PHI never enters the repo).
#
# Run this before `flutter run` if you've re-scraped or never copied yet.
# Idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/tools/v3/out/stanford-v3-visits.json"
DST_DIR="$REPO_ROOT/mobile/assets"
DST="$DST_DIR/stanford-v3-visits.json"

if [ ! -f "$SRC" ]; then
    cat <<EOF >&2
ERROR: $SRC not found.

The mobile prototype's "Scrape all" button reads the CSN list from this
file. To produce it, run the Stanford v3 scrape from a browser; see
tools/v3/RUNBOOK.md.

For a quick test, you can put a minimal JSON at $SRC with the shape:
  { "visits": [{ "item": { "Csn": "<csn>" },
                 "response": { "notesInfo": { "isAtLeastOneNoteShareable": true,
                                              "notesReport": { "reportID": "x" } } } }] }
EOF
    exit 1
fi

mkdir -p "$DST_DIR"
cp "$SRC" "$DST"
echo "Copied $(du -h "$SRC" | cut -f1) → $DST"
echo "  visits in file: $(python3 -c "import json; print(len(json.load(open('$SRC'))['visits']))")"
