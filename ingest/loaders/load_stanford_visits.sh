#!/bin/bash
# Load Stanford MyHealth visits into HAPI FHIR
cd "$(dirname "$0")"
HAPI_URL="${HAPI_URL:-http://localhost:8080/fhir}"

echo "Loading Stanford visits..."
curl -s -X POST "$HAPI_URL" \
  -H "Content-Type: application/fhir+json" \
  -H "Accept: application/fhir+json" \
  -d @stanford_visits_fhir_bundle.json | python3 -c "
import sys, json
d = json.load(sys.stdin)
entries = d.get('entry', [])
ok = sum(1 for e in entries if e.get('response',{}).get('status','').startswith('2'))
print(f'  Loaded: {ok}/{len(entries)} resources')
" 2>/dev/null || echo "  Failed"

echo
echo "Verifying Encounter count..."
TOTAL=$(curl -s -H "Accept: application/fhir+json" "$HAPI_URL/Encounter?_tag=stanford-myhealth-visits&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total','?'))" 2>/dev/null)
echo "  Stanford Encounters in HAPI: $TOTAL"

echo
echo "Done! You may want to trigger reindex:"
echo "  curl -X POST '$HAPI_URL/\$reindex'"
