#!/usr/bin/env bash
# Ingest a mobile-app scrape batch into v2 HAPI.
#
# What it does:
#   1. Finds the latest stanford-batch-*.json the mobile app wrote into the
#      iOS Simulator's app sandbox.
#   2. Rewrites it into the v3-notes file shape that tools/v3/convert_to_v2_bundle.py
#      already knows how to consume (mobile output replaces the skeleton
#      content the original v3 scrape had).
#   3. Runs convert_to_v2_bundle.py for Stanford → FHIR Bundle on disk.
#   4. POSTs the Bundle to v2 HAPI.
#   5. Verifies the new DocumentReference count.
#
# Per P-PHI-STAYS-LOCAL: only counts + sizes emitted to stdout; no clinical
# content leaks.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DEVICE_ID="${DEVICE_ID:-D0DEEFE3-E6D0-49A9-AB02-9AD77348A958}"
APP_DOCS="$HOME/Library/Developer/CoreSimulator/Devices/$DEVICE_ID/data/Containers/Data/Application"

echo "=== Finding latest mobile batch in simulator sandbox ==="
LATEST_BATCH=$(find "$APP_DOCS" -name "stanford-batch-2*.json" -not -name "*manifest*" -type f -mtime -2 2>/dev/null | xargs -I{} stat -f "%m %N" {} | sort -rn | head -1 | cut -d' ' -f2-)
if [ -z "$LATEST_BATCH" ]; then
  echo "ERROR: no recent stanford-batch-*.json in $APP_DOCS" >&2
  echo "  Has the mobile app run a scrape batch on this simulator?" >&2
  exit 1
fi
echo "  $LATEST_BATCH ($(du -h "$LATEST_BATCH" | cut -f1))"

echo ""
echo "=== Step 1: translate mobile output → v3 notes shape ==="
python3 tools/mobile/convert_mobile_batch_to_v3_notes.py "$LATEST_BATCH"

echo ""
echo "=== Step 2: build FHIR Bundle from v3 visits + notes ==="
python3 tools/v3/convert_to_v2_bundle.py stanford 2>&1 | tail -8

echo ""
echo "=== Step 3: POST Bundle to v2 HAPI ==="
DR_BEFORE=$(curl -s "http://localhost:8090/fhir/DocumentReference?_summary=count" | python3 -c "import json,sys; print(json.load(sys.stdin).get('total',0))")
echo "  DocumentReference count before: $DR_BEFORE"

HTTP_STATUS=$(curl -s -o /tmp/ingest-resp.json -w "%{http_code}" -X POST \
  -H "Content-Type: application/fhir+json" \
  --data @tools/v3/out/stanford-v3-bundle.json \
  http://localhost:8090/fhir/)
echo "  POST status: $HTTP_STATUS"
python3 -c "
import json
with open('/tmp/ingest-resp.json') as f:
  d = json.load(f)
if d.get('resourceType') == 'OperationOutcome':
  print('  ERROR:'); print('  ' + json.dumps(d, indent=2)[:600].replace('\\n', '\\n  '))
else:
  from collections import Counter
  status_dist = Counter(e.get('response', {}).get('status', '?') for e in d.get('entry', []))
  for s, n in status_dist.most_common(): print(f'  {n:>4d}  {s}')
"

DR_AFTER=$(curl -s "http://localhost:8090/fhir/DocumentReference?_summary=count" | python3 -c "import json,sys; print(json.load(sys.stdin).get('total',0))")
echo "  DocumentReference count after:  $DR_AFTER  (delta: $((DR_AFTER - DR_BEFORE)))"

echo ""
echo "=== Done. Refresh the app to see the new clinical notes on Stanford encounters. ==="
