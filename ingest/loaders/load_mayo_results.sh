#!/bin/bash
# Load Mayo Clinic test results FHIR bundles into HAPI server
set -e

HAPI="http://localhost:8080/fhir"

echo "Loading Mayo Clinic test results into HAPI..."
for f in mayo_results_fhir_batch_*.json; do
  echo "  Loading $f..."
  curl -s -o /dev/null -w "  %{http_code} %{size_upload} bytes uploaded\n" \
    -X POST "$HAPI" \
    -H "Content-Type: application/fhir+json" \
    -d @"$f"
done

echo ""
echo "Verifying..."
DR_COUNT=$(curl -s "$HAPI/DiagnosticReport?_tag=http://example.org/source%7Cmayo-mychart-results&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))")
OBS_COUNT=$(curl -s "$HAPI/Observation?_tag=http://example.org/source%7Cmayo-mychart-results&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))")
echo "Mayo Clinic results in HAPI: $DR_COUNT DiagnosticReports, $OBS_COUNT Observations"
