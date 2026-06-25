#!/usr/bin/env python3
"""Convert Stanford clinical-triad JSON (Allergies / Immunizations /
HealthIssues) into FHIR R4 resources and POST to v2 HAPI.

Inputs (one file per section, written by the Phase 5a mobile fetcher):
  tools/v3/out/stanford-clinical/
    stanford-allergies-<ts>.json       — top.list.DataList[].AllergyItem
    stanford-immunizations-<ts>.json   — top.list.OrganizationImmunizationList[].OrgImmunizations[]
    stanford-healthissues-<ts>.json    — top.list.DataList[].HealthIssueItem

Mappings (deterministic IDs, idempotent re-runs):

  AllergyIntolerance {
    id:           stanford-allerg-<sha1(stanford-id)>
    identifier:   urn:stanford:myhealth:allergy + AllergyItem.ID
    patient:      Patient/eLnGIsbP3w5y… (Stanford sub-identity, see patient_consolidation)
    clinicalStatus: active (Stanford lists are active by definition; inactive entries
                    show via the IsInactive on the row, which we currently don't see)
    code.text:    AllergyItem.Name
    category:     mapped from PatientFriendlyType.Title (Drug→medication, Food→food,
                  Environmental→environment, …)
    criticality:  IsSevere=true → high
    reaction:     [{manifestation: {text: Reaction.Title}, ...}] per AllergyItem.ReactionList
    recordedDate: parsed from FormattedDateNoted (M/D/YYYY)
  }

  Immunization (one per administration date, since FHIR R4 = one dose-event/resource) {
    id:           stanford-imm-<sha1(stanford-id + date)>
    identifier:   urn:stanford:myhealth:immunization + "<Id>:<date>"
    status:       completed
    vaccineCode.text:  OrgImmunization.Name
    patient:      Patient/eLnGIsbP3w5y…
    occurrenceDateTime: parsed from one FormattedAdministeredDates[i]
  }

  Condition (from HealthIssueItem) {
    id:           stanford-cond-<sha1(stanford-id)>
    identifier:   urn:stanford:myhealth:condition + HealthIssueItem.ID
    clinicalStatus: active
    verificationStatus: confirmed
    code.text:    HealthIssueItem.Name
    subject:      Patient/eLnGIsbP3w5y…
    recordedDate: parsed from FormattedDateNoted
  }

Per P-PHI-STAYS-LOCAL: reads local disk, POSTs to localhost HAPI. Nothing
leaves the host.

Usage:
  python3 convert_mobile_clinical_to_fhir.py             # apply
  python3 convert_mobile_clinical_to_fhir.py --dry-run   # build, no POST
  python3 convert_mobile_clinical_to_fhir.py --wipe      # delete existing
                                                          # Stanford-tagged resources
                                                          # before re-import
"""

import argparse
import glob
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLINICAL_DIR = REPO_ROOT / 'tools' / 'v3' / 'out' / 'stanford-clinical'
HAPI_BASE = 'http://localhost:8090/fhir'

# Stanford sub-identity Patient (from patient_consolidation work — commit 2334015)
STANFORD_PATIENT_REF = 'Patient/eLnGIsbP3w5yfjWlLiwicjQI2Qqhzi1Zkub17YmdqWg03'

IDENT_ALLERGY      = 'urn:stanford:myhealth:allergy'
IDENT_IMMUNIZATION = 'urn:stanford:myhealth:immunization'
IDENT_CONDITION    = 'urn:stanford:myhealth:condition'

SCRAPER_VERSION = 'mobile-flutter-clinical-2026-06-25'

ALLERGY_CATEGORY_MAP = {
    'Drug':          'medication',
    'Drug Class':    'medication',
    'Medication':    'medication',
    'Food':          'food',
    'Environmental': 'environment',
    'Biologic':      'biologic',
}


def det_id(prefix, *parts):
    h = hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


def parse_mdy(s):
    """Parse 'M/D/YYYY' → 'YYYY-MM-DD' or None."""
    if not s: return None
    m = re.match(r'^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$', s)
    if not m: return None
    mo, dy, yr = m.groups()
    return f'{yr}-{int(mo):02d}-{int(dy):02d}'


def hapi_get(path):
    url = f'{HAPI_BASE}{path}' if not path.startswith('http') else path
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def hapi_post_bundle(bundle):
    req = urllib.request.Request(
        HAPI_BASE,
        data=json.dumps(bundle).encode('utf-8'),
        headers={'Content-Type': 'application/fhir+json',
                 'Accept': 'application/fhir+json'},
        method='POST',
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def wipe_by_identifier_system(resource_type, ident_system):
    """Delete every resource of the given type whose identifier carries
    our urn:stanford:myhealth:* system."""
    print(f'Wiping {resource_type} with {ident_system}…')
    sys_param = urllib.parse.quote(f'{ident_system}|', safe=':')
    ids = []
    next_url = f'/{resource_type}?identifier={sys_param}&_elements=id&_count=200'
    while next_url:
        b = hapi_get(next_url)
        for e in b.get('entry') or []:
            r = e.get('resource') or {}
            if r.get('id'): ids.append(r['id'])
        next_url = None
        for link in b.get('link') or []:
            if link.get('relation') == 'next' and link.get('url'):
                next_url = link['url']; break
    print(f'  found {len(ids)} to delete')
    for i in range(0, len(ids), 100):
        chunk = ids[i:i+100]
        bundle = {
            'resourceType': 'Bundle', 'type': 'transaction',
            'entry': [{'request': {'method': 'DELETE', 'url': f'{resource_type}/{rid}'}} for rid in chunk],
        }
        hapi_post_bundle(bundle)


def latest_file(pattern):
    matches = sorted(CLINICAL_DIR.glob(pattern))
    return matches[-1] if matches else None


def build_allergy(item):
    """AllergyItem → AllergyIntolerance."""
    stanford_id = (item.get('ID') or '').strip()
    if not stanford_id: return None
    name = (item.get('Name') or '').strip()
    pft  = (item.get('PatientFriendlyType') or {}).get('Title') or ''
    is_severe = bool(item.get('IsSevere'))
    reactions = item.get('ReactionList') or []
    date = parse_mdy(item.get('FormattedDateNoted'))

    fhir_id = det_id('stanford-allerg', stanford_id)
    obj = {
        'resourceType': 'AllergyIntolerance',
        'id': fhir_id,
        'identifier': [{'system': IDENT_ALLERGY, 'value': stanford_id}],
        'clinicalStatus': {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical',
                'code': 'active', 'display': 'Active',
            }],
        },
        'verificationStatus': {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/allergyintolerance-verification',
                'code': 'confirmed', 'display': 'Confirmed',
            }],
        },
        'type': 'allergy',
        'code': {'text': name},
        'patient': {'reference': STANFORD_PATIENT_REF},
        'meta': {'tag': [
            {'system': 'urn:bina:src-portal',     'code': 'stanford.mychart'},
            {'system': 'urn:bina:src-org',        'code': 'stanford'},
            {'system': 'urn:bina:scraper-version','code': SCRAPER_VERSION},
        ]},
    }
    cat = ALLERGY_CATEGORY_MAP.get(pft)
    if cat: obj['category'] = [cat]
    if is_severe: obj['criticality'] = 'high'
    if date: obj['recordedDate'] = date
    if reactions:
        obj['reaction'] = [{
            'manifestation': [{'text': (r.get('Title') or '').strip()}],
        } for r in reactions if (r.get('Title') or '').strip()]
        if not obj['reaction']: del obj['reaction']
    return obj


def build_immunizations(org_imm_item):
    """OrgImmunization → 1..N Immunization (one per administration date)."""
    stanford_id = (org_imm_item.get('Id') or '').strip()
    name = (org_imm_item.get('Name') or '').strip()
    dates = org_imm_item.get('FormattedAdministeredDates') or []
    if not stanford_id or not name: return []
    out = []
    for raw_date in dates:
        iso = parse_mdy(raw_date)
        if not iso: continue
        fhir_id = det_id('stanford-imm', stanford_id, iso)
        out.append({
            'resourceType': 'Immunization',
            'id': fhir_id,
            'identifier': [{'system': IDENT_IMMUNIZATION, 'value': f'{stanford_id}:{iso}'}],
            'status': 'completed',
            'vaccineCode': {'text': name},
            'patient': {'reference': STANFORD_PATIENT_REF},
            'occurrenceDateTime': iso,
            'meta': {'tag': [
                {'system': 'urn:bina:src-portal',     'code': 'stanford.mychart'},
                {'system': 'urn:bina:src-org',        'code': 'stanford'},
                {'system': 'urn:bina:scraper-version','code': SCRAPER_VERSION},
            ]},
        })
    return out


def build_condition(item):
    """HealthIssueItem → Condition."""
    stanford_id = (item.get('ID') or '').strip()
    name = (item.get('Name') or '').strip()
    if not stanford_id or not name: return None
    date = parse_mdy(item.get('FormattedDateNoted'))
    fhir_id = det_id('stanford-cond', stanford_id)
    obj = {
        'resourceType': 'Condition',
        'id': fhir_id,
        'identifier': [{'system': IDENT_CONDITION, 'value': stanford_id}],
        'clinicalStatus': {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-clinical',
                'code': 'active', 'display': 'Active',
            }],
        },
        'verificationStatus': {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-ver-status',
                'code': 'confirmed', 'display': 'Confirmed',
            }],
        },
        'code': {'text': name},
        'subject': {'reference': STANFORD_PATIENT_REF},
        'meta': {'tag': [
            {'system': 'urn:bina:src-portal',     'code': 'stanford.mychart'},
            {'system': 'urn:bina:src-org',        'code': 'stanford'},
            {'system': 'urn:bina:scraper-version','code': SCRAPER_VERSION},
        ]},
    }
    if date: obj['recordedDate'] = date
    return obj


def put_entry(resource):
    return {
        'fullUrl': f"urn:uuid:{resource['id']}",
        'resource': resource,
        'request': {'method': 'PUT', 'url': f"{resource['resourceType']}/{resource['id']}"},
    }


def main():
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group(required=False)
    grp.add_argument('--dry-run', action='store_true')
    grp.add_argument('--apply',   action='store_true')
    ap.add_argument('--wipe', action='store_true', help='Delete existing stanford-tagged resources before re-import')
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        print('Specify --dry-run or --apply'); sys.exit(2)
    apply = args.apply

    if args.wipe and apply:
        wipe_by_identifier_system('AllergyIntolerance', IDENT_ALLERGY)
        wipe_by_identifier_system('Immunization',      IDENT_IMMUNIZATION)
        wipe_by_identifier_system('Condition',         IDENT_CONDITION)

    # --- Build resources ----------------------------------------------
    stats = Counter()
    bundle_entries = []

    # ALLERGIES
    f = latest_file('stanford-allergies-*.json')
    if f:
        d = json.load(open(f))
        items = (d['list'].get('DataList') or [])
        print(f'\nAllergies: {len(items)} rows  ({f.name})')
        for row in items:
            ai = row.get('AllergyItem') or {}
            r = build_allergy(ai)
            if r:
                bundle_entries.append(put_entry(r))
                stats['allergies'] += 1
    else:
        print('  (no stanford-allergies-*.json found)')

    # IMMUNIZATIONS
    f = latest_file('stanford-immunizations-*.json')
    if f:
        d = json.load(open(f))
        orgs = (d['list'].get('OrganizationImmunizationList') or [])
        print(f'\nImmunizations: {sum(len(o.get("OrgImmunizations") or []) for o in orgs)} rows across {len(orgs)} orgs  ({f.name})')
        for org in orgs:
            for imm in (org.get('OrgImmunizations') or []):
                for r in build_immunizations(imm):
                    bundle_entries.append(put_entry(r))
                    stats['immunization-doses'] += 1
                stats['immunization-rows'] += 1
    else:
        print('  (no stanford-immunizations-*.json found)')

    # CONDITIONS (HealthIssues)
    f = latest_file('stanford-healthissues-*.json')
    if f:
        d = json.load(open(f))
        items = (d['list'].get('DataList') or [])
        print(f'\nHealthIssues (Condition): {len(items)} rows  ({f.name})')
        for row in items:
            hi = row.get('HealthIssueItem') or {}
            r = build_condition(hi)
            if r:
                bundle_entries.append(put_entry(r))
                stats['conditions'] += 1
    else:
        print('  (no stanford-healthissues-*.json found)')

    print(f'\n=== Plan ===')
    for k, v in sorted(stats.items()):
        print(f'  {k:25s} {v}')
    print(f'  bundle entries total      {len(bundle_entries)}')

    if not apply:
        print('\n--dry-run: nothing posted.')
        return

    # --- POST in chunks of 50 ----------------------------------------
    print('\n=== POSTing ===')
    BATCH = 50
    posted, failed = 0, 0
    for i in range(0, len(bundle_entries), BATCH):
        chunk = bundle_entries[i:i+BATCH]
        bundle = {'resourceType': 'Bundle', 'type': 'transaction', 'entry': chunk}
        try:
            hapi_post_bundle(bundle)
            posted += len(chunk)
        except urllib.error.HTTPError as e:
            failed += len(chunk)
            body = e.read().decode('utf-8', errors='replace')[:300]
            print(f'  HTTP-{e.code}: {body}')
    print(f'  posted={posted}  failed={failed}')


if __name__ == '__main__':
    main()
