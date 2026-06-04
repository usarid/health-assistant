#!/usr/bin/env python3
"""v2 UCSF notes converter — rebuilds FHIR DocumentReference resources from raw scrape.

Per P-STRUCTURED-FIRST: the input is structured JSON from Epic's report-content
API. No regex parsing of text blobs required. The HTML body is preserved as-is
in the DocumentReference content (base64-encoded text/html), enabling future
H-005 entity-linking work against the original document structure rather than
a lossy plaintext rendering.

Addresses CONCLUSIONS_LOG.md:
  C-002: preserves source provenance via meta.tag (org, scraper-version,
         converter-version, source-file)
  C-007: per-note grain matches v1 (one DocumentReference per scraped report)
  C-008: source-org tag = "UCSF" for native UCSF scrape; departments such as
         "MarinHealth Cardiovascular Medicine - A UCSF Health Clinic" are
         captured via the type/title but not separately tagged (MarinHealth
         content lives inside UCSF's MyChart organizationId per C-008)
  P-DATA-IS-GOLD: HTML report content preserved as text/html, not stripped

Note on encounter linkage:
  v1 DocumentReferences carry `context.encounter` references built during v1's
  visits ingestion. v2 doesn't have its own encounter resources yet, so this
  initial v2 notes pass produces DocumentReferences WITHOUT context.encounter.
  When v2 encounters are built (next step in Task #11), encounter references
  will be added via a backfill pass using deterministic IDs derived from the
  same fields (CSN, visit date) used by the visits converter.

Outputs a transaction Bundle for loading into the v2 HAPI on port 8090.
"""

import json
import re
import hashlib
import base64
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'lib'))
from fhir_utils import strip_html  # noqa: E402


# ── Constants ──────────────────────────────────────────────────────────
RAW_DIR = Path('/Users/urisarid/usarid@gmail.com/Medical/Synthesis/health-assistant/data/raw-exports')
RAW_FILE = RAW_DIR / 'ucsf_notes_full.json'
OUT_DIR = Path(__file__).resolve().parent / 'out'

CONVERTER_VERSION = 'v2.0.0'
SCRAPER_VERSION_UCSF_NOTES = 'ucsf-notes-2026-04'

# Provenance namespaces (consistent with convert_messages.py)
NS_SRC_PORTAL    = 'urn:bina:source-portal'
NS_SRC_ORG       = 'urn:bina:source-org'
NS_SRC_FILE      = 'urn:bina:source-file'
NS_SCRAPER_VER   = 'urn:bina:scraper-version'
NS_CONVERTER_VER = 'urn:bina:converter-version'

# Identifier namespaces
NS_PORTAL_NOTE_UCSF = 'urn:bina:portal:ucsf:note'


# ── Helpers ────────────────────────────────────────────────────────────
def det_id(prefix, *parts):
    """Deterministic 12-char hash ID — same scheme as convert_messages.py."""
    raw = '|'.join(str(p) for p in parts if p)
    h = hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


# Date parsing for the various formats present in raw notes.
# Examples from raw: "February 19 2026", "Feb 19, 2026", "Feb 19, 2026 11:51 AM"
_DATE_FORMATS = (
    '%B %d %Y',                 # "February 19 2026"
    '%B %d, %Y',                # "February 19, 2026"
    '%b %d, %Y',                # "Feb 19, 2026"
    '%b %d %Y',                 # "Feb 19 2026"
    '%b %d, %Y %I:%M %p',       # "Feb 19, 2026 11:51 AM"
    '%B %d, %Y %I:%M %p',       # "February 19, 2026 11:51 AM"
    '%m/%d/%Y',                 # "2/19/2026"
)

def parse_note_date(date_str):
    if not date_str:
        return None
    s = date_str.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%dT00:00:00Z')
        except ValueError:
            continue
    return None


# ── Conversion ─────────────────────────────────────────────────────────
def convert_note(n):
    """One scraped UCSF note → one FHIR DocumentReference."""
    visit_index = n.get('visitIndex')
    visit_type = (n.get('visitType') or '').strip()
    provider = (n.get('provider') or '').strip()
    department = (n.get('department') or '').strip()
    date_display = n.get('date') or n.get('encounterDate') or ''

    # Prefer encounterDate (more precise format), fall back to date
    date_iso = parse_note_date(n.get('encounterDate')) or parse_note_date(n.get('date'))

    note_content = n.get('noteContent') or {}
    report_html = note_content.get('reportContent', '') or ''

    # Deterministic ID — visit_index is a stable Epic-side index for this scrape;
    # if it's absent we fall back to a hash of visible metadata
    rid = det_id('docref-ucsf', visit_index, visit_type, date_display, provider)

    # Content: preserve HTML per P-STRUCTURED-FIRST.
    # Also produce a plaintext snippet in the description for searchability;
    # the full text is in the HTML attachment.
    content_entries = []
    if report_html:
        content_entries.append({
            'attachment': {
                'contentType': 'text/html',
                'data': base64.b64encode(report_html.encode('utf-8')).decode('ascii'),
                'title': f'{visit_type} - {date_display}' if visit_type else date_display,
            }
        })

    # Type text — preserve v1's "Clinical Note" base type, append visit type for granularity
    type_text = 'Clinical Note'
    if visit_type:
        type_text = f'Clinical Note - {visit_type}'

    # Tags (provenance contract)
    tags = [
        {'system': NS_SRC_PORTAL,    'code': 'ucsf.mychart'},
        {'system': NS_SRC_ORG,       'code': 'UCSF'},
        {'system': NS_CONVERTER_VER, 'code': CONVERTER_VERSION},
        {'system': NS_SCRAPER_VER,   'code': SCRAPER_VERSION_UCSF_NOTES},
        {'system': NS_SRC_FILE,      'code': RAW_FILE.name},
    ]

    # Identifiers
    identifiers = []
    if visit_index is not None:
        identifiers.append({
            'system': NS_PORTAL_NOTE_UCSF,
            'value': f'visitIndex={visit_index}',
        })

    docref = {
        'resourceType': 'DocumentReference',
        'id': rid,
        'status': 'current',
        'type': {'text': type_text},
        'meta': {'tag': tags},
    }
    if identifiers:
        docref['identifier'] = identifiers
    if date_iso:
        docref['date'] = date_iso
    if provider:
        docref['author'] = [{'display': provider}]
    if department:
        # Custodian carries the practice/clinic. For MarinHealth-via-UCSF notes
        # this surfaces the affiliation without breaking the source-org=UCSF tag.
        docref['custodian'] = {'display': department}
    if content_entries:
        docref['content'] = content_entries

    # Plaintext description for search (a snippet of the body, not the full text)
    if report_html:
        plain = strip_html(report_html)
        docref['description'] = plain[:200]

    return docref


# ── Main ───────────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(exist_ok=True)

    print(f'Loading {RAW_FILE}')
    with open(RAW_FILE) as f:
        raw = json.load(f)
    notes = raw.get('notes', [])
    print(f'  notes in raw: {len(notes)}')

    print()
    print('=== Converting ===')
    docrefs = []
    skipped = 0
    for n in notes:
        if n.get('error') or not (n.get('noteContent') or {}).get('reportContent'):
            skipped += 1
            continue
        docrefs.append(convert_note(n))
    print(f'  converted: {len(docrefs)}')
    print(f'  skipped (errored or empty): {skipped}')

    # Summary by visit type
    by_type = Counter()
    by_dept = Counter()
    for n in notes:
        by_type[(n.get('visitType') or '(unknown)')] += 1
        by_dept[(n.get('department') or '(unknown)')] += 1
    print()
    print('Top visit types:')
    for vt, c in by_type.most_common(8):
        print(f'  {c:>3d}  {vt!r}')
    print()
    print('Top departments (cross-institution surfacing per C-008):')
    for d, c in by_dept.most_common(8):
        print(f'  {c:>3d}  {d[:70]!r}')

    # Build Bundle
    bundle = {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': [
            {'resource': d, 'request': {'method': 'PUT', 'url': f'DocumentReference/{d["id"]}'}}
            for d in docrefs
        ],
    }

    out_file = OUT_DIR / 'notes_v2_bundle.json'
    with open(out_file, 'w') as f:
        json.dump(bundle, f, indent=2)

    print()
    print(f'Wrote: {out_file}  ({out_file.stat().st_size / 1024:.0f} KB, {len(docrefs)} entries)')

    # Sample IDs
    print()
    print('Sample IDs (first 3):')
    for d in docrefs[:3]:
        att_size = 0
        if d.get('content') and d['content'][0].get('attachment',{}).get('data'):
            att_size = len(d['content'][0]['attachment']['data'])
        print(f'  {d["id"]}  type={d.get("type",{}).get("text")!r}  '
              f'date={d.get("date","")}  '
              f'author={(d.get("author") or [{}])[0].get("display","")!r}  '
              f'html_b64_size={att_size}')


if __name__ == '__main__':
    main()
