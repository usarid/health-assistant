#!/bin/bash
# Load MSKCC messages FHIR bundle into HAPI FHIR server
# Usage: cd ~/Medical/Synthesis && bash load_messages_to_hapi.sh

FHIR_BASE="http://localhost:8080/fhir"
BUNDLE="mskcc_messages_fhir_bundle.json"

echo "Loading messages into HAPI FHIR at $FHIR_BASE..."

# Check HAPI is running
if ! curl -sf "$FHIR_BASE/metadata" > /dev/null 2>&1; then
    echo "ERROR: HAPI FHIR not reachable at $FHIR_BASE"
    echo "Start Docker first: docker compose up -d"
    exit 1
fi

# Count resources
COUNT=$(python3 -c "import json; d=json.load(open('$BUNDLE')); print(len(d['entry']))")
echo "Bundle has $COUNT resources to load"

# Load each resource individually (more reliable than transaction bundle)
python3 -c "
import json, urllib.request, urllib.error, sys

with open('$BUNDLE') as f:
    bundle = json.load(f)

entries = bundle['entry']
success = 0
errors = 0

for i, entry in enumerate(entries):
    resource = entry['resource']
    rt = resource['resourceType']
    try:
        data = json.dumps(resource).encode('utf-8')
        req = urllib.request.Request(
            '$FHIR_BASE/' + rt,
            data=data, method='POST',
            headers={'Content-Type': 'application/fhir+json', 'Accept': 'application/fhir+json'}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        success += 1
    except urllib.error.HTTPError as e:
        errors += 1
        if errors <= 5:
            print(f'  Error #{i+1}: HTTP {e.code}', file=sys.stderr)
    except Exception as e:
        errors += 1

    if (i + 1) % 50 == 0:
        print(f'  [{i+1}/{len(entries)}] success:{success} errors:{errors}')

print(f'')
print(f'=== Done ===')
print(f'Loaded: {success}')
print(f'Errors: {errors}')
"
