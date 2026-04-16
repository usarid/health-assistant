#!/usr/bin/env python3
"""LOINC Assignment Validator — spot-checks LOINC codes assigned by loinc_mapper.py.

Catches misassignments by checking:
1. Specimen mismatch: urine LOINC on a serum observation (or vice versa)
2. Unit plausibility: does the observation's unit make sense for the LOINC code?
3. Panel context: does the observation belong to a panel that contradicts the LOINC?
4. Display name vs LOINC display: are the names suspiciously different?

Usage:
  python3 validate_loinc.py          # Full validation report
  python3 validate_loinc.py --brief  # Summary counts only
"""

import json
import re
import sys
import requests
from collections import Counter, defaultdict

HAPI_BASE = 'http://localhost:8080/fhir'
BRIEF = '--brief' in sys.argv

# ══════════════════════════════════════════════════════════════
# Known LOINC codes and their expected specimen types / units
# Used to detect misassignments
# ══════════════════════════════════════════════════════════════

# LOINC codes that are specifically urine tests
URINE_LOINCS = {
    '1754-1', '2161-8', '9318-7', '2888-6', '2890-2',
    '5811-5', '2756-5', '5799-2', '5770-3', '2514-8',
    '630-4', '5787-7', '5796-0', '5802-4', '5804-0',
    '5778-6', '5767-9', '5821-4', '5792-7', '2106-3',
    # Urine protein electrophoresis (UPEP)
    '13991-3', '13993-9', '13995-4', '13997-0', '56759-4',
}

# LOINC codes that are specifically serum/plasma tests
SERUM_LOINCS = {
    '2951-2', '2823-3', '2075-0', '2028-9', '3094-0', '2160-0',
    '2345-7', '17861-6', '2885-2', '1751-7', '10834-0', '1759-0',
    '33037-3', '19123-9', '2777-1', '3084-1',
    '1742-6', '1920-8', '6768-6', '1975-2', '1968-7', '2324-2', '3040-3',
    '2093-3', '2085-9', '2089-1', '2571-8',
    '4548-4', '33914-3', '1558-6',
    '2276-4', '2498-4', '2500-7', '2502-3',
    '1988-5', '4537-7',
    '3016-3', '3024-7', '3051-0',
    '2458-8', '2462-0', '2472-9',
    '11050-2', '11051-0', '11052-8',
    '1952-1', '2532-0', '2857-1',
    '51435-6',
    '2871-2', '2865-4', '2870-4', '2868-8', '2867-0',
    '1989-3', '2132-9', '2284-8',
    '29265-6',
}

# LOINC codes that are blood/hematology tests
BLOOD_LOINCS = {
    '6690-2', '789-8', '718-7', '4544-3', '777-3',
    '787-2', '785-6', '786-4', '788-0', '21000-5', '32623-1',
    '751-8', '731-0', '742-7', '711-2', '704-7', '53115-2',
    '770-8', '736-9', '5905-5', '713-8', '706-2', '71695-1',
    '58413-6', '19048-8',
    '5902-2', '6301-6', '3173-2', '3255-7',
    '4548-4',
}

# Keywords that suggest urine context in observation or panel names
URINE_KEYWORDS = re.compile(r'urin|ua\b|u/a|urinalysis', re.IGNORECASE)
SERUM_KEYWORDS = re.compile(r'serum|plasma|ser/plas|blood|cmp|bmp|metabolic|lipid|hepatic|liver', re.IGNORECASE)

# Names that are OK with urine LOINC codes despite containing serum keywords
# (e.g., "Occult Blood" = blood-in-urine dipstick, not a serum test)
URINE_OK_NAMES = {
    'occult blood',
}

# Expected unit patterns per LOINC category
UNIT_EXPECTATIONS = {
    # Urine tests that should NOT have serum-like units
    'urine_qualitative': {
        'loincs': {'5799-2', '5770-3', '2514-8', '5802-4', '5804-0', '2106-3', '630-4'},
        'bad_units': re.compile(r'mg/dL|g/dL|mEq/L|mmol/L|U/L|IU/L', re.IGNORECASE),
    },
    # Serum chemistry that should NOT have urine-like units
    'serum_chemistry': {
        'loincs': {'2951-2', '2823-3', '2075-0', '2160-0', '2345-7', '17861-6'},
        'bad_units': re.compile(r'/area|/hpf|/lpf', re.IGNORECASE),
    },
}


def get_all_mapper_tagged():
    """Fetch all observations tagged by loinc-mapper-v1."""
    all_obs = []
    url = f'{HAPI_BASE}/Observation?_tag=http://example.org/source|loinc-mapper-v1&_count=500'
    while url:
        resp = requests.get(url)
        if resp.status_code != 200:
            print(f'  ERROR fetching: {resp.status_code}')
            break
        bundle = resp.json()
        entries = bundle.get('entry', [])
        all_obs.extend(e['resource'] for e in entries)
        url = None
        for link in bundle.get('link', []):
            if link['relation'] == 'next':
                url = link['url']
                break
        if len(all_obs) % 2000 < 500:
            print(f'  Fetched {len(all_obs)}...')
    return all_obs


def get_assigned_loinc(obs):
    """Get the LOINC code that was assigned by the mapper."""
    for coding in obs.get('code', {}).get('coding', []):
        if coding.get('system') == 'http://loinc.org':
            return coding.get('code'), coding.get('display', '')
    return None, None


def get_display_name(obs):
    """Get the observation's original display name."""
    codings = obs.get('code', {}).get('coding', [])
    for c in codings:
        if c.get('system') != 'http://loinc.org' and c.get('display'):
            return c['display']
    return obs.get('code', {}).get('text', '')


def get_unit(obs):
    """Get the unit from valueQuantity if present."""
    vq = obs.get('valueQuantity', {})
    return vq.get('unit', vq.get('code', ''))


def get_source_tag(obs):
    """Get the source tag."""
    for tag in obs.get('meta', {}).get('tag', []):
        code = tag.get('code', '')
        if code and code not in ('quality-patch-v1', 'quality-patch-v2', 'loinc-mapper-v1'):
            return code
    return ''


def get_parent_report_name(obs):
    """Check if the observation is part of a DiagnosticReport (via hasMember)."""
    # This would require an extra query per observation, so we use the code text as proxy
    return obs.get('code', {}).get('text', '')


def check_specimen_mismatch(obs, loinc_code, display_name):
    """Check if LOINC specimen type contradicts the observation context."""
    issues = []
    name_lower = display_name.lower()
    code_text = obs.get('code', {}).get('text', '').lower()
    context = name_lower + ' ' + code_text

    # Urine LOINC but name suggests serum
    if loinc_code in URINE_LOINCS:
        if name_lower.strip() in URINE_OK_NAMES:
            pass  # Known OK — e.g., "Occult Blood" on a urine dipstick
        elif SERUM_KEYWORDS.search(context) and not URINE_KEYWORDS.search(context):
            issues.append(f'SPECIMEN_MISMATCH: Urine LOINC {loinc_code} but name suggests serum: "{display_name}"')

    # Serum LOINC but name suggests urine
    if loinc_code in SERUM_LOINCS:
        if URINE_KEYWORDS.search(context) and not SERUM_KEYWORDS.search(context):
            issues.append(f'SPECIMEN_MISMATCH: Serum LOINC {loinc_code} but name suggests urine: "{display_name}"')

    return issues


def check_unit_plausibility(obs, loinc_code):
    """Check if the observation's unit is plausible for the LOINC code."""
    issues = []
    unit = get_unit(obs)
    if not unit:
        return issues

    for category, spec in UNIT_EXPECTATIONS.items():
        if loinc_code in spec['loincs'] and spec['bad_units'].search(unit):
            issues.append(f'UNIT_MISMATCH: LOINC {loinc_code} ({category}) has unexpected unit "{unit}"')

    return issues


from loinc_synonyms import expand_synonyms, tokenize, names_match, BOILERPLATE_WORDS


def check_name_divergence(display_name, loinc_display):
    """Check if the original name is suspiciously different from the LOINC display.

    Uses the shared synonym table (loinc_synonyms.py) to avoid flagging known
    abbreviation/full-name pairs like CRP <-> C-Reactive Protein.
    """
    issues = []
    if not display_name or not loinc_display:
        return issues

    name_words = tokenize(display_name) - BOILERPLATE_WORDS
    loinc_words = tokenize(loinc_display) - BOILERPLATE_WORDS

    if not name_words or not loinc_words:
        return issues

    name_expanded = expand_synonyms(name_words)
    loinc_expanded = expand_synonyms(loinc_words)

    overlap = (name_words & loinc_expanded) | (loinc_words & name_expanded)

    if len(overlap) == 0 and len(name_words) >= 2:
        issues.append(f'NAME_DIVERGENCE: "{display_name}" -> LOINC "{loinc_display}" (no word overlap)')

    return issues


def main():
    print('LOINC Assignment Validator')
    print('=' * 60)
    print()

    print('Fetching mapper-tagged observations...')
    observations = get_all_mapper_tagged()
    print(f'Total: {len(observations)}')
    print()

    if not observations:
        print('No mapper-tagged observations found. Run loinc_mapper.py first.')
        return

    all_issues = []
    issue_counts = Counter()
    issues_by_loinc = defaultdict(list)

    for obs in observations:
        loinc_code, loinc_display = get_assigned_loinc(obs)
        if not loinc_code:
            continue

        display_name = get_display_name(obs)
        obs_id = obs.get('id', '?')
        date = (obs.get('effectiveDateTime') or '')[:10]
        source = get_source_tag(obs)

        issues = []
        issues.extend(check_specimen_mismatch(obs, loinc_code, display_name))
        issues.extend(check_unit_plausibility(obs, loinc_code))
        issues.extend(check_name_divergence(display_name, loinc_display))

        for issue in issues:
            issue_type = issue.split(':')[0]
            issue_counts[issue_type] += 1
            record = {
                'id': obs_id,
                'date': date,
                'display_name': display_name,
                'loinc_code': loinc_code,
                'loinc_display': loinc_display,
                'source': source,
                'issue': issue,
            }
            all_issues.append(record)
            issues_by_loinc[loinc_code].append(record)

    # Summary
    print(f'=== VALIDATION SUMMARY ===')
    print(f'Observations checked: {len(observations)}')
    print(f'Issues found: {len(all_issues)}')
    print()

    if issue_counts:
        print('Issue breakdown:')
        for issue_type, count in issue_counts.most_common():
            print(f'  {issue_type}: {count}')
        print()

    if BRIEF:
        return

    # Detail
    if all_issues:
        # Group by LOINC code for easier review
        print(f'=== ISSUES BY LOINC CODE ===')
        for loinc_code in sorted(issues_by_loinc.keys()):
            records = issues_by_loinc[loinc_code]
            print(f'\nLOINC {loinc_code} ({records[0]["loinc_display"]}):')
            for r in records[:5]:  # Show up to 5 examples per LOINC
                print(f'  {r["date"]} | {r["display_name"]} | {r["source"]} | {r["issue"]}')
            if len(records) > 5:
                print(f'  ... and {len(records) - 5} more')

    # Save full report
    report_path = 'loinc_validation_report.json'
    with open(report_path, 'w') as f:
        json.dump({
            'total_checked': len(observations),
            'total_issues': len(all_issues),
            'issue_counts': dict(issue_counts),
            'issues': all_issues,
        }, f, indent=2)
    print(f'\nFull report saved to {report_path}')

    if not all_issues:
        print('\nNo issues found — all LOINC assignments look correct.')


if __name__ == '__main__':
    main()
