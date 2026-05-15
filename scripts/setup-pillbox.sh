#!/bin/bash
# Set up the NLM Pillbox archive for the pill identification feature in the Meds tab.
#
# Fast path (default): clone the companion data repo at usarid/nlm-pillbox-images,
# which already has the original JPEGs, the background-removed PNG cutouts, and
# the metadata CSV. ~3 GB to download but completes in minutes instead of
# downloading 1 GB from NLM + running ~30 min of rembg locally.
#
# Slow path (fallback): if the clone fails or the user sets PILLBOX_NO_CLONE=1,
# download the archive from NLM, extract, and run rembg locally to generate the
# cutouts. Adds ~30 min on an M2 Pro.
#
# Idempotent: safe to re-run; each step skips work already done.
#
# Usage:
#   ./scripts/setup-pillbox.sh
#   PILLBOX_NO_CLONE=1 ./scripts/setup-pillbox.sh    # force slow path
#
# After this completes, recreate the api and web containers to mount the new data:
#   docker compose up -d --force-recreate api web

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_DIR/data/pillbox"
DATA_REPO_URL="https://github.com/usarid/nlm-pillbox-images.git"

echo "=== Setting up NLM Pillbox archive ==="
echo "Repo:    $REPO_DIR"
echo "Data:    $DATA_DIR"
echo ""

mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

# ─── Fast path: clone the companion data repo ─────────────────────────────
# Only attempts the clone if at least one expected artifact is missing.
needs_data() {
  [ ! -d images ] || [ ! -d images_nobg ] || [ ! -f pillbox_meta.csv ]
}

if needs_data; then
  if [ -z "${PILLBOX_NO_CLONE:-}" ]; then
    echo "Fetching pre-processed Pillbox archive from $DATA_REPO_URL (~3 GB)..."
    TMP_CLONE="$(mktemp -d -t pillbox-clone-XXXXXX)"
    # `|| true` because we want to fall back rather than exit under set -e.
    git clone --depth 1 "$DATA_REPO_URL" "$TMP_CLONE" || true
    if [ -d "$TMP_CLONE/images" ] && [ -d "$TMP_CLONE/images_nobg" ]; then
      echo "  Clone succeeded; moving artifacts into place."
      [ ! -d images ]           && mv "$TMP_CLONE/images"           images
      [ ! -d images_nobg ]      && mv "$TMP_CLONE/images_nobg"      images_nobg
      [ ! -f pillbox_meta.csv ] && mv "$TMP_CLONE/pillbox_meta.csv" pillbox_meta.csv
      rm -rf "$TMP_CLONE"
    else
      echo "  Clone failed or incomplete; falling back to NLM download."
      rm -rf "$TMP_CLONE"
    fi
  else
    echo "PILLBOX_NO_CLONE is set; skipping data-repo clone."
  fi
fi

# ─── Slow path: download from NLM directly, run rembg locally ─────────────

# 1. Metadata CSV (~80 MB, 84K rows)
if [ ! -f pillbox_meta.csv ]; then
  echo "Downloading metadata CSV (~80 MB)..."
  curl -L --progress-bar -o pillbox_meta.csv \
    "https://datadiscovery.nlm.nih.gov/api/views/crzr-uvwg/rows.csv?accessType=DOWNLOAD"
fi

# 2. Image archive (~1 GB)
if [ ! -d images ] || [ -z "$(ls -A images 2>/dev/null)" ]; then
  if [ ! -f pillbox_images.zip ]; then
    echo "Downloading image archive (~1 GB, takes a few minutes)..."
    curl -L --progress-bar -o pillbox_images.zip \
      "https://ftp.nlm.nih.gov/projects/pillbox/pillbox_production_images_full_202008.zip"
  fi
  echo "Extracting images..."
  mkdir -p images
  (cd images && unzip -q ../pillbox_images.zip)
  echo "  Extracted $(find images -type f \( -iname '*.jpg' -o -iname '*.png' \) | wc -l | tr -d ' ') image files."
fi

# 3. Build SQLite DB (always — generates from CSV, cheap)
echo ""
echo "Building SQLite database from metadata CSV..."
python3 "$REPO_DIR/scripts/build_pillbox_db.py"

# 4. Background removal (only if images_nobg/ wasn't supplied by the data repo)
if [ ! -d images_nobg ] || [ -z "$(ls -A images_nobg 2>/dev/null)" ]; then
  echo ""
  echo "Removing photo backgrounds (rembg + u2netp). One-time, ~30 min on M2 Pro."
  VENV="$DATA_DIR/.venv"
  if [ ! -d "$VENV" ]; then
    echo "Creating Python venv for rembg at $VENV..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet rembg onnxruntime pillow
  fi
  "$VENV/bin/python3" "$REPO_DIR/scripts/remove_pillbox_backgrounds.py"
fi

echo ""
echo "=== Done ==="
echo ""
echo "Next: recreate the api and web containers so they mount the new data:"
echo "  docker compose up -d --force-recreate api web"
echo ""
echo "Then test the endpoint:"
echo "  curl -sk https://localhost:3000/api/meds/pill-image?name=doxycycline | head"
