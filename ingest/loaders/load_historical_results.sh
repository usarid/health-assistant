#!/bin/bash
# Load historical PDF-sourced test results into HAPI server
set -e

HAPI="http://localhost:8080/fhir"

echo "Loading historical test results into HAPI..."
for f in historical_results_fhir_batch_*.json; do
  echo "  Loading $f..."
  curl -s -o /dev/null -w "  %{http_code} %{size_upload} bytes uploaded\n" \
    -X POST "$HAPI" \
    -H "Content-Type: application/fhir+json" \
    -d @"$f"
done

echo ""
echo "Verifying (fetching sample entries to confirm)..."
curl -s "$HAPI/DiagnosticReport?_tag=http://example.org/source%7Chistorical-pdf-results&_count=5" | python3 -c "
import sys, json
b = json.load(sys.stdin)
entries = b.get('entry', [])
print(f'DiagnosticReports found: {len(entries)}+')
for e in entries:
    r = e['resource']
    print(f'  {r[\"code\"][\"text\"][:60]} ({r.get(\"effectiveDateTime\",\"\")[:10]})')
"

echo ""
echo "Spot checks by code:text..."
for term in "Stool" "SIBO" "VCS" "Cervical Spine" "Celiac"; do
  count=$(curl -s "$HAPI/DiagnosticReport?code:text=$term&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))")
  echo "  '$term': $count DiagnosticReport(s)"
done
