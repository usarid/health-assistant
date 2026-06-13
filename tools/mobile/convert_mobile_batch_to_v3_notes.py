#!/usr/bin/env python3
"""Translate a mobile-app stanford-batch-*.json into the v3 notes file shape
that tools/v3/convert_to_v2_bundle.py already knows how to consume.

The mobile output has:
  { 'captured': [
      # single-note visit (inline render):
      { 'csn', 'html', 'htmlLength', 'visibleTextLength', 'capturedAt' },
      # multi-note visit (list view — each VIEW NOTE click captured separately):
      { 'csn', 'html': '', 'htmlLength', 'visibleTextLength', 'capturedAt',
        'notes': [{'label', 'html', 'htmlLength', 'visibleTextLength'}, ...] },
    ],
    'errors':   [...] }

The v3 notes file expected by convert_to_v2_bundle.py has:
  { 'notes': [{ 'item':     <visits-result row>,
                'response': { 'reportContent': <HTML>, ... },
                '_http', '_provenance' },
              ...] }

This script joins on CSN to find each visit's metadata, then:
  - Single-note CSNs → 1 v3-notes entry as before
  - Multi-note CSNs  → N v3-notes entries, each with the SAME visit metadata
                       but a distinct '_provenance.subIndex' so convert_to_v2_bundle
                       derives a unique DocumentReference rid per sub-note.

Per P-PHI-STAYS-LOCAL: only counts emitted.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
V3_OUT = REPO_ROOT / 'tools' / 'v3' / 'out'
VISITS_FILE = V3_OUT / 'stanford-v3-visits.json'
NOTES_FILE = V3_OUT / 'stanford-v3-notes.json'


def make_v3_entry(visit, html, captured_at, sub_index=None, label=None,
                  html_length=None, visible_text_length=None):
    """Build one v3-notes entry. If sub_index is set, the entry represents
    one row from a multi-note (list-view) visit; convert_to_v2_bundle uses
    subIndex to derive a unique DocumentReference rid for it."""
    prov = {
        'source': 'mobile-flutter-prototype',
        'scraped_at': captured_at,
        'portal_id': 'stanford.mychart',
        'config_version': 'mobile-iter-2',
        'job': 'notes',
        'endpoint': 'after-visit-summary-page-extracted-pgSection',
    }
    if html_length is not None:
        prov['mobileHtmlLength'] = html_length
    if visible_text_length is not None:
        prov['mobileVisibleTextLength'] = visible_text_length
    if sub_index is not None:
        prov['subIndex'] = sub_index
    if label:
        prov['noteLabel'] = label
    return {
        'item': visit,
        'response': {
            'reportContent': html,
            'reportCss': '',
            'baseFontSize': 0,
            'stylesheets': [],
        },
        '_http': {'status': 200, 'ok': True},
        '_provenance': prov,
    }


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
    print(f'  mobile captured CSNs:  {len(captured)}')

    if not VISITS_FILE.exists():
        print(f'ERROR: {VISITS_FILE} not found — needed for visit metadata lookup', file=sys.stderr)
        sys.exit(1)
    print(f'Loading v3 visits:     {VISITS_FILE}')
    with open(VISITS_FILE) as f:
        visits_data = json.load(f)
    visits = visits_data.get('visits', [])

    visit_by_csn = {}
    for v in visits:
        item = v.get('item') or {}
        resp = v.get('response') or {}
        csn = item.get('Csn') or resp.get('csn')
        if csn:
            visit_by_csn[csn] = v
    print(f'  visits indexed by CSN:  {len(visit_by_csn)}')

    if NOTES_FILE.exists():
        backup = NOTES_FILE.with_suffix('.json.pre-mobile-bak')
        if not backup.exists():
            backup.write_bytes(NOTES_FILE.read_bytes())
            print(f'  backed up existing notes file to {backup.name}')

    new_notes = []
    single_count = 0
    multi_csn_count = 0
    multi_sub_count = 0
    join_misses = 0
    for c in captured:
        csn = c['csn']
        captured_at = c.get('capturedAt')
        visit = visit_by_csn.get(csn)
        if visit is None:
            join_misses += 1
            visit = {'item': {'Csn': csn}, 'response': {'csn': csn}}

        sub_notes = c.get('notes')
        if isinstance(sub_notes, list) and sub_notes:
            # Multi-note CSN: emit one v3 entry per sub-note
            multi_csn_count += 1
            for idx, sn in enumerate(sub_notes):
                sn_html = sn.get('html', '')
                if not sn_html:
                    continue
                new_notes.append(make_v3_entry(
                    visit, sn_html, captured_at,
                    sub_index=idx,
                    label=sn.get('label'),
                    html_length=sn.get('htmlLength'),
                    visible_text_length=sn.get('visibleTextLength'),
                ))
                multi_sub_count += 1
        else:
            # Single-note CSN (existing path)
            html = c.get('html', '')
            if not html:
                continue
            new_notes.append(make_v3_entry(
                visit, html, captured_at,
                html_length=c.get('htmlLength'),
                visible_text_length=c.get('visibleTextLength'),
            ))
            single_count += 1

    if join_misses:
        print(f'  WARNING: {join_misses} mobile-captured CSNs not found in v3 visits — emitting with minimal metadata')

    out_payload = {'notes': new_notes}
    with open(NOTES_FILE, 'w') as f:
        json.dump(out_payload, f)
    print(f'Wrote: {NOTES_FILE}')
    print(f'  single-note CSNs:     {single_count}')
    print(f'  multi-note CSNs:      {multi_csn_count} → {multi_sub_count} sub-notes')
    print(f'  total v3 entries:     {len(new_notes)}')
    print(f'  file size:            {NOTES_FILE.stat().st_size / 1024 / 1024:.1f} MB')

    sizes = sorted([len(n['response']['reportContent']) for n in new_notes])
    if sizes:
        print(f'  HTML size median:     {sizes[len(sizes)//2]:>7d} bytes')
        print(f'  HTML size max:        {sizes[-1]:>7d} bytes')


if __name__ == '__main__':
    main()
