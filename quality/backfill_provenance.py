#!/usr/bin/env python3
"""Backfill provenance tags on all existing Observations in HAPI.

Scans every Observation and stamps it with:
  - urn:phv:tag  raw-name:<original display name>
  - urn:phv:tag  pipeline:v<N>

This is a one-time migration script. It's safe to re-run — it skips
observations that already have a raw-name tag.

Usage:
  python3 backfill_provenance.py --dry-run   # Preview
  python3 backfill_provenance.py             # Apply
"""

import sys
import requests

sys.path.insert(0, '.')
from fhir_utils import add_provenance_tag, PHV_TAG_SYSTEM, PIPELINE_VERSION

HAPI_BASE = 'http://localhost:8080/fhir'
DRY_RUN = '--dry-run' in sys.argv


def get_raw_name(obs):
    """Extract the best 'original test name' from an observation."""
    # 1. code.text is the original name set during conversion
    code_text = obs.get('code', {}).get('text', '')
    if code_text:
        return code_text

    # 2. First non-LOINC coding display
    for c in obs.get('code', {}).get('coding', []):
        if c.get('system') != 'http://loinc.org' and c.get('display'):
            return c['display']

    # 3. First coding display (even if LOINC — better than nothing)
    for c in obs.get('code', {}).get('coding', []):
        if c.get('display'):
            return c['display']

    return ''


def get_source_tag(obs):
    """Extract the existing source institution tag code."""
    for t in obs.get('meta', {}).get('tag', []):
        if t.get('system') == 'http://example.org/source' and t.get('code', '') != 'loinc-mapper-v1':
            code = t.get('code', '')
            if 'quality-patch' not in code:
                return code
    return ''


def already_has_raw_name(obs):
    """Check if observation already has a phv:raw-name tag."""
    for t in obs.get('meta', {}).get('tag', []):
        if t.get('system') == PHV_TAG_SYSTEM and t.get('code', '').startswith('raw-name:'):
            return True
    return False


def main():
    print(f'Provenance Backfill — Pipeline v{PIPELINE_VERSION}')
    if DRY_RUN:
        print('(DRY RUN — no changes will be written)\n')
    else:
        print()

    # Paginate through all observations
    all_obs = []
    url = f'{HAPI_BASE}/Observation?_count=500&_sort=-_lastUpdated'
    page = 0
    while url:
        resp = requests.get(url)
        if resp.status_code != 200:
            print(f'Error fetching page {page}: {resp.status_code}')
            break
        bundle = resp.json()
        all_obs.extend(entry['resource'] for entry in bundle.get('entry', []))
        page += 1
        if page % 10 == 0:
            print(f'  Fetched {len(all_obs)} observations...')
        url = None
        for link in bundle.get('link', []):
            if link['relation'] == 'next':
                url = link['url']
                break

    print(f'Total observations: {len(all_obs)}')

    skipped = 0
    updated = 0
    failed = 0
    no_name = 0

    for obs in all_obs:
        # Skip if already backfilled
        if already_has_raw_name(obs):
            skipped += 1
            continue

        raw_name = get_raw_name(obs)
        if not raw_name:
            no_name += 1
            continue

        # Add provenance tags
        add_provenance_tag(obs, f'raw-name:{raw_name}', raw_name)
        add_provenance_tag(obs, f'pipeline:v{PIPELINE_VERSION}',
                           f'PHV Pipeline v{PIPELINE_VERSION}')

        # Add meta.source from existing institution tag if not already set
        source_tag = get_source_tag(obs)
        if source_tag and not obs.get('meta', {}).get('source'):
            obs.setdefault('meta', {})['source'] = f'institution:{source_tag}'

        if DRY_RUN:
            updated += 1
            continue

        # PUT back to HAPI
        obs_id = obs.get('id')
        resp = requests.put(
            f'{HAPI_BASE}/Observation/{obs_id}',
            json=obs,
            headers={'Content-Type': 'application/fhir+json'}
        )
        if resp.status_code in (200, 201):
            updated += 1
        else:
            print(f'  FAIL {obs_id}: {resp.status_code}')
            failed += 1

        if updated % 500 == 0 and updated > 0:
            print(f'  Updated {updated} observations...')

    prefix = '[DRY-RUN] ' if DRY_RUN else ''
    print(f'\n{prefix}=== RESULTS ===')
    print(f'  Already had provenance: {skipped}')
    print(f'  Updated with raw-name:  {updated}')
    print(f'  No name available:      {no_name}')
    if failed:
        print(f'  Failed:                 {failed}')
    print(f'  Total:                  {len(all_obs)}')


if __name__ == '__main__':
    main()
