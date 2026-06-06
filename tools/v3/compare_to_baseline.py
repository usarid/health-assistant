#!/usr/bin/env python3
"""Compare v3 scraper output against existing baselines.

Three modes:

  ucsf-visits  v3-output.json vs raw-exports/ucsf_visits_full.json
  ucsf-notes   v3-output.json vs raw-exports/ucsf_notes_full.json
  stanford-notes  v3-output.json vs AHR coverage manifest (per C-018)

For UCSF, the baselines are the existing v1 scrape outputs — the v3 run should
reproduce them (modulo provenance metadata). Material differences are either
v3 bugs or v1 bugs the v2 rebuild surfaced earlier.

For Stanford notes, there is no prior baseline (Stanford notes were never
scraped; see C-017). The comparison is against the AHR coverage manifest:
for each Stanford DocumentReference AHR knows about, does the v3 scrape have
a matching captured note? Each missing-from-scrape entry is a scraping gap.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone


RAW_DIR = Path('/Users/urisarid/usarid@gmail.com/Medical/Synthesis/health-assistant/data/raw-exports')
AHR_DIR = Path('/Users/urisarid/usarid@gmail.com/Medical/New exports/apple_health_export/clinical-records')


# ── Helpers ────────────────────────────────────────────────────────────
def load_v3_output(path):
    """v3 runtime output is {jobName: [{item, response, _http, _provenance}, ...]}"""
    with open(path) as f:
        return json.load(f)


def normalize_date(s):
    if not s:
        return None
    s = s[:10] if isinstance(s, str) else None
    return s


# ── UCSF visits comparison ─────────────────────────────────────────────
def compare_ucsf_visits(v3_path):
    v3 = load_v3_output(v3_path)
    v3_visits = v3.get('visits') or []
    print(f'v3 visits captured: {len(v3_visits)}')

    raw_path = RAW_DIR / 'ucsf_visits_full.json'
    with open(raw_path) as f:
        raw = json.load(f)
    raw_visits = raw.get('visits') or raw if isinstance(raw, list) else raw.get('visits', [])
    print(f'baseline ucsf_visits_full.json: {len(raw_visits)}')

    # Match by CSN
    def csn_of_v3(entry):
        return (entry.get('item') or {}).get('Csn', '')

    def csn_of_raw(v):
        return v.get('csn', '') or (v.get('details') or {}).get('csn', '')

    v3_csns = {csn_of_v3(e) for e in v3_visits if csn_of_v3(e)}
    raw_csns = {csn_of_raw(v) for v in raw_visits if csn_of_raw(v)}
    print()
    print(f'CSN intersection:    {len(v3_csns & raw_csns)}')
    print(f'v3-only CSNs:        {len(v3_csns - raw_csns)}')
    print(f'baseline-only CSNs:  {len(raw_csns - v3_csns)}')

    # Response shape sanity check: did v3 pull the same details?
    if v3_visits:
        e = v3_visits[0]
        resp = e.get('response') or {}
        details = resp.get('details') or {}
        keys = sorted(list(details.keys()))[:15]
        print()
        print(f'v3 first visit response.details has {len(details)} top-level keys, sample: {keys}')


# ── UCSF notes comparison ──────────────────────────────────────────────
def compare_ucsf_notes(v3_path):
    v3 = load_v3_output(v3_path)
    v3_notes = v3.get('notes') or []
    print(f'v3 notes captured: {len(v3_notes)}')

    raw_path = RAW_DIR / 'ucsf_notes_full.json'
    with open(raw_path) as f:
        raw = json.load(f)
    raw_notes = raw.get('notes') or []
    print(f'baseline ucsf_notes_full.json: {len(raw_notes)}')

    # Match by encounter CSN — but the v3 schema is different from the raw schema, so derive both
    def v3_key(e):
        item = e.get('item') or {}
        # depends_on=visits puts the prior result here
        resp = item.get('response') or {}
        details = resp.get('details') or item
        return details.get('csn') or item.get('Csn', '')

    def raw_key(n):
        return (n.get('details') or {}).get('csn') or n.get('csn', '')

    v3_keys = {v3_key(e) for e in v3_notes if v3_key(e)}
    raw_keys = {raw_key(n) for n in raw_notes if raw_key(n)}
    print()
    print(f'CSN intersection:    {len(v3_keys & raw_keys)}')
    print(f'v3-only CSNs:        {len(v3_keys - raw_keys)}')
    print(f'baseline-only CSNs:  {len(raw_keys - v3_keys)}')

    # Content presence sanity
    v3_with_content = sum(1 for e in v3_notes
                          if (e.get('response') or {}).get('reportHtml') or
                             (e.get('response') or {}).get('reportContent'))
    print(f'v3 notes with non-empty content: {v3_with_content}/{len(v3_notes)}')


# ── Stanford coverage check vs AHR manifest ────────────────────────────
def stanford_ahr_manifest():
    """Build the Stanford-portion of AHR's clinical-record coverage manifest.

    Returns a dict: fingerprint → DocumentReference summary.
    Fingerprint key per C-018: (date_iso, type_text, encounter_csn or '').
    """
    manifest = {}
    for fp in sorted(AHR_DIR.glob('DocumentReference-*.json')):
        with open(fp) as f:
            d = json.load(f)
        custodian = (d.get('custodian') or {}).get('display', '') or ''
        if 'Stanford' not in custodian:
            continue
        date = normalize_date(d.get('date', ''))
        type_text = (d.get('type') or {}).get('text', '') or ''
        csn = ''
        for enc in (d.get('context') or {}).get('encounter', []) or []:
            ident = (enc.get('identifier') or {})
            if ident.get('value'):
                csn = ident['value']
                break
        author = ''
        authors = d.get('author') or []
        if authors:
            author = (authors[0] or {}).get('display', '') or ''
        key = (date, type_text, csn)
        manifest[key] = {
            'date': date,
            'type': type_text,
            'csn': csn,
            'author': author,
            'docref_id': d.get('id'),
        }
    return manifest


def compare_stanford_notes(v3_path):
    v3 = load_v3_output(v3_path)
    v3_notes = v3.get('notes') or []
    print(f'v3 Stanford notes captured: {len(v3_notes)}')

    manifest = stanford_ahr_manifest()
    print(f'AHR Stanford coverage manifest: {len(manifest)} fingerprints')

    # Build scraped fingerprints — try several shapes since Stanford's schema
    # may differ from UCSF's; this lets us be robust to discovery surprises
    scraped = {}
    for e in v3_notes:
        item = e.get('item') or {}
        resp = e.get('response') or {}
        # CSN
        csn = (item.get('response') or {}).get('details', {}).get('csn') or \
              item.get('Csn') or \
              resp.get('csn') or ''
        # Date
        date = item.get('Date') or \
               (item.get('response') or {}).get('details', {}).get('encounterDate') or \
               normalize_date(resp.get('date', '')) or ''
        if isinstance(date, str):
            date = normalize_date(date) or date
        # Type
        ntype = item.get('VisitTypeName') or item.get('EncounterType') or ''
        key = (date, str(ntype), str(csn))
        scraped[key] = e

    print(f'v3 fingerprints emitted: {len(scraped)}')
    # Pure CSN-only check too (more lenient — captures matches even if type/date differ)
    manifest_csns = {csn for (d, t, csn) in manifest.keys() if csn}
    scraped_csns = {csn for (d, t, csn) in scraped.keys() if csn}
    print()
    print('Coverage by CSN:')
    print(f'  matched:    {len(manifest_csns & scraped_csns)}')
    print(f'  AHR-only (scraping gap):       {len(manifest_csns - scraped_csns)}')
    print(f'  scraped-only (newer than AHR): {len(scraped_csns - manifest_csns)}')

    gaps = manifest_csns - scraped_csns
    if gaps:
        # Show some examples
        print()
        print('First 10 AHR fingerprints not captured by v3 scrape:')
        shown = 0
        for (date, type_, csn), info in manifest.items():
            if csn in gaps and shown < 10:
                print(f'  {date}  {type_:30s}  csn={csn[:24]}…')
                shown += 1


# ── Main ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['ucsf-visits', 'ucsf-notes', 'stanford-notes',
                                     'stanford-visits'])
    ap.add_argument('v3_output', help='path to JSON file the v3 runtime emitted')
    args = ap.parse_args()

    print(f'=== compare_to_baseline: {args.mode} ===')
    print(f'v3 output: {args.v3_output}')
    print()

    if args.mode == 'ucsf-visits':
        compare_ucsf_visits(args.v3_output)
    elif args.mode == 'ucsf-notes':
        compare_ucsf_notes(args.v3_output)
    elif args.mode == 'stanford-notes':
        compare_stanford_notes(args.v3_output)
    elif args.mode == 'stanford-visits':
        # Stanford visits baseline (stanford_visits_raw.json) is in a different
        # shape — defer the full diff for now; just sanity-check counts
        v3 = load_v3_output(args.v3_output)
        v3_visits = v3.get('visits') or []
        print(f'v3 Stanford visits captured: {len(v3_visits)}')
        raw_path = RAW_DIR / 'stanford_visits_raw.json'
        with open(raw_path) as f:
            raw = json.load(f)
        print(f'baseline stanford_visits_raw.json: {len(raw)} entries')


if __name__ == '__main__':
    main()
