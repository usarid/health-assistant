#!/bin/bash
# Load wearable BP observations into HAPI FHIR
# First deletes existing BP observations, then loads new batches with systolic+diastolic
cd "$(dirname "$0")"
HAPI_URL="${HAPI_URL:-http://localhost:8080/fhir}"

echo "=== Deleting existing BP observations (code 85354-9) ==="
# Delete in a loop until none remain
while true; do
  COUNT=$(curl -s "$HAPI_URL/Observation?code=http://loinc.org|85354-9&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', 0))" 2>/dev/null)
  echo "  Remaining: $COUNT"
  if [ "$COUNT" = "0" ] || [ -z "$COUNT" ]; then
    break
  fi
  # Delete a page at a time
  curl -s "$HAPI_URL/Observation?code=http://loinc.org|85354-9&_count=100" | \
    python3 -c "
import sys, json
bundle = json.load(sys.stdin)
for entry in bundle.get('entry', []):
    rid = entry.get('resource', {}).get('id')
    if rid:
        print(rid)
" | while read ID; do
    curl -s -X DELETE "$HAPI_URL/Observation/$ID" > /dev/null
  done
done
echo "  All old BP observations deleted."
echo

echo "=== Loading new BP observations (6 batches, 2618 total) ==="
for i in 0 1 2 3 4 5; do
  FILE="wearable_bp_batch_${i}.json"
  if [ -f "$FILE" ]; then
    echo "Loading BP batch $i..."
    curl -s -X POST "$HAPI_URL" \
      -H "Content-Type: application/fhir+json" \
      -d @"$FILE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'  Loaded: {len(d.get(\"entry\",[]))} observations')" 2>/dev/null || echo "  Failed"
  fi
done
echo

echo "=== Verifying ==="
BP=$(curl -s "$HAPI_URL/Observation?code=http://loinc.org|85354-9&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', '?'))" 2>/dev/null)
echo "  BP composite observations in HAPI: $BP"

# Check a sample for diastolic component
echo "  Checking sample for diastolic component..."
curl -s "$HAPI_URL/Observation?code=http://loinc.org|85354-9&_count=1" | \
  python3 -c "
import sys, json
bundle = json.load(sys.stdin)
for entry in bundle.get('entry', []):
    comps = entry['resource'].get('component', [])
    print(f'  Sample observation has {len(comps)} component(s):')
    for c in comps:
        code = c['code']['coding'][0]['display']
        val = c['valueQuantity']['value']
        print(f'    - {code}: {val} mmHg')
" 2>/dev/null

echo
echo "Done! Trigger reindex if needed:"
echo "  curl -X POST '$HAPI_URL/\$reindex'"
