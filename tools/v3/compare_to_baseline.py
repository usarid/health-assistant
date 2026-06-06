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

    # Match by CSN. v3 stores it at item.Csn (from the RenderedData entry).
    # The baseline raw export was produced by the v1 scrape which wrapped the
    # API response in a `details` field; v3 stores the raw response directly.
    # We accept both for resilience.
    def csn_of_v3(entry):
        item = entry.get('item') or {}
        resp = entry.get('response') or {}
        return item.get('Csn') or resp.get('csn') or (resp.get('details') or {}).get('csn') or ''

    def csn_of_raw(v):
        return v.get('csn', '') or (v.get('details') or {}).get('csn', '') or v.get('Csn', '')

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

    # Baseline notes (ucsf_notes_full.json) don't carry the encounter CSN — they
    # only have (date, provider, visitType). Match on a tuple of those instead,
    # using the visit-summary info v3 has access to via item.response.
    def v3_key(e):
        item = e.get('item') or {}
        resp = item.get('response') or {}
        vs = resp.get('visitSummaryInfo') or {}
        inner = item.get('item') or {}
        # encounterDate format: 'Feb 19, 2026'; baseline: 'Feb 19, 2026'
        date = vs.get('encounterDate') or inner.get('Date') or ''
        provider = vs.get('provider') or inner.get('PrimaryProviderName') or ''
        # Use just date+provider — type fields format differs between v1 raw and v3 RD
        return (date.strip(), provider.strip())

    def raw_key(n):
        return ((n.get('encounterDate') or n.get('date') or '').strip(),
                (n.get('provider') or '').strip())

    # Filter to non-empty keys (both parts must be present)
    v3_keys = {k for k in (v3_key(e) for e in v3_notes) if k[0] and k[1]}
    raw_keys = {k for k in (raw_key(n) for n in raw_notes) if k[0] and k[1]}
    print()
    print(f'Match key: (encounter_date, provider)')
    print(f'  v3 keys (with both fields):       {len(v3_keys)}')
    print(f'  baseline keys (with both fields): {len(raw_keys)}')
    print(f'  intersection:    {len(v3_keys & raw_keys)}')
    print(f'  v3-only:         {len(v3_keys - raw_keys)}')
    print(f'  baseline-only:   {len(raw_keys - v3_keys)}')

    # Content presence sanity
    v3_with_content = sum(1 for e in v3_notes
                          if (e.get('response') or {}).get('reportHtml') or
                             (e.get('response') or {}).get('reportContent'))
    print(f'\nv3 notes with non-empty content: {v3_with_content}/{len(v3_notes)}')


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


def _parse_human_date(s):
    """Parse 'Feb 19, 2026' / 'Friday February 19, 2026' / '2026-02-19' to ISO date."""
    if not s:
        return None
    s = s.strip()
    # Strip leading weekday
    s = re.sub(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+', '', s)
    formats = ['%Y-%m-%d', '%b %d, %Y', '%B %d, %Y', '%m/%d/%Y', '%m/%d/%y']
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except (ValueError, TypeError):
            pass
    # Last resort: take the first 10 chars if they look like ISO
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    return None


def compare_stanford_notes(v3_path):
    """Compare v3 Stanford notes against AHR's coverage manifest. Emits only
    counts and aggregate stats — no per-item details (per P-PHI-STAYS-LOCAL)."""
    v3 = load_v3_output(v3_path)
    v3_notes = v3.get('notes') or []
    print(f'v3 Stanford notes captured: {len(v3_notes)}')

    manifest = stanford_ahr_manifest()
    print(f'AHR Stanford coverage manifest: {len(manifest)} fingerprints')

    # v3 schema: each note's `item` IS a visits-result row. The visit details
    # are at item.response (no `details` wrapper). RD entry is at item.item.
    def v3_iso_date(e):
        item = e.get('item') or {}
        resp = item.get('response') or {}
        vs = resp.get('visitSummaryInfo') or {}
        rd = item.get('item') or {}
        for raw in (vs.get('encounterDate'), rd.get('Date'), rd.get('Instant')):
            iso = _parse_human_date(raw)
            if iso:
                return iso
        return None

    v3_dates = [v3_iso_date(e) for e in v3_notes]
    v3_dates_set = {d for d in v3_dates if d}
    manifest_dates = {k[0] for k in manifest.keys() if k[0]}

    # Note that CSNs cannot be matched across these two channels — AHR uses
    # Epic's numeric internal CSN format; MyChart's API returns the encrypted
    # "WP-24" URL-safe token. They are the same encounter keyed differently.
    # Match by ISO date instead; aggregates only.
    print()
    print('Coverage by encounter ISO date (date-only match, not per-note):')
    print(f'  v3 distinct dates:        {len(v3_dates_set)}')
    print(f'  AHR manifest dates:       {len(manifest_dates)}')
    print(f'  dates in both:            {len(v3_dates_set & manifest_dates)}')
    print(f'  AHR-only dates:           {len(manifest_dates - v3_dates_set)}')
    print(f'  v3-only dates:            {len(v3_dates_set - manifest_dates)}')

    # AHR note types histogram for context
    from collections import Counter
    ahr_types = Counter(k[1] for k in manifest.keys() if k[1])
    print()
    print('AHR Stanford note types (count):')
    for t, n in ahr_types.most_common(8):
        print(f'  {n:>4d}  {t}')


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
