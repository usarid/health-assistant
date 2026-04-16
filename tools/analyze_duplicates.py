#!/usr/bin/env python3
"""Analyze the scale of duplicate observations across data sources.

Fetches all observations from HAPI and identifies likely duplicates by
matching on LOINC code + date proximity + value similarity.

Usage: python3 analyze_duplicates.py
Output: duplicate_analysis.json
"""

import json
import re
import sys
import requests
from collections import defaultdict, Counter
from datetime import datetime, timedelta

HAPI_BASE = 'http://localhost:8080/fhir'


def get_all_observations():
    """Fetch all observations from HAPI, handling pagination."""
    all_obs = []
    url = f'{HAPI_BASE}/Observation?_count=500&_sort=-date'
    page = 0
    while url:
        resp = requests.get(url)
        if resp.status_code != 200:
            print(f'  Error: {resp.status_code}')
            break
        bundle = resp.json()
        entries = bundle.get('entry', [])
        all_obs.extend(entry['resource'] for entry in entries)
        page += 1
        if page % 10 == 0:
            print(f'  Fetched {len(all_obs)} observations...')

        url = None
        for link in bundle.get('link', []):
            if link['relation'] == 'next':
                url = link['url']
                break
    return all_obs


def extract_source_tag(obs):
    """Get the source tag from an observation."""
    tags = obs.get('meta', {}).get('tag', [])
    for t in tags:
        code = t.get('code', '')
        if code and code not in ('quality-patch-v1', 'quality-patch-v2'):
            return code
    return None


def extract_loinc(obs):
    """Get LOINC code from observation."""
    for coding in obs.get('code', {}).get('coding', []):
        if coding.get('system') == 'http://loinc.org' and 'code' in coding:
            return coding['code']
    return None


def extract_display(obs):
    """Get display name from observation."""
    codings = obs.get('code', {}).get('coding', [])
    if codings:
        return codings[0].get('display', '')
    return obs.get('code', {}).get('text', '')


def extract_date(obs):
    """Extract date as datetime.date from observation."""
    dt = obs.get('effectiveDateTime') or obs.get('effectivePeriod', {}).get('start', '')
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt.replace('Z', '+00:00')).date()
    except:
        try:
            return datetime.strptime(dt[:10], '%Y-%m-%d').date()
        except:
            return None


def extract_value(obs):
    """Extract a comparable value string from observation."""
    vq = obs.get('valueQuantity', {})
    if vq and vq.get('value') is not None:
        comp = vq.get('comparator', '')
        return f"{comp}{vq['value']} {vq.get('unit', '')}"

    vs = obs.get('valueString')
    if vs:
        return vs.strip()[:100]

    vcc = obs.get('valueCodeableConcept', {})
    if vcc:
        codings = vcc.get('coding', [])
        if codings:
            return codings[0].get('display', '')
        return vcc.get('text', '')

    return None


def normalize_value(val):
    """Normalize a value for comparison."""
    if not val:
        return ''
    val = val.strip().lower()
    # Remove units for numeric comparison
    val = re.sub(r'\s*(mg/dl|mmol/l|g/dl|k/ul|mil/ul|u/l|ng/ml|pg|fl|%|g/l|meq/l)\s*$', '', val, flags=re.I)
    val = val.strip()
    return val


def values_match(v1, v2):
    """Check if two values are semantically equivalent."""
    if not v1 or not v2:
        return False  # Can't determine match

    n1 = normalize_value(v1)
    n2 = normalize_value(v2)

    if n1 == n2:
        return True

    # Numeric comparison with tolerance
    try:
        f1 = float(re.sub(r'[<>]=?', '', n1))
        f2 = float(re.sub(r'[<>]=?', '', n2))
        if f1 == 0 and f2 == 0:
            return True
        if max(f1, f2) > 0:
            return abs(f1 - f2) / max(abs(f1), abs(f2)) < 0.01  # 1% tolerance
    except (ValueError, ZeroDivisionError):
        pass

    # Semantic equivalence
    neg_terms = {'negative', 'not detected', 'none detected', 'no growth', 'neg'}
    if n1 in neg_terms and n2 in neg_terms:
        return True

    return False


def main():
    print('Fetching all observations from HAPI...')
    all_obs = get_all_observations()
    print(f'Total observations: {len(all_obs)}')

    # Group by potential duplicate key: (LOINC or display name, approximate date)
    # Use display name as fallback when LOINC is missing
    groups = defaultdict(list)

    for obs in all_obs:
        loinc = extract_loinc(obs)
        display = extract_display(obs)
        date = extract_date(obs)
        source = extract_source_tag(obs)

        if not date:
            continue

        # Key: use LOINC if available, otherwise normalized display name
        if loinc:
            key = f'loinc:{loinc}'
        elif display:
            key = f'name:{display.lower().strip()}'
        else:
            continue

        groups[key].append({
            'id': obs['id'],
            'display': display,
            'date': date,
            'value': extract_value(obs),
            'source': source,
            'loinc': loinc,
        })

    # Within each group, find date clusters (within ±3 days)
    duplicate_clusters = []
    total_duplicates = 0
    source_pair_counts = Counter()

    for key, obs_list in groups.items():
        if len(obs_list) < 2:
            continue

        # Sort by date
        obs_list.sort(key=lambda o: o['date'])

        # Find clusters of observations within 3 days of each other
        used = set()
        for i, obs_a in enumerate(obs_list):
            if i in used:
                continue
            cluster = [obs_a]
            used.add(i)

            for j, obs_b in enumerate(obs_list):
                if j in used:
                    continue
                if abs((obs_a['date'] - obs_b['date']).days) <= 3:
                    # Check if sources differ
                    if obs_a['source'] != obs_b['source']:
                        cluster.append(obs_b)
                        used.add(j)
                    elif obs_a['source'] == obs_b['source']:
                        # Same source, same date window - could still be dup
                        if values_match(obs_a['value'], obs_b['value']):
                            cluster.append(obs_b)
                            used.add(j)

            if len(cluster) > 1:
                # Verify this is likely a true duplicate, not just coincidence
                sources = set(c['source'] for c in cluster)

                cluster_info = {
                    'key': key,
                    'display': cluster[0]['display'],
                    'dates': [str(c['date']) for c in cluster],
                    'sources': [c['source'] for c in cluster],
                    'values': [c['value'] for c in cluster],
                    'ids': [c['id'] for c in cluster],
                    'count': len(cluster),
                    'cross_source': len(sources) > 1,
                }
                duplicate_clusters.append(cluster_info)
                total_duplicates += len(cluster) - 1  # All but one are duplicates

                # Track source pairs
                for s in sources:
                    for s2 in sources:
                        if s != s2:
                            pair = tuple(sorted([s or 'none', s2 or 'none']))
                            source_pair_counts[pair] += 1

    # Summary statistics
    cross_source = [c for c in duplicate_clusters if c['cross_source']]
    same_source = [c for c in duplicate_clusters if not c['cross_source']]

    print(f'\n=== DUPLICATE ANALYSIS ===')
    print(f'Total duplicate clusters: {len(duplicate_clusters)}')
    print(f'Cross-source clusters: {len(cross_source)}')
    print(f'Same-source clusters: {len(same_source)}')
    print(f'Total duplicate observations (removable): {total_duplicates}')
    print(f'\nSource pair overlap:')
    for pair, count in source_pair_counts.most_common(20):
        print(f'  {pair[0]} <-> {pair[1]}: {count} clusters')

    # Show examples
    print(f'\n=== CROSS-SOURCE DUPLICATE EXAMPLES (first 20) ===')
    for c in cross_source[:20]:
        print(f'{c["display"]}:')
        for i in range(c['count']):
            print(f'  {c["dates"][i]} | src={c["sources"][i]} | val={c["values"][i]}')
        print()

    # Save full analysis
    output = {
        'summary': {
            'total_observations': len(all_obs),
            'total_duplicate_clusters': len(duplicate_clusters),
            'cross_source_clusters': len(cross_source),
            'same_source_clusters': len(same_source),
            'total_removable_duplicates': total_duplicates,
            'source_pair_overlap': {f'{p[0]} <-> {p[1]}': c for p, c in source_pair_counts.most_common()},
        },
        'duplicate_clusters': duplicate_clusters,
    }

    with open('duplicate_analysis.json', 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print(f'\nFull analysis saved to duplicate_analysis.json')


if __name__ == '__main__':
    main()
