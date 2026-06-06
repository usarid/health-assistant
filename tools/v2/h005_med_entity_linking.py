#!/usr/bin/env python3
"""H-005 prototype: entity-link medications in clinical notes to MedicationRequest resources.

Picks the largest UCSF Office Visit note (rich with structured tables) and:
  1. Extracts medication-table contents from the HTML
  2. Normalizes med names (Epic format → simple drug name)
  3. Looks up each candidate in v1 HAPI's MedicationRequest store
  4. Reports: matched / partial / unmatched, plus the pattern of failures

Goal: empirically test H-005's testable claim that note-embedded medication
dumps can be resolved to canonical MedicationRequest resources. Surfaces what
matching strategies work, where they fail, and what additional infrastructure
(RxNorm crosswalk, fuzzy match) would be needed for production use.
"""

import json
import re
import urllib.request
import base64
from collections import Counter
from html.parser import HTMLParser

DOC_ID = 'docref-ucsf-b7a8f3cc8c22'  # Ramon Partida Office Visit 2024-10-03
V2_BASE = 'http://localhost:8090/fhir'
V1_BASE = 'http://localhost:8080/fhir'


# ── HTML table extraction ──────────────────────────────────────────────
class TableContextExtractor(HTMLParser):
    """Walks HTML, tracks per-table the heading-like text just before it."""
    def __init__(self):
        super().__init__()
        self.tables = []        # list of (preceding_text, rows)
        self.text_buf = []      # accumulating text outside tables
        self.in_table = False
        self.current_rows = []
        self.current_row = []
        self.cell_text = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.in_table = True
            self.current_rows = []
            preceding = ' '.join(self.text_buf[-30:]).strip()[-200:]
            self._preceding = preceding
            self.text_buf = []
        elif tag in ('td', 'th') and self.in_table:
            self.in_cell = True
            self.cell_text = []
        elif tag == 'tr' and self.in_table:
            self.current_row = []

    def handle_endtag(self, tag):
        if tag == 'table' and self.in_table:
            self.tables.append((self._preceding, self.current_rows))
            self.in_table = False
            self.current_rows = []
        elif tag in ('td', 'th') and self.in_cell:
            self.in_cell = False
            self.current_row.append(' '.join(self.cell_text).strip())
            self.cell_text = []
        elif tag == 'tr' and self.in_table:
            if any(c for c in self.current_row):
                self.current_rows.append(self.current_row)
            self.current_row = []

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self.in_cell:
            self.cell_text.append(text)
        elif not self.in_table:
            self.text_buf.append(text)


def extract_tables(html):
    p = TableContextExtractor()
    p.feed(html)
    return p.tables


def identify_medication_tables(tables):
    """Return tables whose preceding-text suggests a medication list."""
    med = []
    for preceding, rows in tables:
        p = (preceding or '').lower()
        if any(k in p for k in ('medications', 'medication list',
                                'current medications', 'current hypertension')):
            med.append((preceding.strip(), rows))
    return med


# ── Medication name normalization ──────────────────────────────────────
# Strip Epic's mixed-case formatting, brand-name parens, dose, form, sig.
# Examples:
#   "amLODIPine (NORVASC) 5 mg tablet" → "amlodipine"
#   "hydroCHLOROthiazide 12.5 mg tablet" → "hydrochlorothiazide"
#   "losartan (COZAAR) 50 mg tablet" → "losartan"
#   "acetaminophen (TYLENOL ORAL)" → "acetaminophen"
DOSE_FORM_RE = re.compile(
    r'\s*\d[\d./]*\s*(mg|mcg|g|ml|unit|units|tab|tabs|tablet|capsule|cap|caps|'
    r'puff|patch|ml|liquid|cream|ointment|drop)\b.*$',
    re.IGNORECASE,
)


def normalize_med_name(raw):
    if not raw:
        return ''
    # Take just the first line / first non-empty chunk if multi-cell concat
    s = raw.split('\n')[0].split('|')[0].strip()
    # Remove brand-name parens
    s = re.sub(r'\([^)]*\)', '', s)
    # Trim trailing dose+form
    s = DOSE_FORM_RE.sub('', s)
    # Collapse and lowercase
    return ' '.join(s.lower().split()).strip()


# ── Lookup against HAPI ────────────────────────────────────────────────
def fetch_all_medication_requests(base):
    out = []
    url = f'{base}/MedicationRequest?_count=200'
    while url:
        with urllib.request.urlopen(url) as r:
            bundle = json.loads(r.read())
        out.extend(e['resource'] for e in bundle.get('entry', []))
        url = next((l['url'] for l in bundle.get('link', []) if l.get('relation') == 'next'), None)
    return out


def med_request_text(mr):
    """Get the human-readable med name from a MedicationRequest.

    v1 ingestion stores the name primarily in medicationReference.display
    (731 of 739 MRs). A small minority use medicationCodeableConcept.text
    (patient-entered or patient-reported additions). Check both.
    """
    ref_disp = (mr.get('medicationReference') or {}).get('display', '') or ''
    if ref_disp:
        return ref_disp
    cc = mr.get('medicationCodeableConcept') or {}
    return cc.get('text', '') or ''


# ── Main ───────────────────────────────────────────────────────────────
def main():
    print('=== H-005 medication entity-linking prototype ===')
    print(f'Note: {DOC_ID}')
    print()

    with urllib.request.urlopen(f'{V2_BASE}/DocumentReference/{DOC_ID}') as r:
        d = json.loads(r.read())
    html = base64.b64decode(d['content'][0]['attachment']['data']).decode('utf-8')
    print(f'HTML: {len(html):,} bytes')

    tables = extract_tables(html)
    print(f'Tables: {len(tables)}')

    # Scan ALL cells in ALL tables — don't assume we can label tables in advance.
    # A med-name cell has either:
    #   (a) Epic mixed-case format (internal capital, e.g. "amLODIPine")
    #   (b) a dose+form suffix (e.g. "X mg tablet" / "Y mcg capsule")
    print()
    print('=== Extracting candidate medications from all table cells ===')
    candidates = []  # (raw_cell, normalized, table_preceding)
    for preceding, rows in tables:
        for row in rows:
            for cell in row:
                if not cell or len(cell) < 4:
                    continue
                looks_med = (
                    re.search(r'\b\d+\s*(mg|mcg|g|ml|tab|tablet|capsule|cap|caps|patch|unit)\b',
                              cell, re.IGNORECASE)
                    or re.search(r'[a-z][A-Z]', cell)  # Epic mixed-case
                )
                if looks_med:
                    norm = normalize_med_name(cell)
                    if norm and len(norm) > 2:
                        candidates.append((cell[:90], norm, preceding[:60]))

    # Dedup by normalized name
    seen = set()
    unique = []
    for raw, norm, head in candidates:
        if norm not in seen:
            seen.add(norm)
            unique.append((raw, norm, head))
    candidates = unique
    print(f'Unique candidate medications across all tables: {len(candidates)}')
    for raw, norm, _ in candidates:
        print(f'  raw {raw!r:65s}  →  norm {norm!r}')

    print()
    print('=== Loading v1 HAPI MedicationRequest corpus ===')
    mrs = fetch_all_medication_requests(V1_BASE)
    print(f'  {len(mrs)} MedicationRequests in v1')

    # Build a normalized lookup. Each MR may have several distinct sources
    # ("losartan", "losartan 50mg tablet", "losartan (COZAAR)"); normalize all.
    by_normname = {}
    for mr in mrs:
        text = med_request_text(mr)
        norm = normalize_med_name(text)
        if norm:
            by_normname.setdefault(norm, []).append((mr['id'], text))
    print(f'  {len(by_normname)} distinct normalized names in v1')

    # Try prefix-match too: a candidate "losartan" matches any MR text starting
    # with "losartan" as a word — captures cases where the candidate is the
    # active ingredient and the HAPI text has a longer form.
    def prefix_match(norm):
        out = []
        for k, lst in by_normname.items():
            if k == norm or k.startswith(norm + ' ') or norm.startswith(k + ' '):
                out.extend(lst)
        return out

    print()
    print('=== Matching candidates to MedicationRequests ===')
    exact = partial = unmatched = 0
    results = []
    for raw, norm, head in candidates:
        direct = by_normname.get(norm, [])
        prefix = prefix_match(norm) if not direct else []
        if direct:
            exact += 1
            results.append(('exact', raw, norm, len(direct), direct[:3]))
        elif prefix:
            partial += 1
            results.append(('prefix', raw, norm, len(prefix), prefix[:3]))
        else:
            unmatched += 1
            results.append(('unmatched', raw, norm, 0, []))

    print()
    print(f'  exact     : {exact:>3d}')
    print(f'  prefix    : {partial:>3d}')
    print(f'  unmatched : {unmatched:>3d}')
    print()
    print('=== Per-candidate detail ===')
    for cat, raw, norm, count, samples in results:
        print(f'  [{cat:>9s}] {raw!r:60s} → norm={norm!r}  matches={count}')
        for mr_id, mr_text in samples[:2]:
            print(f'              candidate v1 MR: {mr_id}  text={mr_text[:80]!r}')

    print()
    print('=== Implications for H-005 ===')
    total = len(candidates)
    if total:
        print(f'  Match rate (exact OR prefix): {(exact+partial)/total*100:.0f}%')
        print(f'  If we replaced each matched mention with a reference like')
        print(f'  "[MedicationRequest: <id>]", we would replace {exact+partial} dump-rows')
        print(f'  in this single note. The {unmatched} unmatched are the cost of')
        print(f'  not yet having a synonym/brand layer (RxNorm-style crosswalk).')


if __name__ == '__main__':
    main()
