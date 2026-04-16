#!/bin/bash
FHIR_BASE="http://localhost:8080/fhir"
echo "Loading Sutter test results..."
resp=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$FHIR_BASE" \
  -H "Content-Type: application/fhir+json" \
  -d @sutter_results_fhir_batch_1.json)
echo "  sutter_results_fhir_batch_1.json: 4 resources -> HTTP $resp"
