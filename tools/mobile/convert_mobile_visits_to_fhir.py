#!/usr/bin/env python3
"""Convert an Epic MyChart /Visits/VisitsList/LoadPast (or LoadUpcoming)
response into FHIR R4 Encounter resources and POST to v2 HAPI.

Input file: tools/v3/out/<portal>-clinical/<portal>-visits-past-<ts>.json
            tools/v3/out/<portal>-clinical/<portal>-visits-upcoming-<ts>.json
Shape: { List: { <orgId>: { Organization: {...}, List: [ visit-object, ... ] } } }
       Each visit-object has Csn/Id, Instant (/Date(millis)/), Date, Time,
       Organization, VisitTypeName, EncounterType, PrimaryProvider*,
       PrimaryDepartment, ChiefComplaint, various status flags.

Notable: UCSF's Happy Together surfaces visits from 8+ organizations
through one API call (UCSF + Sutter + Providence + John Muir + Mass
General Brigham + Mayo + Altais + MSKCC). We tag each encounter with
the source portal (ucsf.mychart) AND with the origin organization's
name via `urn:bina:src-org-name`, so downstream can distinguish
"UCSF's own visits" from "MSKCC visits UCSF sees via Happy Together."

Per P-PHI-STAYS-LOCAL: reads local disk, POSTs to localhost HAPI.
"""

import argparse
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
sys.path.insert(0, str(REPO_ROOT / 'tools'))
from portal_registry import get_portal  # noqa: E402

HAPI_BASE = 'http://localhost:8090/fhir'
V3_OUT = REPO_ROOT / 'tools' / 'v3' / 'out'

PORTAL = None
CLINICAL_DIR = None
PATIENT_REF = None
SRC_PORTAL_TAG = None
IDENT_ENCOUNTER = None
ID_PREFIX_ENC = None

SCRAPER_VERSION = 'mobile-flutter-visits-2026-08-13'

# Epic EncounterType → FHIR Encounter.class mapping. Rough — Epic's
# encounter-type enum is portal-specific; we cover the common cases
# and fall back to AMB (ambulatory) for anything else.
CLASS_MAP = {
    1: ('IMP', 'inpatient encounter'),
    2: ('EMER', 'emergency'),
    3: ('AMB', 'ambulatory'),
    4: ('AMB', 'ambulatory'),   # office visit
    5: ('HH',  'home health'),
}


def det_id(prefix, *parts):
    h = hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


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


def parse_instant(s):
    """Parse Epic's /Date(1786151700000)/ format → ISO datetime.
    Falls back to None on parse failure."""
    if not s: return None
    m = re.match(r'^/Date\((\d+)([+-]\d{4})?\)/$', str(s))
    if not m: return None
    ms = int(m.group(1))
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def build_encounter(visit, native_org_name):
    """One visit-object → FHIR Encounter. Returns None if we can't get
    a stable id."""
    csn = (visit.get('Csn') or visit.get('Id') or '').strip()
    if not csn:
        return None

    fhir_id = det_id(ID_PREFIX_ENC, csn)
    enc_type_code = visit.get('EncounterType')
    cls_code, cls_display = CLASS_MAP.get(enc_type_code, ('AMB', 'ambulatory'))

    is_canceled = bool(visit.get('IsCanceled'))
    is_no_show = bool(visit.get('IsNoShow'))
    is_past = bool(visit.get('IsPastVisit'))
    status = ('cancelled' if is_canceled
              else 'entered-in-error' if is_no_show
              else 'finished' if is_past
              else 'planned')

    obj = {
        'resourceType': 'Encounter',
        'id': fhir_id,
        'identifier': [{'system': IDENT_ENCOUNTER, 'value': csn}],
        'status': status,
        'class': {
            'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
            'code': cls_code,
            'display': cls_display,
        },
        'subject': {'reference': PATIENT_REF},
        'meta': {'tag': [
            {'system': 'urn:bina:src-portal',     'code': SRC_PORTAL_TAG},
            {'system': 'urn:bina:src-org',        'code': PORTAL.id},
            {'system': 'urn:bina:scraper-version','code': SCRAPER_VERSION},
        ]},
    }

    # VisitTypeName → type.text (e.g. "Procedure", "Office Visit").
    vt = (visit.get('VisitTypeName') or '').strip()
    if vt:
        obj['type'] = [{'text': vt}]

    # Origin organization (via Happy Together). Not the FHIR
    # serviceProvider (that's a Reference to an Organization resource,
    # which we don't create); use a tag instead so downstream can
    # distinguish e.g. Mayo-via-UCSF from UCSF-native.
    if native_org_name:
        obj['meta']['tag'].append(
            {'system': 'urn:bina:src-org-name', 'code': native_org_name[:60]})

    # Period from Epic's /Date(millis)/. Falls back to nothing if unparseable.
    start = parse_instant(visit.get('Instant'))
    end = parse_instant(visit.get('DischargeDate'))
    if start or end:
        obj['period'] = {}
        if start: obj['period']['start'] = start
        if end:   obj['period']['end'] = end

    # PrimaryProvider / PrimaryProviderName → participant (Practitioner
    # is display-only; we don't create Practitioner resources here).
    prov_name = (visit.get('PrimaryProviderName') or '').strip()
    if not prov_name:
        prim = visit.get('PrimaryProvider') or {}
        if isinstance(prim, dict):
            prov_name = (prim.get('Name') or '').strip()
    if prov_name:
        obj['participant'] = [{
            'individual': {'display': prov_name},
        }]

    # PrimaryDepartment.Name → location display.
    dept = visit.get('PrimaryDepartment') or {}
    dept_name = (dept.get('Name') if isinstance(dept, dict) else '').strip() if dept else ''
    if dept_name:
        obj['location'] = [{'location': {'display': dept_name}}]

    # ChiefComplaint → reasonCode
    cc = (visit.get('ChiefComplaint') or '').strip()
    if cc:
        obj['reasonCode'] = [{'text': cc}]

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
    ap.add_argument('--portal', default='ucsf',
                    help='Portal id (default: ucsf; Stanford visits ingest via a separate path)')
    ap.add_argument('--wipe', action='store_true',
                    help='Delete all existing Encounters with this portal\'s system before re-import')
    args = ap.parse_args()
    apply = args.apply

    global PORTAL, CLINICAL_DIR, PATIENT_REF, SRC_PORTAL_TAG
    global IDENT_ENCOUNTER, ID_PREFIX_ENC
    PORTAL = get_portal(args.portal)
    CLINICAL_DIR = PORTAL.input_dir(V3_OUT, 'clinical')
    PATIENT_REF = PORTAL.patient_ref
    SRC_PORTAL_TAG = PORTAL.src_portal_tag
    IDENT_ENCOUNTER = PORTAL.identifier_system('encounter')
    ID_PREFIX_ENC = f'{PORTAL.id}-enc'
    print(f'Portal: {PORTAL.name} ({PORTAL.id})')
    print(f'  clinical dir: {CLINICAL_DIR}')

    if args.wipe and apply:
        wipe_by_identifier_system('Encounter', IDENT_ENCOUNTER)

    stats = Counter()
    bundle_entries = []

    for kind in ('past', 'upcoming'):
        f = latest_file(f'{PORTAL.id}-visits-{kind}-*.json')
        if not f:
            print(f'  (no {PORTAL.id}-visits-{kind}-*.json found)')
            continue
        d = json.load(open(f))
        body = d.get('list', d)   # writeClinicalList wraps in {section, fetchedAt, list}
        by_org = (body.get('List') or {}) if isinstance(body, dict) else {}
        print(f'\n{kind}: {len(by_org)} orgs  ({f.name})')
        for org_id, bucket in by_org.items():
            if not isinstance(bucket, dict): continue
            org_name = (bucket.get('Organization') or {}).get('OrganizationName', '')
            visits = bucket.get('List') or []
            for v in visits:
                if not isinstance(v, dict): continue
                enc = build_encounter(v, org_name)
                if enc:
                    bundle_entries.append(put_entry(enc))
                    stats[f'encounters-{kind}'] += 1
                else:
                    stats['skipped-no-csn'] += 1

    # Dedup — some CSNs appear across list buckets (rare but happens).
    seen = set()
    deduped = []
    for e in bundle_entries:
        rid = (e.get('resource') or {}).get('id')
        if rid in seen:
            stats['deduped-in-bundle'] += 1
            continue
        seen.add(rid)
        deduped.append(e)
    bundle_entries = deduped

    print(f'\n=== Plan ===')
    for k, v in sorted(stats.items()):
        print(f'  {k:25s} {v}')
    print(f'  bundle entries total      {len(bundle_entries)}')

    if not apply:
        print('\n--dry-run: nothing posted.')
        return

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
