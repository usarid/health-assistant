#!/usr/bin/env python3
"""v1↔v2 diff tool for Communication resources.

Compares production HAPI (port 8080) against v2 HAPI (port 8090).
Goal: surface every difference and categorize it as either an *expected*
v2 cleanup (cosmetic), a *real* semantic divergence (interesting — likely a
v2 bug or a v3 heuristic to systematize), or an asymmetric existence
(v1-only / v2-only — likely a manual edit in production, or a v1 dropped row).

Outputs:
  out/diff_communications_report.jsonl  — one JSON line per pair/orphan with full detail
  out/diff_communications_summary.txt    — human-readable categorized summary

Match strategy:
  - Extract all WP-24 substrings from identifier values on each side
  - Stanford numeric IDs become a second match key family
  - Match by first overlapping key; track each v2 comm at most once
"""

import json
import re
import sys
import urllib.request
from pathlib import Path
from collections import defaultdict, Counter

V1 = 'http://localhost:8080/fhir'
V2 = 'http://localhost:8090/fhir'
OUT_DIR = Path(__file__).resolve().parent / 'out'
OUT_DIR.mkdir(exist_ok=True)

WP24_RE = re.compile(r'WP-24[A-Za-z0-9_\-+=/.%]+')
PAYLOAD_PREFIX_RE = re.compile(r'^\[[^\]]+\]\s+[^:]+:\s*')


# ── HAPI access ────────────────────────────────────────────────────────
def fetch_all_comms(base):
    out = []
    url = f"{base}/Communication?_count=500"
    while url:
        with urllib.request.urlopen(url) as r:
            bundle = json.loads(r.read())
        out.extend(e['resource'] for e in bundle.get('entry', []))
        url = next((l['url'] for l in bundle.get('link', []) if l.get('relation') == 'next'), None)
    return out


# ── Matching ───────────────────────────────────────────────────────────
def match_keys(comm):
    """Yield all possible match keys for a Communication."""
    keys = set()
    for ident in comm.get('identifier', []):
        val = ident.get('value', '') or ''
        # All WP-24 substrings
        for m in WP24_RE.finditer(val):
            keys.add(('wp24', m.group(0)))
        # Stanford numeric ID
        if val.isdigit():
            keys.add(('stan-id', val))
    return keys


def build_index(comms):
    """key → list of comms; one comm appears under multiple keys."""
    idx = defaultdict(list)
    for c in comms:
        for k in match_keys(c):
            idx[k].append(c)
    return idx


# ── Field-level comparison ─────────────────────────────────────────────
def topic_text(c):
    return (c.get('topic') or {}).get('text', '') or ''


def sent_iso(c):
    return c.get('sent', '') or ''


def payload_strings(c):
    return [p.get('contentString', '') or '' for p in (c.get('payload') or [])]


def strip_payload_prefix(s):
    """Remove the '[date] Sender: ' prefix to isolate the body."""
    m = PAYLOAD_PREFIX_RE.match(s)
    return s[m.end():].strip() if m else s.strip()


def normalize_body(s):
    """Whitespace-normalize a body."""
    return re.sub(r'\s+', ' ', s).strip()


def compare_topics(t1, t2):
    if t1 == t2:
        return 'identical'
    if t2 and t1.startswith(t2):
        return 'v2-cleaned-prefix'  # expected v2 improvement
    if t1 and t2.startswith(t1):
        return 'v2-extended'
    return 'differs'


def compare_payloads(p1_list, p2_list):
    """Compare two payload lists. Returns one of:
       identical, cosmetic-only, len-differs, body-differs
    """
    if p1_list == p2_list:
        return 'identical'
    if len(p1_list) != len(p2_list):
        return 'len-differs'
    bodies1 = [normalize_body(strip_payload_prefix(s)) for s in p1_list]
    bodies2 = [normalize_body(strip_payload_prefix(s)) for s in p2_list]
    if bodies1 == bodies2:
        return 'cosmetic-only'
    return 'body-differs'


def diff_fields(c1, c2):
    """Compute structured per-field diff between v1 and v2 comm."""
    return {
        'topic_v1': topic_text(c1),
        'topic_v2': topic_text(c2),
        'topic_compare': compare_topics(topic_text(c1), topic_text(c2)),
        'sent_v1': sent_iso(c1),
        'sent_v2': sent_iso(c2),
        'sent_equal': sent_iso(c1) == sent_iso(c2),
        'payload_count_v1': len(payload_strings(c1)),
        'payload_count_v2': len(payload_strings(c2)),
        'payload_compare': compare_payloads(payload_strings(c1), payload_strings(c2)),
    }


def categorize_pair(d):
    """Bucket the diff into match-identical / match-cosmetic / match-semantic."""
    topic_ok = d['topic_compare'] in ('identical', 'v2-cleaned-prefix')
    sent_ok = d['sent_equal']
    payload_ok_strict = d['payload_compare'] == 'identical'
    payload_ok_cosmetic = d['payload_compare'] in ('identical', 'cosmetic-only')

    if topic_ok and sent_ok and payload_ok_strict and d['topic_compare'] == 'identical':
        return 'match-identical'
    if topic_ok and sent_ok and payload_ok_cosmetic:
        return 'match-cosmetic'
    return 'match-semantic'


# ── Identify a source-org tag for the diff record ──────────────────────
def source_org(c):
    for t in (c.get('meta') or {}).get('tag', []):
        if t.get('system') == 'urn:bina:source-org':
            return t.get('code', '')
    # v1 falls through here; try to infer from the old tag scheme
    for t in (c.get('meta') or {}).get('tag', []):
        code = t.get('code', '')
        if code.endswith('-mychart-scrape') or code.endswith('-myhealth-scrape'):
            return code.split('-')[0].upper()
    return 'UNKNOWN'


def source_portal(c):
    for t in (c.get('meta') or {}).get('tag', []):
        if t.get('system') == 'urn:bina:source-portal':
            return t.get('code', '')
        if t.get('code', '').endswith('-mychart-scrape'):
            return 'mskcc.mychart'
        if t.get('code', '').endswith('-myhealth-scrape'):
            return 'stanford.myhealth'
    return ''


# ── Main ───────────────────────────────────────────────────────────────
def main():
    print(f'Fetching v1 Communications from {V1}...')
    v1 = fetch_all_comms(V1)
    print(f'  {len(v1)}')
    print(f'Fetching v2 Communications from {V2}...')
    v2 = fetch_all_comms(V2)
    print(f'  {len(v2)}')

    print()
    print('Building match indexes...')
    v2_index = build_index(v2)
    print(f'  v2 indexed under {len(v2_index)} keys')

    # Pair v1 → v2 by first matching key
    pairs = []
    v2_matched_ids = set()
    v1_only = []
    no_keys = 0

    for c1 in v1:
        keys = match_keys(c1)
        if not keys:
            no_keys += 1
            v1_only.append(c1)
            continue
        matched = None
        for k in keys:
            candidates = v2_index.get(k, [])
            for c2 in candidates:
                if c2['id'] not in v2_matched_ids:
                    matched = c2
                    break
            if matched:
                break
        if matched:
            v2_matched_ids.add(matched['id'])
            pairs.append((c1, matched))
        else:
            v1_only.append(c1)

    v2_only = [c for c in v2 if c['id'] not in v2_matched_ids]

    print()
    print('=== Match results ===')
    print(f'  Pairs:    {len(pairs)}')
    print(f'  v1-only:  {len(v1_only)} ({no_keys} had no extractable match key)')
    print(f'  v2-only:  {len(v2_only)}')

    # Categorize pairs
    category_counts = Counter()
    pair_records = []
    for c1, c2 in pairs:
        d = diff_fields(c1, c2)
        cat = categorize_pair(d)
        category_counts[cat] += 1
        pair_records.append({
            'category': cat,
            'v1_id': c1['id'],
            'v2_id': c2['id'],
            'v1_org': source_org(c1),
            'v2_org': source_org(c2),
            'v2_portal': source_portal(c2),
            **d,
        })

    print()
    print('=== Pair categories ===')
    for cat, n in category_counts.most_common():
        print(f'  {cat:20s} {n:>5d}')

    # Write JSONL
    jsonl_path = OUT_DIR / 'diff_communications_report.jsonl'
    with open(jsonl_path, 'w') as f:
        for r in pair_records:
            f.write(json.dumps(r) + '\n')
        for c in v1_only:
            f.write(json.dumps({
                'category': 'v1-only',
                'v1_id': c['id'],
                'v1_org': source_org(c),
                'topic_v1': topic_text(c),
                'sent_v1': sent_iso(c),
                'payload_count_v1': len(payload_strings(c)),
                'identifiers': [
                    f"{i.get('system','')}|{i.get('value','')[:80]}"
                    for i in c.get('identifier', [])
                ],
            }) + '\n')
        for c in v2_only:
            f.write(json.dumps({
                'category': 'v2-only',
                'v2_id': c['id'],
                'v2_org': source_org(c),
                'v2_portal': source_portal(c),
                'topic_v2': topic_text(c),
                'sent_v2': sent_iso(c),
                'payload_count_v2': len(payload_strings(c)),
            }) + '\n')

    print()
    print(f'Detailed report written to: {jsonl_path}')
    print(f'  {jsonl_path.stat().st_size / 1024:.0f} KB, '
          f'{len(pair_records) + len(v1_only) + len(v2_only)} lines')

    # Summary breakdown by org and by sub-category
    print()
    print('=== Pair categories by source-org (v2 perspective) ===')
    org_cat = defaultdict(lambda: Counter())
    for r in pair_records:
        org_cat[r['v2_org']][r['category']] += 1
    orgs_sorted = sorted(org_cat.keys(), key=lambda o: -sum(org_cat[o].values()))
    header = f'  {"org":12s} {"identical":>10s} {"cosmetic":>10s} {"semantic":>10s} {"total":>8s}'
    print(header)
    for o in orgs_sorted:
        cc = org_cat[o]
        total = sum(cc.values())
        print(f'  {o:12s} {cc["match-identical"]:>10d} {cc["match-cosmetic"]:>10d} '
              f'{cc["match-semantic"]:>10d} {total:>8d}')

    # Detail breakdown of semantic diffs (what specifically differs?)
    semantic_pairs = [r for r in pair_records if r['category'] == 'match-semantic']
    if semantic_pairs:
        print()
        print('=== Semantic-diff breakdown (most informative) ===')
        topic_modes = Counter(r['topic_compare'] for r in semantic_pairs)
        sent_diffs = sum(1 for r in semantic_pairs if not r['sent_equal'])
        payload_modes = Counter(r['payload_compare'] for r in semantic_pairs)
        print(f'  topic_compare distribution:   {dict(topic_modes)}')
        print(f'  sent_equal=False:             {sent_diffs}')
        print(f'  payload_compare distribution: {dict(payload_modes)}')

        # Show first 5 semantic pairs in detail
        print()
        print('=== First 5 semantic-diff pairs (for triage) ===')
        for r in semantic_pairs[:5]:
            print()
            print(f'  [{r["category"]}] v1={r["v1_id"]}  v2={r["v2_id"]}  ({r["v2_org"]})')
            print(f'    topic v1: {r["topic_v1"][:100]!r}')
            print(f'    topic v2: {r["topic_v2"][:100]!r}')
            print(f'    sent v1: {r["sent_v1"]}  v2: {r["sent_v2"]}')
            print(f'    payload counts v1/v2: {r["payload_count_v1"]}/{r["payload_count_v2"]}  '
                  f'compare: {r["payload_compare"]}')

    # v1-only / v2-only summaries
    if v1_only:
        print()
        print(f'=== v1-only sample (first 5 of {len(v1_only)}) ===')
        for c in v1_only[:5]:
            print(f'  {c["id"]}  org={source_org(c)}  sent={sent_iso(c)[:19]}  topic={topic_text(c)[:60]!r}')

    if v2_only:
        print()
        print(f'=== v2-only sample (first 5 of {len(v2_only)}) ===')
        for c in v2_only[:5]:
            print(f'  {c["id"]}  org={source_org(c)}  sent={sent_iso(c)[:19]}  topic={topic_text(c)[:60]!r}')


if __name__ == '__main__':
    main()
