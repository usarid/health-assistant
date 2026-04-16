#!/bin/bash
# Reload all scraped FHIR resources with full-text narratives
HAPI_URL="${HAPI_URL:-http://localhost:8080/fhir}"

echo "Loading UCSF Encounters (241)..."
curl -s -X POST "$HAPI_URL" \
  -H "Content-Type: application/fhir+json" \
  -d @ucsf_encounters_fhir_bundle.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  Encounters: {len(d.get('entry',[]))} processed\")" 2>/dev/null || echo "  Failed"

echo "Loading UCSF Clinical Notes (108)..."
curl -s -X POST "$HAPI_URL" \
  -H "Content-Type: application/fhir+json" \
  -d @ucsf_notes_fhir_bundle.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  Notes: {len(d.get('entry',[]))} processed\")" 2>/dev/null || echo "  Failed"

echo "Loading MSKCC Messages (376)..."
curl -s -X POST "$HAPI_URL" \
  -H "Content-Type: application/fhir+json" \
  -d @mskcc_messages_fhir_bundle.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  Messages: {len(d.get('entry',[]))} processed\")" 2>/dev/null || echo "  Failed"

echo ""
echo "Verifying full-text search for 'neuropathy'..."
RESULT=$(curl -s "$HAPI_URL/DocumentReference?_text=neuropathy&_summary=count")
COUNT=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', 'unknown'))" 2>/dev/null)
echo "  DocumentReference hits for 'neuropathy': $COUNT"

echo "Done!"
