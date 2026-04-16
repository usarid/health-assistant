#!/bin/bash
# Load Stanford MyHealth test results into HAPI FHIR
cd "$(dirname "$0")"
HAPI_URL="${HAPI_URL:-http://localhost:8080/fhir}"

echo "Loading Stanford test results (9 batches)..."
for i in $(seq 1 9); do
  FILE="stanford_results_fhir_batch_${i}.json"
  if [ ! -f "$FILE" ]; then continue; fi
  RESULT=$(curl -s -X POST "$HAPI_URL" \
    -H "Content-Type: application/fhir+json" \
    -H "Accept: application/fhir+json" \
    -d @"$FILE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
entries = d.get('entry', [])
ok = sum(1 for e in entries if e.get('response',{}).get('status','').startswith('2'))
print(f'{ok}/{len(entries)}')
" 2>/dev/null)
  echo "  Batch $i: $RESULT"
done

echo
echo "Verifying counts..."
for TYPE in DiagnosticReport Observation; do
  TOTAL=$(curl -s -H "Accept: application/fhir+json" \
    "$HAPI_URL/${TYPE}?_tag=stanford-myhealth-results&_summary=count" | \
    python3 -c "import sys,json; print(json.load(sys.stdin).get('total','?'))" 2>/dev/null)
  echo "  Stanford ${TYPE}s: $TOTAL"
done

echo
echo "Triggering reindex..."
curl -s -X POST "$HAPI_URL/\$reindex" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  Job:', d.get('parameter',[{}])[0].get('valueString','?'))" 2>/dev/null
echo
echo "Done!"
