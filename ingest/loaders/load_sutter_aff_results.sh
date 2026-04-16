#!/bin/bash
FHIR_BASE="http://localhost:8080/fhir"
echo "Loading Sutter Affiliates test results (2 batches)..."
for f in sutter_aff_results_fhir_batch_*.json; do
  count=$(python3 -c "import json; print(len(json.load(open('$f'))['entry']))")
  resp=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$FHIR_BASE" \
    -H "Content-Type: application/fhir+json" \
    -d @"$f")
  echo "  $f: $count resources -> HTTP $resp"
done
echo "Done!"
