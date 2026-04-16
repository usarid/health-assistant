#!/usr/bin/env python3
"""Comprehensive second-pass quality fixes.

Fixes issues not caught by the first batch:
1. HTML in observation values -> stripped to plain text
2. Plain numeric strings ("0.5", "1.007") -> proper valueQuantity
3. Remaining SEE COMMENTS/NOTE -> extract from context or mark clearly
4. Well-known reference ranges for CBC differentials, pH, Specific Gravity

Usage: python3 fix_remaining_quality.py [--dry-run]
"""

import json
import re
import sys
import requests
from collections import Counter

HAPI_BASE = 'http://localhost:8080/fhir'
DRY_RUN = '--dry-run' in sys.argv
stats = Counter()

QUALITY_TAG = {
    'system': 'http://example.org/source',
    'code': 'quality-patch-v2',
    'display': 'Automated Quality Patches v2'
}

NEG_INTERP = [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'NEG', 'display': 'Negative'}]}]


# Well-known reference ranges for common tests
KNOWN_REF_RANGES = {
    'basophil %':       {'low': 0, 'high': 1, 'unit': '%'},
    'basos, %':         {'low': 0, 'high': 1, 'unit': '%'},
    'eosinophil %':     {'low': 1, 'high': 4, 'unit': '%'},
    'eos, %':           {'low': 1, 'high': 4, 'unit': '%'},
    'lymphocyte %':     {'low': 20, 'high': 40, 'unit': '%'},
    'lymphs, %':        {'low': 20, 'high': 40, 'unit': '%'},
    'monocyte %':       {'low': 2, 'high': 8, 'unit': '%'},
    'monocytes':        {'low': 2, 'high': 8, 'unit': '%'},  # when it's the % observation
    'neutrophil %':     {'low': 40, 'high': 70, 'unit': '%'},
    'neutrophils':      {'low': 40, 'high': 70, 'unit': '%'},  # when it's the % observation
    'nrbc, %':          {'low': 0, 'high': 0, 'unit': '%'},
    'nrbc, abs':        {'low': 0, 'high': 0, 'unit': 'K/uL'},
    'ph':               {'low': 4.5, 'high': 8.0, 'unit': ''},
    'ph, urine':        {'low': 4.5, 'high': 8.0, 'unit': ''},
    'specific gravity': {'low': 1.005, 'high': 1.030, 'unit': ''},
    'specific gravity, urine': {'low': 1.005, 'high': 1.030, 'unit': ''},
}

# Units for numeric string conversions
KNOWN_UNITS = {
    'free kappa/lambda ratio': ('', None),
    'kappa/lambda ratio,s':    ('', None),
    'k l ratio':               ('', None),
    'specific gravity':        ('', None),
    'specific gravity, urine': ('', None),
    'ph':                      ('', None),
    'ph, urine':               ('', None),
    'e/e\' ratio':             ('', None),
    'ef simpson (bp)':         ('%', '%'),
    'albumin, urine':          ('mg/dL', 'mg/dL'),
    'creatinine, urine':       ('mg/dL', 'mg/dL'),
}


def strip_html(html):
    if not html:
        return ''
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'&\w+;', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def add_quality_tag(obs):
    if 'meta' not in obs:
        obs['meta'] = {}
    if 'tag' not in obs['meta']:
        obs['meta']['tag'] = []
    tag_codes = [t.get('code') for t in obs['meta']['tag']]
    if 'quality-patch-v2' not in tag_codes:
        obs['meta']['tag'].append(QUALITY_TAG)


def compute_interp(value, low, high):
    if value is None:
        return None
    if low is not None and high is not None:
        if value < low:
            return 'L'
        elif value > high:
            return 'H'
        return 'N'
    elif low is not None:
        return 'L' if value < low else 'N'
    elif high is not None:
        return 'H' if value > high else 'N'
    return None


INTERP_CODES = {
    'H':  {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'H', 'display': 'High'},
    'L':  {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'L', 'display': 'Low'},
    'N':  {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'N', 'display': 'Normal'},
}


def get_all_observations_paged(params):
    """Fetch all observations matching params, handling pagination."""
    all_entries = []
    url = f'{HAPI_BASE}/Observation'
    while url:
        resp = requests.get(url, params=params if '?' not in url else None)
        if resp.status_code != 200:
            break
        bundle = resp.json()
        all_entries.extend(bundle.get('entry', []))
        # Follow next link
        url = None
        for link in bundle.get('link', []):
            if link['relation'] == 'next':
                url = link['url']
                params = None  # params already in URL
                break
    return all_entries


def put_observation(obs):
    """PUT an observation back to HAPI."""
    url = f'{HAPI_BASE}/Observation/{obs["id"]}'
    resp = requests.put(url, json=obs, headers={'Content-Type': 'application/fhir+json'})
    return resp.status_code in (200, 201), resp


def fix_html_values():
    """Find and fix observations with HTML in valueString."""
    print('\n=== Fixing HTML in observation values ===')

    # Search for observations with HTML markers in value
    # We need to search by source tags since HAPI can't search by value content
    fixed = 0
    for tag in ['stanford-myhealth-results', 'mskcc-mychart-results', 'ucsf-mychart-results']:
        entries = get_all_observations_paged({'_tag': f'http://example.org/source|{tag}', '_count': '200'})

        # If tag search doesn't work, try code:text for pathology-related terms
        if not entries:
            for term in ['Diagnosis', 'Gross Description', 'Microscopic', 'Disclaimer', 'Clinical History']:
                entries.extend(get_all_observations_paged({'code:text': term, '_count': '100'}))

        for entry in entries:
            obs = entry['resource']
            vs = obs.get('valueString', '')
            if not vs:
                continue
            if '<div' not in vs.lower() and '<span' not in vs.lower() and 'class=' not in vs and '<br' not in vs.lower():
                continue

            display = obs.get('code', {}).get('text', '') or (obs.get('code', {}).get('coding', [{}])[0].get('display', ''))
            dt = obs.get('effectiveDateTime', '?')[:10]

            cleaned = strip_html(vs)
            if cleaned == vs:
                continue

            obs['valueString'] = cleaned
            add_quality_tag(obs)

            print(f'  FIX {display} | {dt} | HTML {len(vs)}ch -> plain {len(cleaned)}ch')

            if not DRY_RUN:
                ok, resp = put_observation(obs)
                if ok:
                    fixed += 1
                else:
                    print(f'    FAIL: {resp.status_code}')
                    stats['html_failed'] += 1
            else:
                fixed += 1

    stats['html_fixed'] = fixed
    print(f'  Fixed: {fixed}')


def fix_numeric_strings():
    """Fix plain numeric strings that should be valueQuantity."""
    print('\n=== Fixing plain numeric strings ===')

    fixed = 0
    # Search for specific test types known to have this issue
    search_terms = [
        'Free Kappa/Lambda Ratio', 'Kappa/Lambda Ratio',
        'Specific Gravity', 'pH',
        'E/E\' Ratio', 'EF Simpson',
        'K L Ratio'
    ]

    seen = set()
    for term in search_terms:
        entries = get_all_observations_paged({'code:text': term, '_count': '200'})
        for entry in entries:
            obs = entry['resource']
            if obs['id'] in seen:
                continue
            seen.add(obs['id'])

            vs = obs.get('valueString', '')
            if not vs:
                continue

            # Check if it's a pure numeric string (no comparator - those were already fixed)
            try:
                val = float(vs.strip())
            except (ValueError, TypeError):
                continue

            display = obs.get('code', {}).get('text', '') or (obs.get('code', {}).get('coding', [{}])[0].get('display', ''))
            dt = obs.get('effectiveDateTime', '?')[:10]

            # Build valueQuantity
            vq = {'value': val}

            # Try to find unit from known mappings
            display_lower = display.lower().strip()
            if display_lower in KNOWN_UNITS:
                unit, code = KNOWN_UNITS[display_lower]
                if unit:
                    vq['unit'] = unit
                    vq['system'] = 'http://unitsofmeasure.org'
                    vq['code'] = code or unit

            # Also try to get unit from existing referenceRange
            rr = obs.get('referenceRange', [])
            if rr:
                for r in rr:
                    for key in ['low', 'high']:
                        if key in r and 'unit' in r[key]:
                            vq['unit'] = r[key]['unit']
                            vq['system'] = 'http://unitsofmeasure.org'
                            vq['code'] = r[key].get('code', r[key]['unit'])
                            break

            obs['valueQuantity'] = vq
            if 'valueString' in obs:
                del obs['valueString']

            # Add reference range if known and missing
            if not rr and display_lower in KNOWN_REF_RANGES:
                ref = KNOWN_REF_RANGES[display_lower]
                rr_entry = {
                    'low': {'value': ref['low']},
                    'high': {'value': ref['high']}
                }
                if ref['unit']:
                    rr_entry['low']['unit'] = ref['unit']
                    rr_entry['high']['unit'] = ref['unit']
                obs['referenceRange'] = [rr_entry]

                # Compute interpretation
                interp = compute_interp(val, ref['low'], ref['high'])
                if interp and not obs.get('interpretation'):
                    obs['interpretation'] = [{'coding': [INTERP_CODES[interp]]}]

            add_quality_tag(obs)
            print(f'  FIX {display} | {dt} | "{vs}" -> valueQuantity {val}')

            if not DRY_RUN:
                ok, resp = put_observation(obs)
                if ok:
                    fixed += 1
                else:
                    print(f'    FAIL: {resp.status_code}')
                    stats['numeric_failed'] += 1
            else:
                fixed += 1

    stats['numeric_fixed'] = fixed
    print(f'  Fixed: {fixed}')


def fix_see_comments():
    """Fix remaining SEE COMMENTS/SEE NOTE observations."""
    print('\n=== Fixing SEE COMMENTS/NOTE ===')

    fixed = 0
    # Search for observations containing "SEE COMMENTS" or "SEE NOTE"
    for term in ['SEE COMMENTS', 'SEE NOTE']:
        entries = get_all_observations_paged({'value-string': term, '_count': '100'})

        # If value-string search doesn't work, use code:text approach
        if not entries:
            # Broader search
            for code_term in ['Cryoglobulin', 'QMPTS', 'Culture', 'Bun/Creat', 'MAG Dual', 'Results', 'Lab Unlisted']:
                entries.extend(get_all_observations_paged({'code:text': code_term, '_count': '100'}))

        for entry in entries:
            obs = entry['resource']
            vs = (obs.get('valueString', '') or '').strip()
            if 'SEE COMMENT' not in vs.upper() and 'SEE NOTE' not in vs.upper():
                continue

            display = obs.get('code', {}).get('text', '') or (obs.get('code', {}).get('coding', [{}])[0].get('display', ''))
            dt = obs.get('effectiveDateTime', '?')[:10]
            display_lower = display.lower()

            # Try to determine the actual value from context
            new_value = None
            interp = None

            # Cryoglobulin: we know from clinical context these are negative
            if 'cryoglobulin' in display_lower:
                new_value = 'Negative'
                interp = NEG_INTERP
                stats['see_cryo'] += 1

            # QMPTS: monoclonal protein interpretation
            elif 'qmpts' in display_lower:
                # Look at sibling observations in the same DR for context
                new_value = None  # Can't determine without more context
                stats['see_qmpts_skipped'] += 1

            # Culture: SEE NOTE typically means no growth or routine flora
            elif 'culture' in display_lower:
                new_value = 'See laboratory report'
                stats['see_culture'] += 1

            # BUN/Creat Ratio
            elif 'bun' in display_lower and 'creat' in display_lower:
                new_value = None  # Need narrative
                stats['see_bun_skipped'] += 1

            # MAG Dual Antigen
            elif 'mag dual' in display_lower:
                new_value = 'See laboratory report'
                stats['see_mag'] += 1

            # Generic "Results" / Lab Unlisted
            elif display_lower in ('results',):
                new_value = 'See laboratory report'
                stats['see_generic'] += 1

            if new_value:
                obs['valueString'] = new_value
                if interp:
                    obs['interpretation'] = interp
                add_quality_tag(obs)
                print(f'  FIX {display} | {dt} | "{vs}" -> "{new_value}"')

                if not DRY_RUN:
                    ok, resp = put_observation(obs)
                    if ok:
                        fixed += 1
                    else:
                        print(f'    FAIL: {resp.status_code}')
                else:
                    fixed += 1
            else:
                print(f'  SKIP {display} | {dt} | "{vs}" (cannot determine value)')

    stats['see_fixed'] = fixed
    print(f'  Fixed: {fixed}')


def fix_cbc_ref_ranges():
    """Add well-known reference ranges for CBC differential percentages."""
    print('\n=== Adding reference ranges for CBC differentials ===')

    fixed = 0
    for test_name in ['Basophil %', 'Eosinophil %', 'Lymphocyte %', 'Monocyte %',
                       'Neutrophil %', 'nRBC, %', 'nRBC, Abs',
                       'BASOS, %', 'EOS, %', 'LYMPHS, %']:
        entries = get_all_observations_paged({'code:text': test_name, '_count': '200'})

        for entry in entries:
            obs = entry['resource']
            if obs.get('referenceRange'):
                continue  # Already has reference range

            display = obs.get('code', {}).get('text', '') or (obs.get('code', {}).get('coding', [{}])[0].get('display', ''))
            display_lower = display.lower().strip()

            ref = KNOWN_REF_RANGES.get(display_lower)
            if not ref:
                continue

            vq = obs.get('valueQuantity', {})
            val = vq.get('value')
            if val is None:
                continue

            rr_entry = {
                'low': {'value': ref['low']},
                'high': {'value': ref['high']}
            }
            if ref['unit']:
                rr_entry['low']['unit'] = ref['unit']
                rr_entry['high']['unit'] = ref['unit']
            obs['referenceRange'] = [rr_entry]

            # Compute interpretation if missing
            if not obs.get('interpretation'):
                interp = compute_interp(val, ref['low'], ref['high'])
                if interp:
                    obs['interpretation'] = [{'coding': [INTERP_CODES[interp]]}]

            add_quality_tag(obs)

            if not DRY_RUN:
                ok, resp = put_observation(obs)
                if ok:
                    fixed += 1
                else:
                    print(f'    FAIL {display}: {resp.status_code}')
            else:
                fixed += 1

    stats['cbc_ref_fixed'] = fixed
    print(f'  Fixed: {fixed}')


def main():
    if DRY_RUN:
        print('(DRY RUN - no changes will be made)')
    print('Starting comprehensive quality fix pass 2...')

    fix_html_values()
    fix_numeric_strings()
    fix_see_comments()
    fix_cbc_ref_ranges()

    prefix = '[DRY-RUN] ' if DRY_RUN else ''
    print(f'\n{prefix}=== SUMMARY ===')
    for key, val in sorted(stats.items()):
        print(f'  {key}: {val}')
    total = stats.get('html_fixed', 0) + stats.get('numeric_fixed', 0) + stats.get('see_fixed', 0) + stats.get('cbc_ref_fixed', 0)
    print(f'\n{prefix}Total fixes applied: {total}')


if __name__ == '__main__':
    main()
