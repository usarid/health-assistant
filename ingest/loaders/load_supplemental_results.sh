#!/bin/bash
# Load supplemental PDF-sourced test results into HAPI server
set -e

HAPI="http://localhost:8080/fhir"

echo "Loading supplemental test results into HAPI..."
for f in supplemental_results_fhir_batch_*.json; do
  echo "  Loading $f..."
  curl -s -o /dev/null -w "  %{http_code} %{size_upload} bytes uploaded\n" \
    -X POST "$HAPI" \
    -H "Content-Type: application/fhir+json" \
    -d @"$f"
done

echo ""
echo "Verifying (fetching 2 entries to confirm)..."
curl -s "$HAPI/DiagnosticReport?_tag=http://example.org/source%7Csupplemental-pdf-results&_count=5" | python3 -c "
import sys, json
b = json.load(sys.stdin)
entries = b.get('entry', [])
print(f'DiagnosticReports found: {len(entries)}+')
for e in entries:
    r = e['resource']
    print(f'  {r[\"code\"][\"text\"][:60]} ({r.get(\"effectiveDateTime\",\"\")[:10]})')
"
