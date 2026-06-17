#!/usr/bin/env bash
# Copies mobile-app input files from tools/v3/out/ into mobile/assets/
# so the Flutter build can bundle them. All sources + destinations are
# gitignored (PHI never enters the repo).
#
# Currently syncs:
#   stanford-v3-visits.json     — CSN list for the per-visit note scraper
#   stanford-lab-orders.json    — eorderid list for the per-lab-result
#                                  HTML fetcher (Phase 4-2). Optional;
#                                  produced by tools/mobile/build_lab_orders_discovery.py.
#
# Run this before `flutter run` if you've re-scraped or never copied yet.
# Idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/tools/v3/out"
DST_DIR="$REPO_ROOT/mobile/assets"
mkdir -p "$DST_DIR"

# Visits file (required)
SRC_VISITS="$SRC_DIR/stanford-v3-visits.json"
DST_VISITS="$DST_DIR/stanford-v3-visits.json"
if [ ! -f "$SRC_VISITS" ]; then
    cat <<EOF >&2
ERROR: $SRC_VISITS not found.

The mobile prototype's "Scrape all" button reads the CSN list from this
file. To produce it, run the Stanford v3 scrape from a browser; see
tools/v3/RUNBOOK.md.

For a quick test, you can put a minimal JSON at $SRC_VISITS with the shape:
  { "visits": [{ "item": { "Csn": "<csn>" },
                 "response": { "notesInfo": { "isAtLeastOneNoteShareable": true,
                                              "notesReport": { "reportID": "x" } } } }] }
EOF
    exit 1
fi
cp "$SRC_VISITS" "$DST_VISITS"
echo "Copied $(du -h "$SRC_VISITS" | cut -f1) → $DST_VISITS"
echo "  visits in file: $(python3 -c "import json; print(len(json.load(open('$SRC_VISITS'))['visits']))")"

# Lab orders file (optional — only present if you ran build_lab_orders_discovery.py)
SRC_LABS="$SRC_DIR/stanford-lab-orders.json"
DST_LABS="$DST_DIR/stanford-lab-orders.json"
if [ -f "$SRC_LABS" ]; then
    cp "$SRC_LABS" "$DST_LABS"
    echo "Copied $(du -h "$SRC_LABS" | cut -f1) → $DST_LABS"
    echo "  lab orders in file: $(python3 -c "import json; print(json.load(open('$SRC_LABS'))['count'])")"
else
    echo "(skipping lab orders — $SRC_LABS not present; run tools/mobile/build_lab_orders_discovery.py first)"
fi
