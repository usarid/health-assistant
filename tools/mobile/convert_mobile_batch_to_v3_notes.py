#!/usr/bin/env python3
"""Translate a mobile-app stanford-batch-*.json into the v3 notes file shape
that tools/v3/convert_to_v2_bundle.py already knows how to consume.

The mobile output has:
  { 'captured': [{csn, html, htmlLength, visibleTextLength, capturedAt}, ...],
    'errors':   [...] }

The v3 notes file expected by convert_to_v2_bundle.py has:
  { 'notes': [{ 'item':     <the visits-result row, ie. {item: <RD entry>, response: <visit details>}>,
                'response': { 'reportContent': <HTML>, ... },
                '_http', '_provenance' },
              ...] }

This script joins on CSN: for each mobile-captured note, find the matching
visit in tools/v3/out/stanford-v3-visits.json and emit a v3-notes entry
whose response.reportContent is the mobile-captured HTML (much better
content than the original v3 scrape's 755-char skeletons — see C-021).

After running this, the existing pipeline:
    python3 tools/v3/convert_to_v2_bundle.py stanford
produces a FHIR Bundle ready to POST to v2 HAPI.

Per P-PHI-STAYS-LOCAL: only counts emitted.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
V3_OUT = REPO_ROOT / 'tools' / 'v3' / 'out'
VISITS_FILE = V3_OUT / 'stanford-v3-visits.json'
NOTES_FILE = V3_OUT / 'stanford-v3-notes.json'


def main():
    if len(sys.argv) != 2:
        print('usage: convert_mobile_batch_to_v3_notes.py <stanford-batch-*.json>', file=sys.stderr)
        sys.exit(1)

    batch_path = Path(sys.argv[1])
    if not batch_path.exists():
        print(f'ERROR: {batch_path} not found', file=sys.stderr)
        sys.exit(1)

    print(f'Loading mobile batch:  {batch_path}')
    with open(batch_path) as f:
        batch = json.load(f)
    captured = batch.get('captured', [])
    print(f'  mobile captured notes:  {len(captured)}')

    if not VISITS_FILE.exists():
        print(f'ERROR: {VISITS_FILE} not found — needed for visit metadata lookup', file=sys.stderr)
        sys.exit(1)
    print(f'Loading v3 visits:     {VISITS_FILE}')
    with open(VISITS_FILE) as f:
        visits_data = json.load(f)
    visits = visits_data.get('visits', [])

    # Index visits by CSN for join
    visit_by_csn = {}
    for v in visits:
        item = v.get('item') or {}
        resp = v.get('response') or {}
        csn = item.get('Csn') or resp.get('csn')
        if csn:
            visit_by_csn[csn] = v
    print(f'  visits indexed by CSN:  {len(visit_by_csn)}')

    # Back up the existing notes file (so we can recover the old shape if needed)
    if NOTES_FILE.exists():
        backup = NOTES_FILE.with_suffix('.json.pre-mobile-bak')
        if not backup.exists():
            backup.write_bytes(NOTES_FILE.read_bytes())
            print(f'  backed up existing notes file to {backup.name}')

    # Build new v3-format notes entries from the mobile output
    new_notes = []
    join_misses = 0
    for c in captured:
        csn = c['csn']
        html = c.get('html', '')
        if not html:
            continue
        visit = visit_by_csn.get(csn)
        if visit is None:
            join_misses += 1
            # Still emit; convert_to_v2_bundle.py extracts CSN from response too
            visit = {'item': {'Csn': csn}, 'response': {'csn': csn}}
        new_notes.append({
            'item': visit,
            'response': {
                'reportContent': html,
                'reportCss': '',
                'baseFontSize': 0,
                'stylesheets': [],
            },
            '_http': {'status': 200, 'ok': True},
            '_provenance': {
                'source': 'mobile-flutter-prototype',
                'scraped_at': c.get('capturedAt'),
                'portal_id': 'stanford.mychart',
                'config_version': 'mobile-iter-2',
                'job': 'notes',
                'endpoint': 'after-visit-summary-page-extracted-pgSection',
                'mobileHtmlLength': c.get('htmlLength'),
                'mobileVisibleTextLength': c.get('visibleTextLength'),
            },
        })

    if join_misses:
        print(f'  WARNING: {join_misses} mobile-captured CSNs not found in v3 visits — emitting with minimal metadata')

    out_payload = {'notes': new_notes}
    with open(NOTES_FILE, 'w') as f:
        json.dump(out_payload, f)
    print(f'Wrote: {NOTES_FILE}')
    print(f'  notes in file:   {len(new_notes)}')
    print(f'  file size:       {NOTES_FILE.stat().st_size / 1024 / 1024:.1f} MB')

    # Size sanity (no content)
    sizes = sorted([len(n['response']['reportContent']) for n in new_notes])
    if sizes:
        print(f'  HTML size median: {sizes[len(sizes)//2]:>7d} bytes')
        print(f'  HTML size max:    {sizes[-1]:>7d} bytes')


if __name__ == '__main__':
    main()
