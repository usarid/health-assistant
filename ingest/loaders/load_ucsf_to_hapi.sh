#!/bin/bash
# Load UCSF visits and notes into HAPI FHIR
HAPI_URL="${HAPI_URL:-http://localhost:8080/fhir}"

echo "Loading UCSF Encounters..."
curl -s -X POST "$HAPI_URL" \
  -H "Content-Type: application/fhir+json" \
  -d @ucsf_encounters_fhir_bundle.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"Encounters: {len(d.get('entry',[]))} processed\")" 2>/dev/null || echo "Failed to load encounters"

echo "Loading UCSF Clinical Notes..."
curl -s -X POST "$HAPI_URL" \
  -H "Content-Type: application/fhir+json" \
  -d @ucsf_notes_fhir_bundle.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"Notes: {len(d.get('entry',[]))} processed\")" 2>/dev/null || echo "Failed to load notes"

echo "Done!"
