#!/usr/bin/env python3
"""Build the lab-orders discovery file for the mobile per-result HTML
fetch batch.

Reads every Stanford DiagnosticReport already in v2 HAPI that carries
an identifier in `urn:stanford:myhealth:order` (these are the eorderids
captured during the original v3 scrape — currently 172 records with
metadata but no body content). Writes a JSON file in the same shape as
stanford-v3-visits.json so the mobile app can read it via
rootBundle.loadString.

Run this before `scripts/sync-mobile-assets.sh` and before triggering
the mobile app's "Fetch all lab bodies" menu action.

Per P-PHI-STAYS-LOCAL: this script reads from localhost HAPI and writes
to local disk. Nothing leaves the host. Only counts emitted.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'tools'))
from portal_registry import get_portal  # noqa: E402

V3_OUT = REPO_ROOT / 'tools' / 'v3' / 'out'
HAPI_BASE = 'http://localhost:8090/fhir'


def hapi_get(path):
    url = f'{HAPI_BASE}{path}'
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--portal', default='stanford',
                    help='Portal id from mobile/assets/portals/*.json (default: stanford)')
    args = ap.parse_args()
    portal = get_portal(args.portal)
    IDENT_SYSTEM = portal.identifier_system('order')
    OUT_FILE = V3_OUT / f'{portal.id}-lab-orders.json'
    print(f'Portal: {portal.name} ({portal.id})')
    print(f'Querying HAPI for DiagnosticReports with {IDENT_SYSTEM} identifiers…')
    sys_param = urllib.parse.quote(f'{IDENT_SYSTEM}|', safe=':')
    rows = []
    seen_ids = set()
    next_url = (f'/DiagnosticReport?identifier={sys_param}'
                f'&_elements=id,identifier,code,effectiveDateTime,issued,status'
                f'&_count=200')
    while next_url:
        bundle = hapi_get(next_url)
        for e in bundle.get('entry', []) or []:
            r = e.get('resource') or {}
            fhir_id = r.get('id')
            if not fhir_id or fhir_id in seen_ids:
                continue
            seen_ids.add(fhir_id)
            eorderid = next(
                (i.get('value') for i in (r.get('identifier') or [])
                 if i.get('system') == IDENT_SYSTEM),
                None)
            if not eorderid:
                continue
            rows.append({
                'fhirId': fhir_id,
                'eorderid': eorderid,
                'code': (r.get('code') or {}).get('text', ''),
                'effective': r.get('effectiveDateTime') or r.get('issued') or '',
                'status': r.get('status', ''),
            })
        next_url = None
        for link in bundle.get('link', []) or []:
            if link.get('relation') == 'next' and link.get('url'):
                u = link['url']
                if u.startswith(HAPI_BASE):
                    next_url = u[len(HAPI_BASE):]
                break

    print(f'  found: {len(rows)} lab orders')

    payload = {
        'portal': portal.id,
        'sourceQuery': f'DiagnosticReport?identifier={IDENT_SYSTEM}|',
        'count': len(rows),
        'orders': rows,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, 'w') as f:
        json.dump(payload, f)
    size_kb = OUT_FILE.stat().st_size / 1024
    print(f'Wrote: {OUT_FILE} ({size_kb:.1f} KB)')

    # Quick status sanity (counts only, no PHI)
    from collections import Counter
    status_dist = Counter(r['status'] for r in rows)
    print(f'  status distribution: {dict(status_dist)}')


if __name__ == '__main__':
    main()
