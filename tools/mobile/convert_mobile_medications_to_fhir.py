#!/usr/bin/env python3
"""Convert Stanford Medications payload (extracted from the embedded
JS constructor call in /Clinical/Medications HTML — see
ScrapeJobs.stanfordMedicationsFetch) to FHIR R4 MedicationRequest
resources and POST to v2 HAPI.

Input:
  tools/v3/out/stanford-clinical/stanford-medications-<ts>.json
  Shape: { section, fetchedAt, list: {
    CommunityMembers: [{
      Organization: { OrganizationName, IsLocal, ... },
      PrescriptionList: { Prescriptions: [Rx, ...] }
    }, ...]
  }}

Per-Rx mapping:
  MedicationRequest {
    id:           stanford-medrx-<sha1(Rx.ID)>
    identifier:   urn:stanford:myhealth:medication-rx + Rx.ID
    status:       'active' if 'refill-enabled' in ClassList else 'completed'
    intent:       'order'  (patient-reported → 'plan')
    medicationCodeableConcept.text: Rx.Name
    subject:      Patient/eLnGIsbP3w5y…  (Stanford sub-identity)
    authoredOn:   parsed from Rx.StartDate or DateToDisplay ('July 31, 2026' → '2026-07-31')
    requester:    {display: AuthorizingProvider.Name,
                   identifier: {system: urn:stanford:myhealth:provider-id,
                                value: AuthorizingProvider.ID}}
    dosageInstruction[0].text:  Rx.Sig  (e.g. "Take 2 capsules (50 mg total) by mouth daily in the evening")
    dispenseRequest:  from Rx.RefillDetails (WrittenDispenseQuantity + Unit,
                                             DaySupply, NextDispenseDate)
    reportedBoolean:  Rx.IsPatientReported
    note[0].text:     Rx.CriticalMedMessage / OutpatientPauseSummary when non-empty
    meta.tag:  stanford + refill-status + patient-reported flag
  }

Preserves both the currently-active (refill-enabled=9) and historical
(refill-disabled=14) prescriptions — status field distinguishes. The
existing 14 Feb-2026 MedRequests (identifier system
urn:oid:1.2.840.114350.1.13.717...) live in a DIFFERENT identifier
namespace and are left untouched; they represent an earlier snapshot
we can retire later or reconcile.

Per P-PHI-STAYS-LOCAL: reads local disk, POSTs to localhost HAPI.

Usage:
  python3 convert_mobile_medications_to_fhir.py --dry-run
  python3 convert_mobile_medications_to_fhir.py --apply [--wipe]
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
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'tools'))
from portal_registry import get_portal  # noqa: E402

HAPI_BASE = 'http://localhost:8090/fhir'
V3_OUT = REPO_ROOT / 'tools' / 'v3' / 'out'

# Portal-derived constants populated in main() from --portal.
PORTAL = None
CLINICAL_DIR = None
PATIENT_REF = None
SRC_PORTAL_TAG = None
IDENT_MEDREQ = None
IDENT_PROVIDER = None
ID_PREFIX_MEDRX = None

SCRAPER_VERSION = 'mobile-flutter-meds-2026-08-10'

MONTH_NAMES = {
    'january':1, 'february':2, 'march':3, 'april':4, 'may':5, 'june':6,
    'july':7, 'august':8, 'september':9, 'october':10, 'november':11, 'december':12,
}


def det_id(prefix, *parts):
    h = hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


def parse_date_display(s):
    """Parse 'July 31, 2026' → '2026-07-31', or 'M/D/YYYY' → same, else None."""
    if not s: return None
    s = s.strip()
    m = re.match(r'^([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})$', s)
    if m:
        mo = MONTH_NAMES.get(m.group(1).lower())
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def hapi_get(path):
    url = f'{HAPI_BASE}{path}' if not path.startswith('http') else path
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def hapi_post_bundle(bundle):
    req = urllib.request.Request(
        HAPI_BASE,
        data=json.dumps(bundle).encode('utf-8'),
        headers={'Content-Type': 'application/fhir+json'},
        method='POST',
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def wipe_by_identifier_system(resource_type, ident_system):
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


def stanford_meta_tags(rx):
    tags = [
        {'system': 'urn:bina:src-portal',     'code': SRC_PORTAL_TAG},
        {'system': 'urn:bina:src-org',        'code': PORTAL.id},
        {'system': 'urn:bina:scraper-version','code': SCRAPER_VERSION},
    ]
    cls = rx.get('ClassList') or []
    refill_enabled = 'refill-enabled' in cls
    tags.append({'system': 'urn:bina:refill-status',
                 'code': 'refill-enabled' if refill_enabled else 'refill-disabled'})
    if rx.get('IsPatientReported'):
        tags.append({'system': 'urn:bina:reported-by', 'code': 'patient'})
    if rx.get('IsClinicReported'):
        tags.append({'system': 'urn:bina:reported-by', 'code': 'clinic'})
    return tags


def build_medreq(rx):
    stanford_id = (rx.get('ID') or '').strip()
    name = (rx.get('Name') or '').strip()
    if not stanford_id or not name:
        return None
    cls = rx.get('ClassList') or []
    refill_enabled = 'refill-enabled' in cls
    fhir_id = det_id(ID_PREFIX_MEDRX, stanford_id)
    obj = {
        'resourceType': 'MedicationRequest',
        'id': fhir_id,
        'identifier': [{'system': IDENT_MEDREQ, 'value': stanford_id}],
        'status': 'active' if refill_enabled else 'completed',
        'intent': 'plan' if rx.get('IsPatientReported') else 'order',
        'medicationCodeableConcept': {'text': name},
        'subject': {'reference': PATIENT_REF},
        'reportedBoolean': bool(rx.get('IsPatientReported')),
        'meta': {'tag': stanford_meta_tags(rx)},
    }
    authored = parse_date_display(rx.get('StartDate')) \
             or parse_date_display(rx.get('DateToDisplay')) \
             or parse_date_display(rx.get('FormattedDateNoted'))
    if authored:
        obj['authoredOn'] = authored
    # Sig → dosageInstruction
    sig = (rx.get('Sig') or '').strip()
    if sig:
        obj['dosageInstruction'] = [{
            'text': sig,
            'asNeededBoolean': bool(rx.get('IsFrequencyPRN')),
        }]
    # Requester (authorizing prescriber)
    ap = rx.get('AuthorizingProvider') or {}
    prov_name = (ap.get('Name') or '').strip()
    prov_id = (ap.get('ID') or '').strip()
    if prov_name or prov_id:
        req_actor = {}
        if prov_name: req_actor['display'] = prov_name
        if prov_id:   req_actor['identifier'] = {'system': IDENT_PROVIDER, 'value': prov_id}
        obj['requester'] = req_actor
    # DispenseRequest from RefillDetails
    rd = rx.get('RefillDetails') or {}
    qty = rd.get('WrittenDispenseQuantity')
    unit = rd.get('WrittenDispenseUnit')
    day_supply = rd.get('DaySupply')
    next_dispense = rd.get('NextDispenseDate')
    dr = {}
    if qty is not None and unit:
        # Some Epic responses put unit as a dict with 'Title'/'Text' — normalize
        unit_str = unit if isinstance(unit, str) else (
            (isinstance(unit, dict) and (unit.get('Title') or unit.get('Text'))) or None)
        if unit_str:
            try:
                dr['quantity'] = {'value': float(qty), 'unit': unit_str}
            except (ValueError, TypeError):
                pass
    if isinstance(day_supply, (int, float)) and day_supply > 0:
        dr['expectedSupplyDuration'] = {'value': day_supply, 'unit': 'd',
                                        'system': 'http://unitsofmeasure.org', 'code': 'd'}
    if isinstance(next_dispense, str) and next_dispense:
        iso_next = parse_date_display(next_dispense)
        if iso_next:
            dr['validityPeriod'] = {'start': iso_next}
    if dr:
        obj['dispenseRequest'] = dr
    # Note from critical / pause messages
    notes = []
    for k in ('CriticalMedMessage', 'OutpatientPauseSummary'):
        v = (rx.get(k) or '').strip()
        if v: notes.append({'text': v})
    if notes: obj['note'] = notes
    return obj


def put_entry(resource):
    return {
        'fullUrl': f"urn:uuid:{resource['id']}",
        'resource': resource,
        'request': {'method': 'PUT', 'url': f"{resource['resourceType']}/{resource['id']}"},
    }


def main():
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument('--dry-run', action='store_true')
    grp.add_argument('--apply',   action='store_true')
    ap.add_argument('--portal', default='stanford',
                    help='Portal id from mobile/assets/portals/*.json (default: stanford)')
    ap.add_argument('--wipe', action='store_true',
                    help='Delete existing MedicationRequests for this portal before re-import')
    args = ap.parse_args()
    apply = args.apply

    global PORTAL, CLINICAL_DIR, PATIENT_REF, SRC_PORTAL_TAG
    global IDENT_MEDREQ, IDENT_PROVIDER, ID_PREFIX_MEDRX
    PORTAL = get_portal(args.portal)
    CLINICAL_DIR = PORTAL.input_dir(V3_OUT, 'clinical')
    PATIENT_REF = PORTAL.patient_ref
    SRC_PORTAL_TAG = PORTAL.src_portal_tag
    IDENT_MEDREQ    = PORTAL.identifier_system('medication-rx')
    IDENT_PROVIDER  = PORTAL.identifier_system('provider-id')
    ID_PREFIX_MEDRX = f'{PORTAL.id}-medrx'
    print(f'Portal: {PORTAL.name} ({PORTAL.id})')
    print(f'  clinical dir: {CLINICAL_DIR}')

    if args.wipe and apply:
        wipe_by_identifier_system('MedicationRequest', IDENT_MEDREQ)

    f = latest_file(f'{PORTAL.id}-medications-2*.json')
    if not f:
        print(f'No {PORTAL.id}-medications-*.json found under', CLINICAL_DIR)
        sys.exit(1)
    print(f'Reading {f.name}')
    d = json.load(open(f))
    cms = (d.get('list') or {}).get('CommunityMembers') or []
    stats = Counter()
    entries = []
    for cm in cms:
        org = (cm.get('Organization') or {}).get('OrganizationName', '?')
        prescs = ((cm.get('PrescriptionList') or {}).get('Prescriptions') or [])
        print(f"  Org: {org} — {len(prescs)} Rx")
        for rx in prescs:
            mr = build_medreq(rx)
            if not mr:
                stats['skipped-no-id-or-name'] += 1
                continue
            entries.append(put_entry(mr))
            stats[mr['status']] += 1
            if mr.get('reportedBoolean'):
                stats['patient-reported'] += 1

    print('\n=== Plan ===')
    for k, v in sorted(stats.items()):
        print(f'  {k:25s} {v}')
    print(f'  bundle entries total     {len(entries)}')

    if not apply:
        print('\n--dry-run: nothing posted.'); return

    print('\n=== POSTing ===')
    BATCH = 50
    posted, failed = 0, 0
    for i in range(0, len(entries), BATCH):
        chunk = entries[i:i+BATCH]
        bundle = {'resourceType': 'Bundle', 'type': 'transaction', 'entry': chunk}
        try:
            hapi_post_bundle(bundle)
            posted += len(chunk)
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:500]
            print(f'  HTTP-{e.code}: {body}')
            failed += len(chunk)
    print(f'  posted={posted}  failed={failed}')


if __name__ == '__main__':
    main()
