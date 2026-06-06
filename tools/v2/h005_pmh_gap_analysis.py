#!/usr/bin/env python3
"""H-005 follow-up: size the C-015 PMH gap across all 108 UCSF clinical notes.

For each note: extract PMH-table diagnoses, allergies, vital-sign panel.
Aggregate across notes; dedupe diagnoses (patient's PMH is patient-stable —
"GERD" mentioned in 10 notes counts once).

Report: cumulative unique unmatched real diagnoses (the actual C-015 gap),
plus aggregate confirmation of allergies and vitals patterns.
"""

import json
import re
import urllib.request
import base64
from collections import Counter, defaultdict
from html.parser import HTMLParser
from datetime import datetime

V2_BASE = 'http://localhost:8090/fhir'
V1_BASE = 'http://localhost:8080/fhir'


# ── Table extraction ──────────────────────────────────────────────────
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
    if head.startswith('vitals'):
        return 'Vitals'
    return None


def normalize_text(s):
    if not s:
        return ''
    s = re.sub(r'\([^)]*\)', '', s)
    return ' '.join(s.lower().split()).strip()


# ── Improved row classification ───────────────────────────────────────
BULLET_CHARS = {'•', '*', '-', '●'}


def is_main_diagnosis_row(row):
    """A main row starts with a bullet AND has 2nd cell content that
    looks like a diagnosis name (not a comment fragment)."""
    if not row or len(row) < 2:
        return False
    if row[0] not in BULLET_CHARS:
        return False
    name = (row[1] or '').strip()
    if not name or len(name) > 120:
        return False
    # Sub-comments often start with lowercase or include verbs/dates only
    if name[0].islower():
        return False
    return True


def extract_pmh_diagnoses(pmh_rows):
    """Return (name, date) for each true main diagnosis row."""
    out = []
    for r in pmh_rows:
        if not is_main_diagnosis_row(r):
            continue
        name = r[1].strip()
        # Strip "(CMS code)" suffix only
        name = re.sub(r'\s*\([^)]*code[^)]*\)\s*', '', name, flags=re.IGNORECASE).strip()
        date = r[2].strip() if len(r) > 2 else ''
        out.append((name, date))
    return out


def extract_allergens(allergy_rows):
    out = []
    for r in allergy_rows:
        if not is_main_diagnosis_row(r):
            continue
        out.append(r[1].strip())
    return out


def extract_vitals(vitals_rows):
    """Vitals: key:value pairs."""
    out = {}
    for r in vitals_rows:
        if len(r) >= 2 and r[0].endswith(':'):
            k = r[0].rstrip(':').strip()
            v = r[1].strip()
            if k and v:
                out[k] = v
    return out


# ── Generic fetch ─────────────────────────────────────────────────────
def fetch_all(base, rt, params=''):
    out = []
    url = f'{base}/{rt}?_count=200' + params
    while url:
        with urllib.request.urlopen(url) as r:
            bundle = json.loads(r.read())
        out.extend(e['resource'] for e in bundle.get('entry', []))
        url = next((l['url'] for l in bundle.get('link', []) if l.get('relation') == 'next'), None)
    return out


def cond_text(c):
    return ((c.get('code') or {}).get('text') or '').strip()


def ai_text(a):
    return ((a.get('code') or {}).get('text') or '').strip()


# ── Vital matching helpers ────────────────────────────────────────────
VITAL_PATTERNS = {
    'BP': r'blood pressure|bp\b|systolic|diastolic',
    'Pulse': r'heart rate|pulse',
    'SpO2': r'oxygen|spo2|sao2|sat',
    'Weight': r'body weight|^weight$',
    'Temp': r'temperature|temp\b',
    'Resp': r'respir',
}


def find_vital_match(vital_name, value_str, obs_by_text, note_dt):
    pat = VITAL_PATTERNS.get(vital_name)
    if not pat:
        return None
    candidates = []
    for k, lst in obs_by_text.items():
        if re.search(pat, k, re.IGNORECASE):
            candidates.extend(lst)
    # Score by date proximity
    best = None
    best_delta = None
    for o in candidates:
        d = o.get('effectiveDateTime') or o.get('issued') or ''
        if not d:
            continue
        try:
            dd = datetime.fromisoformat(d[:19].replace('Z', ''))
            delta = abs((dd - note_dt).days)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = (delta, o, d[:10])
        except Exception:
            continue
    return best


# ── Main ──────────────────────────────────────────────────────────────
def main():
    print('=== H-005 PMH gap analysis — cumulative across all UCSF notes ===')
    print()

    # 1. Fetch all v2 UCSF notes
    print('Fetching v2 UCSF clinical notes...')
    docs = fetch_all(V2_BASE, 'DocumentReference', '&_tag=urn:bina:source-org%7CUCSF')
    print(f'  {len(docs)} notes')

    # 2. Fetch v1 corpus
    print('Fetching v1 Condition corpus...')
    conditions = fetch_all(V1_BASE, 'Condition')
    cond_by_norm = defaultdict(list)
    for c in conditions:
        n = normalize_text(cond_text(c))
        if n:
            cond_by_norm[n].append(c)
    print(f'  {len(conditions)} Conditions, {len(cond_by_norm)} unique normalised names')

    print('Fetching v1 AllergyIntolerance corpus...')
    ais = fetch_all(V1_BASE, 'AllergyIntolerance')
    ai_by_norm = defaultdict(list)
    for a in ais:
        n = normalize_text(ai_text(a))
        if n:
            ai_by_norm[n].append(a)
    print(f'  {len(ais)} AllergyIntolerance, {len(ai_by_norm)} unique normalised names')

    print('Fetching v1 vital-sign Observations...')
    obs = fetch_all(V1_BASE, 'Observation', '&category=vital-signs')
    obs_by_text = defaultdict(list)
    for o in obs:
        code = o.get('code') or {}
        text = (code.get('text', '') or '').lower()
        if text:
            obs_by_text[text].append(o)
        for c in code.get('coding', []) or []:
            disp = (c.get('display', '') or '').lower()
            if disp:
                obs_by_text[disp].append(o)
    print(f'  {len(obs)} vital-sign Observations, {len(obs_by_text)} unique code-texts')

    # 3. Process each note
    print()
    print('Processing notes...')

    # Per-diagnosis: (normalised name) → {raw_name, notes_count, dates, match_status}
    all_diagnoses = defaultdict(lambda: {
        'raw_examples': [],
        'note_count': 0,
        'dates': set(),
        'matched': False,
        'match_examples': [],
    })

    all_allergens = defaultdict(lambda: {
        'raw_examples': [],
        'note_count': 0,
        'matched': False,
    })

    vitals_stats = Counter()
    vitals_perfect_same_day = 0
    vitals_notes_with_vitals = 0
    vitals_match_deltas = []

    notes_with_pmh = 0
    notes_with_allergies = 0

    for d in docs:
        content = d.get('content') or []
        if not content:
            continue
        att = content[0].get('attachment') or {}
        if not att.get('data'):
            continue
        html = base64.b64decode(att['data']).decode('utf-8')
        p = TableExtractor()
        p.feed(html)
        kinds = {}
        for rows in p.tables:
            k = table_kind(rows)
            if k and k not in kinds:
                kinds[k] = rows

        note_date = d.get('date', '')[:10]
        try:
            note_dt = datetime.fromisoformat(note_date) if note_date else None
        except Exception:
            note_dt = None

        # PMH
        if 'PMH' in kinds:
            notes_with_pmh += 1
            for name, date in extract_pmh_diagnoses(kinds['PMH']):
                norm = normalize_text(name)
                if len(norm) < 3:
                    continue
                entry = all_diagnoses[norm]
                entry['raw_examples'].append(name)
                entry['note_count'] += 1
                if date:
                    entry['dates'].add(date)
                # Match against Condition store
                direct = cond_by_norm.get(norm, [])
                partial = []
                if not direct:
                    for k, lst in cond_by_norm.items():
                        if (k in norm and len(k) > 5) or (norm in k and len(norm) > 5):
                            partial.extend(lst)
                            break  # one example is enough
                if direct or partial:
                    entry['matched'] = True
                    entry['match_examples'] = (direct + partial)[:1]

        # Allergies
        if 'Allergies' in kinds:
            notes_with_allergies += 1
            for raw in extract_allergens(kinds['Allergies']):
                norm = normalize_text(raw)
                if len(norm) < 3:
                    continue
                e = all_allergens[norm]
                e['raw_examples'].append(raw)
                e['note_count'] += 1
                if norm in ai_by_norm:
                    e['matched'] = True

        # Vitals
        if 'Vitals' in kinds and note_dt:
            v = extract_vitals(kinds['Vitals'])
            if v:
                vitals_notes_with_vitals += 1
                perfect = True
                for vname in ('BP', 'Pulse', 'SpO2', 'Weight', 'Temp'):
                    if vname not in v:
                        continue
                    m = find_vital_match(vname, v[vname], obs_by_text, note_dt)
                    if m is None:
                        vitals_stats[f'{vname}_unmatched'] += 1
                        perfect = False
                    else:
                        delta = m[0]
                        vitals_match_deltas.append(delta)
                        vitals_stats[f'{vname}_matched'] += 1
                        if delta != 0:
                            perfect = False
                if perfect:
                    vitals_perfect_same_day += 1

    # 4. Report
    print()
    print('=' * 72)
    print(f'Notes with PMH table:       {notes_with_pmh}/{len(docs)}')
    print(f'Notes with Allergies table: {notes_with_allergies}/{len(docs)}')
    print(f'Notes with Vitals table:    {vitals_notes_with_vitals}/{len(docs)}')

    print()
    print('━━━ PMH gap analysis (C-015 sizing) ━━━')
    print(f'  Unique normalised diagnoses across all notes: {len(all_diagnoses)}')
    matched = sum(1 for v in all_diagnoses.values() if v['matched'])
    unmatched = sum(1 for v in all_diagnoses.values() if not v['matched'])
    print(f'    matched in v1 Condition:                    {matched}  ({matched/len(all_diagnoses)*100:.0f}%)')
    print(f'    UNMATCHED (the C-015 gap):                  {unmatched}  ({unmatched/len(all_diagnoses)*100:.0f}%)')

    # Filter unmatched by note_count >= 2 (likely real, recurring diagnoses)
    print()
    print('  Top unmatched diagnoses by note recurrence (most likely real, omitted from v1):')
    print(f'    {"recurrence":>10s}  {"dates":>15s}  diagnosis')
    unmatched_items = [(k, v) for k, v in all_diagnoses.items() if not v['matched']]
    unmatched_items.sort(key=lambda x: -x[1]['note_count'])
    for norm, e in unmatched_items[:25]:
        dates = ','.join(sorted(e['dates']))[:14]
        # Pick a representative raw form
        raw = e['raw_examples'][0]
        print(f'    {e["note_count"]:>10d}  {dates:>15s}  {raw[:60]!r}')

    print()
    if len(unmatched_items) > 25:
        print(f'  ... plus {len(unmatched_items)-25} more')

    # Stratify by single-occurrence vs multi-occurrence (single is likely a typo or one-off comment)
    once = sum(1 for k, v in unmatched_items if v['note_count'] == 1)
    many = sum(1 for k, v in unmatched_items if v['note_count'] >= 2)
    print()
    print(f'  Of {unmatched} unmatched:')
    print(f'    seen in only 1 note (lower-confidence): {once}')
    print(f'    seen in ≥2 notes (high-confidence real diagnoses):  {many}')

    print()
    print('━━━ Allergies aggregate ━━━')
    print(f'  Unique normalised allergens across all notes: {len(all_allergens)}')
    a_matched = sum(1 for v in all_allergens.values() if v['matched'])
    a_unmatched = sum(1 for v in all_allergens.values() if not v['matched'])
    print(f'    matched in v1 AllergyIntolerance: {a_matched}')
    print(f'    UNMATCHED:                       {a_unmatched}')
    for norm, e in all_allergens.items():
        flag = '✓' if e['matched'] else '✗'
        print(f'    {flag} {norm!r:50s} recurrence={e["note_count"]}')

    print()
    print('━━━ Vitals aggregate (same-day correspondence test) ━━━')
    if vitals_match_deltas:
        same_day = sum(1 for d in vitals_match_deltas if d == 0)
        print(f'  Total vital-sign matches:        {len(vitals_match_deltas)}')
        print(f'  Same-day matches (Δ=0):          {same_day}  ({same_day/len(vitals_match_deltas)*100:.0f}%)')
        print(f'  Within 7 days:                   {sum(1 for d in vitals_match_deltas if d <= 7)}')
        print(f'  Within 30 days:                  {sum(1 for d in vitals_match_deltas if d <= 30)}')
        print(f'  Notes with all vitals same-day:  {vitals_perfect_same_day}/{vitals_notes_with_vitals}')
    print(f'  Per-vital stats: {dict(vitals_stats)}')


if __name__ == '__main__':
    main()
