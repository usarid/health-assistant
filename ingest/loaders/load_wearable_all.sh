#!/bin/bash
# Load all wearable observations (non-BP) into HAPI FHIR
# BP was already loaded via load_wearable_bp.sh
# This loads: Heart Rate, Resting HR, HRV, Steps, BMI, Sleep, Respiratory Rate, SpO2
cd "$(dirname "$0")"
HAPI_URL="${HAPI_URL:-http://localhost:8080/fhir}"

echo "=== Loading wearable observations (42 batches, 20,521 total) ==="
echo "  (Heart Rate, Resting HR, HRV, Steps, BMI, Sleep, Respiratory Rate, SpO2)"
echo

LOADED=0
FAILED=0
for i in $(seq 0 41); do
  FILE="wearable_other_batch_${i}.json"
  if [ -f "$FILE" ]; then
    COUNT=$(python3 -c "import json; print(len(json.load(open('$FILE'))['entry']))")
    echo -n "Batch $i ($COUNT entries)... "
    RESULT=$(curl -s -X POST "$HAPI_URL" \
      -H "Content-Type: application/fhir+json" \
      -H "Accept: application/fhir+json" \
      -d @"$FILE" 2>&1)
    OK=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('entry',[])))" 2>/dev/null)
    if [ -n "$OK" ] && [ "$OK" -gt 0 ]; then
      echo "OK ($OK)"
      LOADED=$((LOADED + OK))
    else
      echo "FAILED"
      FAILED=$((FAILED + 1))
    fi
  fi
done

echo
echo "=== Summary ==="
echo "  Loaded: $LOADED observations"
echo "  Failed batches: $FAILED"
echo

echo "=== Verifying counts ==="
for CODE_DISPLAY in \
  "8867-4:Heart Rate" \
  "40443-4:Resting HR" \
  "80404-7:HRV" \
  "55423-8:Steps" \
  "39156-5:BMI" \
  "93832-4:Sleep" \
  "9279-1:Respiratory Rate" \
  "59408-5:SpO2"; do
  CODE="${CODE_DISPLAY%%:*}"
  DISPLAY="${CODE_DISPLAY#*:}"
  N=$(curl -s -H "Accept: application/fhir+json" "$HAPI_URL/Observation?code=http://loinc.org|$CODE&_summary=count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total','?'))" 2>/dev/null)
  printf "  %-20s %s\n" "$DISPLAY" "$N"
done

echo
echo "Done! You may want to trigger reindex:"
echo "  curl -X POST '$HAPI_URL/\$reindex'"
