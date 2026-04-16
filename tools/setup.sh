#!/bin/bash
###############################################################################
# Personal Health Vault — Setup & Startup Script
#
# Run this from the Synthesis directory on your Mac:
#   cd ~/path/to/Medical/Synthesis
#   chmod +x setup.sh
#   ./setup.sh
###############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Personal Health Vault — Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Step 1: Check prerequisites ─────────────────────────────────────────

echo ""
echo "[1/5] Checking prerequisites..."

# Docker
if ! command -v docker &> /dev/null; then
    echo "  ERROR: Docker not found."
    echo ""
    echo "  Install Docker Desktop for Mac from:"
    echo "    https://www.docker.com/products/docker-desktop/"
    echo ""
    echo "  After installing, open Docker Desktop and wait for it to start,"
    echo "  then run this script again."
    exit 1
fi

# Docker running?
if ! docker info &> /dev/null 2>&1; then
    echo "  ERROR: Docker is installed but not running."
    echo ""
    echo "  Open Docker Desktop and wait for it to start,"
    echo "  then run this script again."
    exit 1
fi

echo "  OK  Docker is installed and running"

# Docker Compose
if docker compose version &> /dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo "  ERROR: Docker Compose not found. It should come with Docker Desktop."
    exit 1
fi

echo "  OK  Docker Compose available ($COMPOSE_CMD)"

# Python
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "  ERROR: Python not found. Install Python 3.10+ from python.org"
    exit 1
fi

echo "  OK  Python available ($($PYTHON --version))"

# Set up virtual environment and install requests
if [ ! -d "$VENV_DIR" ]; then
    echo "  ...  Creating Python virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
fi
# Activate venv for the rest of this script
source "$VENV_DIR/bin/activate"
PYTHON="$VENV_DIR/bin/python"

if ! $PYTHON -c "import requests" 2>/dev/null; then
    echo "  ...  Installing Python requests library..."
    $PYTHON -m pip install requests --quiet
fi
echo "  OK  Python requests library available (in .venv)"

# Master bundle exists?
if [ ! -f "MASTER_health_record_FINAL.json" ]; then
    echo "  ERROR: MASTER_health_record_FINAL.json not found in current directory."
    echo "  Make sure you're running this from the Synthesis folder."
    exit 1
fi
echo "  OK  Master FHIR bundle found ($(du -h MASTER_health_record_FINAL.json | cut -f1))"

# ─── Step 2: Start services ──────────────────────────────────────────────

echo ""
echo "[2/5] Starting Docker services..."
echo "  This may take a few minutes on first run (downloading images)."
echo ""

$COMPOSE_CMD up -d

echo ""
echo "  OK  Docker services started"

# ─── Step 3: Wait for HAPI FHIR ──────────────────────────────────────────

echo ""
echo "[3/5] Waiting for HAPI FHIR to be ready..."
echo "  (First startup takes 1-2 minutes while it initializes the database)"

MAX_WAIT=300
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -sf http://localhost:8080/fhir/metadata > /dev/null 2>&1; then
        echo "  OK  HAPI FHIR is ready!"
        break
    fi
    sleep 5
    WAITED=$((WAITED + 5))
    echo "  ...  Waiting... (${WAITED}s)"
done

if [ $WAITED -ge $MAX_WAIT ]; then
    echo "  ERROR: HAPI FHIR did not start within ${MAX_WAIT}s."
    echo "  Check logs with: $COMPOSE_CMD logs -f hapi"
    exit 1
fi

# ─── Step 4: Check if data already loaded ─────────────────────────────────

echo ""
echo "[4/5] Checking existing data..."

PATIENT_COUNT=$(curl -sf "http://localhost:8080/fhir/Patient?_summary=count" | $PYTHON -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null || echo "0")

if [ "$PATIENT_COUNT" -gt "0" ]; then
    echo "  Found existing data ($PATIENT_COUNT patients)."
    echo ""
    read -p "  Re-ingest all data? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "  Will re-ingest (existing data will be updated)."
        $PYTHON ingest_to_hapi.py --no-resume
    else
        echo "  Skipping ingestion. Data already loaded."
    fi
else
    echo "  No data found. Starting ingestion..."
    echo ""

    # ─── Step 5: Load data ────────────────────────────────────────────────

    echo "[5/5] Loading 34,243 FHIR resources..."
    echo "  This will take several minutes."
    echo ""

    $PYTHON ingest_to_hapi.py
fi

# ─── Done ─────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Personal Health Vault is running!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  FHIR Server:   http://localhost:8080/fhir"
echo "  OpenSearch:    http://localhost:9200"
echo "  PostgreSQL:    localhost:5432 (user: hapi, db: hapi)"
echo ""
echo "  Quick test:"
echo "    curl http://localhost:8080/fhir/Patient?_summary=count"
echo "    curl http://localhost:8080/fhir/Observation?code=2093-3&_count=5"
echo ""
echo "  Search examples:"
echo "    # All labs from 2025"
echo "    curl 'http://localhost:8080/fhir/Observation?date=ge2025-01-01&date=le2025-12-31&_count=50'"
echo ""
echo "    # Full-text search for 'enterography'"
echo "    curl 'http://localhost:8080/fhir/Observation?_content=enterography'"
echo ""
echo "    # All conditions"
echo "    curl 'http://localhost:8080/fhir/Condition?_count=100'"
echo ""
echo "  Management:"
echo "    $COMPOSE_CMD logs -f hapi    # View server logs"
echo "    $COMPOSE_CMD stop            # Stop services"
echo "    $COMPOSE_CMD start           # Restart services"
echo "    $COMPOSE_CMD down            # Remove containers"
echo "    $COMPOSE_CMD down -v         # Remove containers + data"
echo ""
echo "  Search utility (activate venv first):"
echo "    source .venv/bin/activate"
echo "    python3 phv_search.py 'bone lesion'        # Full-text search"
echo "    python3 phv_search.py --type Observation    # By resource type"
echo "    python3 phv_search.py --loinc 2093-3        # By LOINC code"
echo "    python3 phv_search.py --date 2025           # By date"
echo ""
