#!/usr/bin/env python3
"""Convert an Epic MyChart /api/test-results/GetList response into FHIR
R4 DiagnosticReport SHELLS (metadata only, no numeric values yet) and
POST to v2 HAPI.

The list-view endpoint returns ~250 lab orders with name, provider,
date, and organization — but zero `resultComponents` inline (all have
hasAllDetails=false). To get the actual numeric values (glucose 95,
HbA1c 7.2, etc.), each lab requires a separate GetDetails fetch —
implemented in mobile/lib/scrape/scrape_jobs.dart::epicLabDetail,
same URL pattern as Stanford. That per-item fetch loop is
follow-up work; this converter gets the metadata skeletons in first.

Input file: tools/v3/out/<portal>-clinical/<portal>-testresults-*.json
            OR: read the raw probe file if that's what we have on disk.
Shape: { newResultGroups: [{ key, resultList: [result_key], organizationID,
         sortDate, formattedDate, ... }],
         newResults: { result_key: { name, key, orderMetadata: {...}, ... } } }

Per P-PHI-STAYS-LOCAL: reads local disk, POSTs to localhost HAPI.
"""

import argparse
import hashlib
import json
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
IDENT_DR = None
ID_PREFIX_LABDR = None

SCRAPER_VERSION = 'mobile-flutter-labs-list-2026-08-13'


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


def build_dr(result_obj, sort_date, organization_id, org_name):
    key = (result_obj.get('key') or '').strip()
    name = (result_obj.get('name') or '').strip()
    if not key or not name:
        return None
    om = result_obj.get('orderMetadata') or {}
    prio_iso = (om.get('prioritizedInstantISO') or '').strip() or sort_date
    provider = (om.get('authorizingProviderName') or om.get('orderProviderName') or '').strip()

    fhir_id = det_id(ID_PREFIX_LABDR, key)
    obj = {
        'resourceType': 'DiagnosticReport',
        'id': fhir_id,
        'identifier': [{'system': IDENT_DR, 'value': key}],
        'status': 'final',
        'code': {'text': name},
        'subject': {'reference': PATIENT_REF},
        'meta': {'tag': [
            {'system': 'urn:bina:src-portal',     'code': SRC_PORTAL_TAG},
            {'system': 'urn:bina:src-org',        'code': PORTAL.id},
            {'system': 'urn:bina:scraper-version','code': SCRAPER_VERSION},
        ]},
    }
    if prio_iso:
        obj['effectiveDateTime'] = prio_iso
        obj['issued'] = prio_iso
    if provider:
        obj['performer'] = [{'display': provider}]
    if org_name:
        obj['meta']['tag'].append(
            {'system': 'urn:bina:src-org-name', 'code': org_name[:60]})

    # category = LAB (Epic MyChart's resultType is 'LAB' for these).
    result_type = (om.get('resultType') or 'LAB').upper()
    obj['category'] = [{
        'coding': [{
            'system': 'http://terminology.hl7.org/CodeSystem/v2-0074',
            'code': result_type,
        }],
    }]
    return obj


def put_entry(resource):
    return {
        'fullUrl': f"urn:uuid:{resource['id']}",
        'resource': resource,
        'request': {'method': 'PUT', 'url': f"{resource['resourceType']}/{resource['id']}"},
    }


def find_labs_list_file():
    """The test-results GetList response can land as one of:
      - {portal}-testresults-*.json (if the mobile app fetches it as a
        proper section — not implemented yet)
      - {portal}-probe-*.json (the debug probe menu writes this shape)
    Try the specific first, fall back to probe."""
    p = latest_file(f'{PORTAL.id}-testresults-*.json')
    if p:
        return p, 'testresults'
    # Fallback: latest probe file, extract testResultsGetList response.
    p = latest_file(f'{PORTAL.id}-probe-*.json')
    if p:
        return p, 'probe'
    return None, None


def main():
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument('--dry-run', action='store_true')
    grp.add_argument('--apply',   action='store_true')
    ap.add_argument('--portal', default='ucsf')
    ap.add_argument('--wipe', action='store_true')
    args = ap.parse_args()
    apply = args.apply

    global PORTAL, CLINICAL_DIR, PATIENT_REF, SRC_PORTAL_TAG
    global IDENT_DR, ID_PREFIX_LABDR
    PORTAL = get_portal(args.portal)
    CLINICAL_DIR = PORTAL.input_dir(V3_OUT, 'clinical')
    PATIENT_REF = PORTAL.patient_ref
    SRC_PORTAL_TAG = PORTAL.src_portal_tag
    # 'order' matches the convention convert_mobile_labs_to_fhir.py uses
    # to look up parent DRs when POSTing per-component Observations —
    # keep the systems aligned so the details converter can find these.
    IDENT_DR = PORTAL.identifier_system('order')
    ID_PREFIX_LABDR = f'{PORTAL.id}-labdr'
    print(f'Portal: {PORTAL.name} ({PORTAL.id})')

    f, kind = find_labs_list_file()
    if not f:
        print(f'No {PORTAL.id}-testresults-*.json or -probe-*.json found under {CLINICAL_DIR}')
        sys.exit(1)
    print(f'Reading {f.name}  (via {kind})')
    d = json.load(open(f))

    if kind == 'probe':
        # Probe response wraps: {section, fetchedAt, list: {portal, results: {name: {sample, ...}}}}
        results = ((d.get('list') or {}).get('results') or {})
        # Try any of the 3 body variations that succeeded.
        for name in ('testResultsGetList_empty', 'testResultsGetList_paged', 'testResultsGetList_extern'):
            r = results.get(name)
            if isinstance(r, dict) and r.get('ok') and r.get('sample'):
                labs_body = json.loads(r['sample'])
                break
        else:
            print('  no successful testResultsGetList variant in probe')
            sys.exit(1)
    else:
        labs_body = (d.get('list') or d)

    if args.wipe and apply:
        wipe_by_identifier_system('DiagnosticReport', IDENT_DR)

    stats = Counter()
    entries = []
    groups = labs_body.get('newResultGroups') or []
    new_results = labs_body.get('newResults') or {}

    print(f'\n  groups: {len(groups)}, newResults entries: {len(new_results)}')

    # Some newResults keys carry a trailing '^'. Group.resultList[i]
    # never does. Only ~20% of labs actually appear inside a group;
    # the remainder are standalone (still real labs, just ungrouped).
    # Iterate newResults as the source of truth. Cross-reference groups
    # for sort_date + organizationID (falls back to orderMetadata if
    # not grouped).
    key_to_group = {}
    for grp in groups:
        for rk in (grp.get('resultList') or []):
            key_to_group[rk] = grp
            key_to_group[rk + '^'] = grp

    for nr_key, r in new_results.items():
        if not isinstance(r, dict):
            continue
        grp = key_to_group.get(nr_key)
        sort_date = (grp or {}).get('sortDate') or ''
        org_id = (grp or {}).get('organizationID') or ''
        dr = build_dr(r, sort_date, org_id, '')
        if dr:
            entries.append(put_entry(dr))
            stats['diagnostic-reports'] += 1
        else:
            stats['skipped-no-key-or-name'] += 1

    # Dedup.
    seen = set()
    deduped = []
    for e in entries:
        rid = (e.get('resource') or {}).get('id')
        if rid in seen:
            stats['deduped-in-bundle'] += 1
            continue
        seen.add(rid)
        deduped.append(e)
    entries = deduped

    print(f'\n=== Plan ===')
    for k, v in sorted(stats.items()):
        print(f'  {k:25s} {v}')
    print(f'  bundle entries total      {len(entries)}')

    if not apply:
        print('\n--dry-run: nothing posted.')
        return

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
            failed += len(chunk)
            body = e.read().decode('utf-8', errors='replace')[:300]
            print(f'  HTTP-{e.code}: {body}')
    print(f'  posted={posted}  failed={failed}')


if __name__ == '__main__':
    main()
