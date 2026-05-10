#!/bin/bash
# Set up the NLM Pillbox archive for the pill identification feature in the Meds tab.
# Downloads ~1 GB of images + ~80 MB metadata, extracts, and builds a SQLite DB.
# Idempotent: safe to re-run (will skip downloads if files exist, rebuild DB).
#
# Usage:
#   ./scripts/setup-pillbox.sh
#
# After this completes, recreate the api and web containers to mount the new data:
#   docker compose up -d --force-recreate api web

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_DIR/data/pillbox"

echo "=== Setting up NLM Pillbox archive ==="
echo "Repo:    $REPO_DIR"
echo "Data:    $DATA_DIR"
echo ""

mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

# 1. Metadata CSV (~80 MB, 84K rows)
if [ ! -f pillbox_meta.csv ]; then
  echo "Downloading metadata CSV (~80 MB)..."
  curl -L --progress-bar -o pillbox_meta.csv \
    "https://datadiscovery.nlm.nih.gov/api/views/crzr-uvwg/rows.csv?accessType=DOWNLOAD"
else
  echo "Metadata CSV already present, skipping download."
fi

# 2. Image archive (~1 GB)
if [ ! -f pillbox_images.zip ]; then
  echo "Downloading image archive (~1 GB, takes a few minutes)..."
  curl -L --progress-bar -o pillbox_images.zip \
    "https://ftp.nlm.nih.gov/projects/pillbox/pillbox_production_images_full_202008.zip"
else
  echo "Image archive already present, skipping download."
fi

# 3. Extract images
if [ ! -d images ] || [ -z "$(ls -A images 2>/dev/null)" ]; then
  echo "Extracting images..."
  mkdir -p images
  (cd images && unzip -q ../pillbox_images.zip)
  echo "  Extracted $(find images -type f \( -iname '*.jpg' -o -iname '*.png' \) | wc -l | tr -d ' ') image files."
else
  echo "Images already extracted, skipping."
fi

# 4. Build SQLite DB
echo ""
echo "Building SQLite database from metadata CSV..."
python3 "$REPO_DIR/scripts/build_pillbox_db.py"

echo ""
echo "=== Done ==="
echo ""
echo "Next: recreate the api and web containers so they mount the new data:"
echo "  docker compose up -d --force-recreate api web"
echo ""
echo "Then test the endpoint:"
echo "  curl -sk https://localhost:3000/api/meds/pill-image?name=doxycycline | head"
