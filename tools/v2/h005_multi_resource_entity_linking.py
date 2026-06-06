#!/usr/bin/env python3
"""H-005 prototype, extended: entity-link Allergies / PMH / Vitals / Family History.

After medications (h005_med_entity_linking.py) confirmed the mechanism,
this script tests whether the same approach generalises to other resource
types in the same UCSF Office Visit note:

  - Allergies table     → AllergyIntolerance
  - Past Medical History → Condition
  - Vitals              → Observation (BP, Pulse, SpO2, Weight)
  - Family History      → FamilyMemberHistory (rarely populated in v1)

Each one tests a different match strategy and surfaces different gaps.
"""

import json
import re
import urllib.request
import base64
from collections import Counter, defaultdict
from html.parser import HTMLParser
from datetime import datetime

DOC_ID = 'docref-ucsf-b7a8f3cc8c22'
NOTE_DATE = '2024-10-03'
V2_BASE = 'http://localhost:8090/fhir'
V1_BASE = 'http://localhost:8080/fhir'


# ── HTML table extraction ──────────────────────────────────────────────
class TableExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.in_table = False
        self.rows = []
        self.row = []
        self.cell = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.in_table = True
            self.rows = []
        elif tag in ('td', 'th') and self.in_table:
            self.in_cell = True
            self.cell = []
        elif tag == 'tr' and self.in_table:
            self.row = []

    def handle_endtag(self, tag):
        if tag == 'table' and self.in_table:
            self.tables.append(self.rows)
            self.in_table = False
        elif tag in ('td', 'th') and self.in_cell:
            self.in_cell = False
            self.row.append(' '.join(self.cell).strip())
            self.cell = []
        elif tag == 'tr' and self.in_table:
            if any(c for c in self.row):
                self.rows.append(self.row)
            self.row = []

    def handle_data(self, d):
        if self.in_cell and d.strip():
            self.cell.append(d.strip())


def table_kind(rows):
    head = ' '.join(' '.join(r) for r in rows[:3]).lower()
    if 'allergies/contraindications' in head or 'allergen' in head:
        return 'Allergies'
    if 'past medical history' in head:
        return 'PMH'
    if 'family history' in head:
        return 'FamHx'
    if head.startswith('vitals') or 'vitals' in head.split()[:5]:
        return 'Vitals'
    return None


# ── Generic helpers ────────────────────────────────────────────────────
def fetch_all(base, rt):
    out = []
    url = f'{base}/{rt}?_count=200'
    while url:
        with urllib.request.urlopen(url) as r:
            bundle = json.loads(r.read())
        out.extend(e['resource'] for e in bundle.get('entry', []))
        url = next((l['url'] for l in bundle.get('link', []) if l.get('relation') == 'next'), None)
    return out


def normalize_text(s):
    """Lowercase, strip parenthetical brand/abbreviation, collapse whitespace."""
    if not s:
        return ''
    s = re.sub(r'\([^)]*\)', '', s)
    return ' '.join(s.lower().split()).strip()


def extract_bullet_rows(rows):
    """Drop header rows and rows that are just sub-comments; yield the main entries.

    Epic notes format:
      header row(s)
      bullet row: ['•', 'Diagnosis name', 'Date']
      sub-row:    ['', 'comment about diagnosis']
    """
    main_rows = []
    for r in rows:
        if not r or len(r) < 2:
            continue
        # Skip header/title rows
        joined = ' '.join(r).lower()
        if joined.startswith(('past medical', 'allergies', 'family history',
                              'diagnosis', 'allergen', 'medication', 'problem')):
            continue
        # A "main" row starts with a bullet character
        if r[0] in ('•', '*', '-') or (len(r) >= 2 and r[1] and not r[0]):
            main_rows.append(r)
    return main_rows


# ── Allergies ──────────────────────────────────────────────────────────
def extract_allergies(allergy_rows):
    """Yield candidate allergen names."""
    out = []
    for r in extract_bullet_rows(allergy_rows):
        # Cells: '•', 'Allergen', 'Reaction' typically
        if len(r) >= 2 and r[1]:
            out.append(r[1].strip())
    return out


def link_allergies(note_allergens, ais):
    """Match each note allergen against AllergyIntolerance.code.text."""
    by_name = defaultdict(list)
    for a in ais:
        name = (a.get('code') or {}).get('text', '') or ''
        if name:
            by_name[normalize_text(name)].append(a)

    results = []
    for cand in note_allergens:
        norm = normalize_text(cand)
        direct = by_name.get(norm, [])
        partial = []
        if not direct:
            # Substring either way
            for k, lst in by_name.items():
                if k in norm or norm in k:
                    partial.extend(lst)
        cat = 'exact' if direct else ('partial' if partial else 'unmatched')
        results.append((cat, cand, direct + partial))
    return results, list(by_name.keys())


# ── PMH / Conditions ───────────────────────────────────────────────────
def extract_pmh(pmh_rows):
    """Yield candidate diagnosis names with optional date."""
    out = []
    for r in extract_bullet_rows(pmh_rows):
        # Cells: '•', 'Diagnosis', 'Date' typically
        if len(r) >= 2 and r[1]:
            name = r[1].strip()
            # Strip "(CMS code)" suffix
            name = re.sub(r'\s*\([^)]*code[^)]*\)\s*', '', name, flags=re.IGNORECASE)
            date = r[2].strip() if len(r) > 2 else ''
            out.append((name, date))
    return out


def link_conditions(note_pmh, conditions):
    """Match each note diagnosis against Condition.code.text."""
    by_name = defaultdict(list)
    for c in conditions:
        text = (c.get('code') or {}).get('text', '') or ''
        if text:
            by_name[normalize_text(text)].append(c)

    results = []
    for cand, date in note_pmh:
        norm = normalize_text(cand)
        direct = by_name.get(norm, [])
        partial = []
        if not direct:
            for k, lst in by_name.items():
                if (k in norm and len(k) > 4) or (norm in k and len(norm) > 4):
                    partial.extend(lst)
        cat = 'exact' if direct else ('partial' if partial else 'unmatched')
        results.append((cat, cand, date, direct + partial))
    return results, list(by_name.keys())


# ── Vitals ─────────────────────────────────────────────────────────────
def extract_vitals(vitals_rows):
    """Vitals are key:value pairs — 'BP: | 98/65' format."""
    out = {}
    for r in vitals_rows:
        if len(r) >= 2 and r[0].endswith(':'):
            k = r[0].rstrip(':').strip()
            v = r[1].strip()
            if k and v:
                out[k] = v
    return out


def link_vitals(note_vitals, observations, note_date):
    """For each note vital, find Observation matching the metric and closest date."""
    # Map note vital labels to LOINC / common Observation codes
    vital_keys = {
        'BP': ('blood pressure', 'systolic|diastolic|blood pressure'),
        'Pulse': ('heart rate', 'pulse|heart rate'),
        'SpO2': ('oxygen saturation', 'oxygen|spo2|sao2'),
        'Weight': ('body weight', 'weight'),
    }

    # Index observations by code text or first display
    obs_by_text = defaultdict(list)
    for o in observations:
        code = o.get('code') or {}
        text = (code.get('text', '') or '').lower()
        for c in code.get('coding', []):
            disp = (c.get('display', '') or '').lower()
            if disp:
                obs_by_text[disp].append(o)
        if text:
            obs_by_text[text].append(o)

    note_dt = datetime.fromisoformat(note_date)

    results = []
    for vname, value in note_vitals.items():
        match_re = vital_keys.get(vname)
        if not match_re:
            results.append(('skipped', vname, value, []))
            continue
        canonical, pattern = match_re
        # Search obs_by_text for matching obs entries
        candidates = []
        for k, lst in obs_by_text.items():
            if re.search(pattern, k):
                candidates.extend(lst)
        # Get closest in time
        candidates_dated = []
        for o in candidates:
            d = o.get('effectiveDateTime') or o.get('issued') or ''
            try:
                dd = datetime.fromisoformat(d[:19])
                delta = abs((dd - note_dt).days)
                candidates_dated.append((delta, o, d[:10]))
            except Exception:
                pass
        candidates_dated.sort(key=lambda x: x[0])
        cat = 'matched' if candidates_dated else 'unmatched'
        results.append((cat, vname, value, candidates_dated[:3]))
    return results


# ── Main ───────────────────────────────────────────────────────────────
def main():
    print('=== H-005 prototype, extended to Allergies / PMH / Vitals ===')
    print(f'Note: {DOC_ID}  date: {NOTE_DATE}')
    print()

    with urllib.request.urlopen(f'{V2_BASE}/DocumentReference/{DOC_ID}') as r:
        d = json.loads(r.read())
    html = base64.b64decode(d['content'][0]['attachment']['data']).decode('utf-8')
    p = TableExtractor()
    p.feed(html)

    # Map kind → first matching table
    by_kind = {}
    for rows in p.tables:
        k = table_kind(rows)
        if k and k not in by_kind:
            by_kind[k] = rows

    # Allergies
    print('━━━ Allergies ━━━')
    if 'Allergies' in by_kind:
        allergens = extract_allergies(by_kind['Allergies'])
        print(f'Note allergens: {len(allergens)}')
        for a in allergens:
            print(f'  • {a!r}')
        ais = fetch_all(V1_BASE, 'AllergyIntolerance')
        results, ai_keys = link_allergies(allergens, ais)
        print(f'v1 AllergyIntolerance count: {len(ais)}')
        print(f'v1 distinct AI names: {len(ai_keys)} — {ai_keys}')
        for cat, cand, matches in results:
            print(f'  [{cat:>9s}] {cand!r:50s}  →  {len(matches)} v1 match(es)')
            for m in matches[:2]:
                print(f'              {m["id"]}  text={(m.get("code") or {}).get("text","")!r}')
    print()

    # PMH / Conditions
    print('━━━ Past Medical History → Condition ━━━')
    if 'PMH' in by_kind:
        pmh = extract_pmh(by_kind['PMH'])
        print(f'Note PMH entries: {len(pmh)}')
        cs = fetch_all(V1_BASE, 'Condition')
        results, c_keys = link_conditions(pmh, cs)
        print(f'v1 Condition count: {len(cs)}')
        print(f'v1 distinct Condition names: {len(c_keys)}')
        exact = sum(1 for r in results if r[0] == 'exact')
        partial = sum(1 for r in results if r[0] == 'partial')
        unmatched = sum(1 for r in results if r[0] == 'unmatched')
        print(f'  exact: {exact}   partial: {partial}   unmatched: {unmatched}')
        print('  Per-candidate:')
        for cat, cand, date, matches in results:
            date_str = f' [{date}]' if date else ''
            print(f'    [{cat:>9s}] {cand[:50]!r:55s}{date_str}  → {len(matches)} v1 match(es)')
            for m in matches[:1]:
                print(f'                 v1 cond: {m["id"][:32]} text={(m.get("code") or {}).get("text","")!r}')
    print()

    # Vitals
    print('━━━ Vitals → Observation ━━━')
    if 'Vitals' in by_kind:
        v_extract = extract_vitals(by_kind['Vitals'])
        print(f'Note vitals: {v_extract}')
        obs = fetch_all(V1_BASE, 'Observation')
        print(f'v1 Observation count: {len(obs)}')
        # Filter to vital-signs categorised obs to avoid scanning 36k labs
        vital_obs = [o for o in obs if any(
            (cat.get('coding') or [{}])[0].get('code') == 'vital-signs'
            for cat in (o.get('category') or [])
        )]
        print(f'v1 Observations categorised as vital-signs: {len(vital_obs)}')
        results = link_vitals(v_extract, vital_obs, NOTE_DATE)
        for cat, name, value, matches in results:
            print(f'  [{cat:>9s}] {name}={value!r}  → {len(matches)} v1 candidate(s)')
            for delta, o, d in matches[:1]:
                obs_val = o.get('valueQuantity') or o.get('valueString') or {}
                if isinstance(obs_val, dict) and 'value' in obs_val:
                    val_str = f"{obs_val['value']} {obs_val.get('unit','')}"
                else:
                    val_str = str(obs_val)
                print(f'              ±{delta}d {d}  {o["id"][:32]}  value={val_str!r}')

    print()
    print('=== Aggregate summary ===')
    summary = []
    if 'Allergies' in by_kind:
        ar_results, _ = link_allergies(extract_allergies(by_kind['Allergies']),
                                       fetch_all(V1_BASE, 'AllergyIntolerance'))
        exact = sum(1 for r in ar_results if r[0] == 'exact')
        partial = sum(1 for r in ar_results if r[0] == 'partial')
        unmatched = sum(1 for r in ar_results if r[0] == 'unmatched')
        summary.append(('Allergies', len(ar_results), exact, partial, unmatched))
    if 'PMH' in by_kind:
        cr_results, _ = link_conditions(extract_pmh(by_kind['PMH']),
                                        fetch_all(V1_BASE, 'Condition'))
        exact = sum(1 for r in cr_results if r[0] == 'exact')
        partial = sum(1 for r in cr_results if r[0] == 'partial')
        unmatched = sum(1 for r in cr_results if r[0] == 'unmatched')
        summary.append(('PMH→Condition', len(cr_results), exact, partial, unmatched))

    print(f'  {"resource":20s}  {"cands":>6s}  {"exact":>6s}  {"partial":>8s}  {"unmatched":>10s}')
    for r in summary:
        print(f'  {r[0]:20s}  {r[1]:>6d}  {r[2]:>6d}  {r[3]:>8d}  {r[4]:>10d}')


if __name__ == '__main__':
    main()
