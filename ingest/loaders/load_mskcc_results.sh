#!/bin/bash
FHIR_BASE="http://localhost:8080/fhir"
echo "Loading MSKCC test results..."
for f in mskcc_results_fhir_batch_*.json; do
  count=$(python3 -c "import json; print(len(json.load(open('$f'))['entry']))")
  resp=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$FHIR_BASE" \
    -H "Content-Type: application/fhir+json" \
    -d @"$f")
  echo "  $f: $count resources -> HTTP $resp"
done

echo ""
echo "Verifying counts..."
dr=$(curl -s "$FHIR_BASE/DiagnosticReport?_tag=mskcc-mychart-results&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))")
obs=$(curl -s "$FHIR_BASE/Observation?_tag=mskcc-mychart-results&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))")
echo "  MSKCC DiagnosticReports: $dr"
echo "  MSKCC Observations: $obs"
