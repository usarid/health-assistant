#!/usr/bin/env python3
"""Classify AHR-only gap dates against the v3 scrape's visit roster.

For each AHR-known clinical note date that v3 didn't capture, decide why:
  A. no_visit_in_v3       — no visit on that date in MyChart at all
  B. visit_not_local      — visit exists but IsLocal=false (our filter)
  C. no_shareable_note    — visit is local but notesInfo unshareable
  D. note_captured_filter — shouldn't happen; sanity check

Emits ONLY aggregate counts. Per P-PHI-STAYS-LOCAL.

Usage:
  python3 tools/v3/investigate_gap.py stanford
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


V3_DIR = Path(__file__).resolve().parent / 'out'
AHR_DIR = Path('/Users/urisarid/usarid@gmail.com/Medical/New exports/apple_health_export/clinical-records')


def parse_human_date(s):
    if not s:
        return None
    s = s.strip()
    s = re.sub(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+', '', s)
    for fmt in ('%Y-%m-%d', '%b %d, %Y', '%B %d, %Y', '%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except (ValueError, TypeError):
            pass
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    return None


def visit_iso_date(v):
    """Return the ISO date of a v3 visit-result row, trying a few sources."""
    item = v.get('item') or {}
    resp = v.get('response') or {}
    vs = resp.get('visitSummaryInfo') or {}
    rd = item if isinstance(item, dict) and 'IsLocal' in item else (item.get('item') or {})
    for raw in (vs.get('encounterDate'), rd.get('Date'), rd.get('Instant')):
        iso = parse_human_date(raw)
        if iso:
            return iso
    return None


def visit_status(v):
    """Return a tuple: (is_local, has_shareable_note)."""
    item = v.get('item') or {}
    rd = item if 'IsLocal' in item else (item.get('item') or {})
    is_local = bool(rd.get('IsLocal'))
    ni = (v.get('response') or {}).get('notesInfo') or {}
    shareable = bool(ni.get('isAtLeastOneNoteShareable')
                     and (ni.get('notesReport') or {}).get('reportID'))
    return is_local, shareable


def ahr_manifest(institution_match):
    """Return {date_iso: [type, ...]} for the matching institution."""
    by_date = defaultdict(list)
    for fp in sorted(AHR_DIR.glob('DocumentReference-*.json')):
        with open(fp) as f:
            d = json.load(f)
        custodian = (d.get('custodian') or {}).get('display', '') or ''
        if institution_match not in custodian:
            continue
        raw_date = d.get('date', '')
        iso = parse_human_date(raw_date) or (raw_date[:10] if raw_date else None)
        if not iso:
            continue
        type_text = (d.get('type') or {}).get('text', '') or '(no type)'
        by_date[iso].append(type_text)
    return dict(by_date)


def classify_gap_date(date, all_visits):
    """For an AHR-only date, find any v3 visit on that date and classify."""
    on_date = [v for v in all_visits if visit_iso_date(v) == date]
    if not on_date:
        return 'A. no_visit_in_v3'
    locals_ = [v for v in on_date if visit_status(v)[0]]
    if not locals_:
        return 'B. visit_not_local'
    shareable = [v for v in locals_ if visit_status(v)[1]]
    if not shareable:
        return 'C. local_visit_no_shareable_note'
    return 'D. shareable_note_not_captured_BUG'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('portal', choices=['stanford', 'ucsf'])
    args = ap.parse_args()

    portal_name = {'stanford': 'Stanford', 'ucsf': 'UCSF'}[args.portal]
    v3_file = V3_DIR / f'{args.portal}-v3-visits.json'

    visits = json.load(open(v3_file))['visits']
    v3_dates = {visit_iso_date(v) for v in visits if visit_iso_date(v)}

    manifest_by_date = ahr_manifest(portal_name)
    manifest_dates = set(manifest_by_date.keys())

    ahr_only = manifest_dates - v3_dates
    print(f'=== {portal_name} gap investigation ===')
    print(f'v3 visits in roster:         {len(visits)}')
    print(f'v3 distinct ISO dates:       {len(v3_dates)}')
    print(f'AHR manifest dates:          {len(manifest_dates)}')
    print(f'AHR-only dates to classify:  {len(ahr_only)}')
    print()

    # Classify each gap date
    cats = Counter()
    for d in ahr_only:
        cats[classify_gap_date(d, visits)] += 1

    print('Classification of AHR-only dates:')
    for cat, n in sorted(cats.items()):
        print(f'  {n:>3d}  {cat}')
    print()

    # Note-type histogram across the gap dates
    type_counts = Counter()
    for d in ahr_only:
        for t in manifest_by_date[d]:
            type_counts[t] += 1
    print('AHR note types across gap dates:')
    for t, n in type_counts.most_common():
        print(f'  {n:>3d}  {t}')


if __name__ == '__main__':
    main()
