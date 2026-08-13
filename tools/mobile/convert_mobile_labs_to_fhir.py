#!/usr/bin/env python3
"""Convert Stanford lab GetDetails JSON files to FHIR Observations and
augment the parent DiagnosticReports in v2 HAPI.

Input: per-lab JSON files in tools/v3/out/stanford-labs/, each shaped
   { eorderid, details: { results: [ { name, orderMetadata,
                                      resultComponents: [...] } ] } }
captured by the mobile fetcher (Phase 4-2). Each
resultComponent has a uniform 3-field shape (componentInfo,
componentResultInfo, componentComments) — see
ScrapeJobs.stanfordLabDetail and tools/portal-scout/captures/stanford/
labs/ for the reverse-engineering record.

Output: for each input file we PUT into HAPI:
  - one Observation per resultComponent (deterministic id, idempotent)
  - the parent DiagnosticReport with .result[] pointing at the new
    Observations and .status/.effective updated from orderMetadata.

Mapping:
  Observation {
    id:           obs-lab-<sha1(eorderid+componentID)[:12]>
    identifier:   urn:stanford:myhealth:component-result + eorderid:componentID
    status:       final
    category:     laboratory
    code:         { text: commonName,
                    coding: [{system: urn:stanford:component-id, code: componentID}] }
    subject:      same as parent DR
    effective:    orderMetadata.prioritizedInstantISO
    value*:       valueQuantity if numericValue+units present, else valueString
    referenceRange: low/high from componentResultInfo.referenceRange,
                    text from formattedReferenceRange
    interpretation: mapped from abnormalFlagCategoryValue (omit if "Unknown")
    note:         from componentComments.contentAsString (if present)
  }
  DiagnosticReport (existing, PUT-updated):
    result:       [Reference(Observation/<id>), ...]
    status:       from orderMetadata.resultStatus
    effective*:   from orderMetadata.prioritizedInstantISO

Per P-PHI-STAYS-LOCAL: reads from local disk, POSTs to localhost HAPI.
Nothing leaves the host.

Usage:
  python3 convert_mobile_labs_to_fhir.py             # apply
  python3 convert_mobile_labs_to_fhir.py --dry-run   # build, don't POST
  python3 convert_mobile_labs_to_fhir.py --limit 10  # process N files only
"""

import argparse
import glob
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

# Portal-derived constants populated in main() from --portal.
PORTAL = None
LABS_DIR = None
SRC_PORTAL_TAG = None
DR_IDENT_SYSTEM = None
OBS_IDENT_SYSTEM = None
COMPONENT_SYSTEM = None

SCRAPER_VERSION = 'mobile-flutter-getdetails-2026-06-24'

# Stanford resultStatus → FHIR DiagnosticReport.status
STATUS_MAP = {
    'Final': 'final',
    'Preliminary': 'preliminary',
    'Corrected': 'corrected',
    'Amended': 'amended',
    'Appended': 'appended',
    'Cancelled': 'cancelled',
    'Entered in Error': 'entered-in-error',
}

# Stanford abnormalFlagCategoryValue → FHIR ObservationInterpretation v3 code.
# Unknown/blank means Stanford didn't supply one — omit rather than fabricate.
FLAG_MAP = {
    'Normal': 'N',
    'High': 'H',
    'Low': 'L',
    'Abnormal': 'A',
    'Critical High': 'HH',
    'Critical Low': 'LL',
    'Critical': 'AA',
    'Better': 'B',
    'Worse': 'W',
}


def det_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


def hapi_get(path: str):
    url = f'{HAPI_BASE}{path}' if not path.startswith('http') else path
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def build_existing_dr_index() -> dict:
    """Walk HAPI for every DR carrying our Stanford order identifier.
    Returns {eorderid: {fhirId, subjectRef}}."""
    print(f'Indexing DiagnosticReports with {DR_IDENT_SYSTEM} identifiers…')
    sys_param = urllib.parse.quote(f'{DR_IDENT_SYSTEM}|', safe=':')
    out = {}
    next_url = (f'/DiagnosticReport?identifier={sys_param}'
                f'&_elements=id,identifier,subject&_count=200')
    while next_url:
        bundle = hapi_get(next_url)
        for e in bundle.get('entry', []) or []:
            r = e.get('resource') or {}
            fhir_id = r.get('id')
            if not fhir_id:
                continue
            eorderid = next(
                (i.get('value') for i in (r.get('identifier') or [])
                 if i.get('system') == DR_IDENT_SYSTEM),
                None)
            if not eorderid:
                continue
            subj = (r.get('subject') or {}).get('reference')
            out[eorderid] = {'fhirId': fhir_id, 'subjectRef': subj}
        next_url = None
        for link in bundle.get('link', []) or []:
            if link.get('relation') == 'next' and link.get('url'):
                next_url = link['url']
                break
    print(f'  indexed: {len(out)} DRs')
    return out


def build_observation(eorderid: str, dr_index_entry: dict, component: dict,
                      effective_iso: str, row_index: int) -> dict:
    """Map one resultComponent → one FHIR R4 Observation. Returns None if
    the component carries no value to record. row_index is included in
    the deterministic id + identifier so labs that repeat a componentID
    (e.g. Stanford's wound-culture orders that list "GRAM STAIN" twice
    with the same component id) don't collide on a single hash."""
    ci = component.get('componentInfo') or {}
    ri = component.get('componentResultInfo') or {}
    cc = component.get('componentComments') or {}

    component_id = ci.get('componentID') or ''
    common_name  = ci.get('commonName') or ci.get('name') or ''
    units        = ci.get('units') or ''

    value_str   = (ri.get('value') or '').strip()
    numeric_val = ri.get('numericValue')
    rr          = ri.get('referenceRange') or {}
    flag        = (ri.get('abnormalFlagCategoryValue') or '').strip()
    comment_txt = (cc.get('contentAsString') or '').strip()

    # Skip pure-placeholder rows with neither value nor comment (e.g.
    # "FASTING STATUS" with empty value, no comment).
    if not value_str and not comment_txt:
        return None

    fhir_id = det_id('obs-lab', eorderid, component_id, str(row_index))
    obs = {
        'resourceType': 'Observation',
        'id': fhir_id,
        'identifier': [{
            'system': OBS_IDENT_SYSTEM,
            'value': f'{eorderid}:{component_id}:{row_index}',
        }],
        'status': 'final',
        'category': [{
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                'code': 'laboratory',
                'display': 'Laboratory',
            }],
        }],
        'code': {
            'text': common_name,
            'coding': [{
                'system': COMPONENT_SYSTEM,
                'code': component_id,
                'display': common_name,
            }] if component_id else [],
        },
        'subject': {'reference': dr_index_entry['subjectRef']} if dr_index_entry.get('subjectRef') else None,
        'effectiveDateTime': effective_iso or None,
        'meta': {
            'tag': [
                {'system': 'urn:bina:src-portal', 'code': SRC_PORTAL_TAG},
                {'system': 'urn:bina:src-org',    'code': PORTAL.id},
                {'system': 'urn:bina:scraper-version', 'code': SCRAPER_VERSION},
            ],
        },
    }
    # Drop the subject key if we couldn't resolve one
    if obs['subject'] is None: del obs['subject']
    if obs['effectiveDateTime'] is None: del obs['effectiveDateTime']
    if not obs['code']['coding']: del obs['code']['coding']

    # Value: prefer Quantity when we have a numeric + units, else String.
    if numeric_val is not None and units and isinstance(numeric_val, (int, float)):
        obs['valueQuantity'] = {
            'value': numeric_val,
            'unit': units,
            # We don't have UCUM mapping yet; leave system/code unset.
        }
    elif value_str:
        obs['valueString'] = value_str

    # Reference range (only if we have at least a low or high or text).
    low  = rr.get('low')
    high = rr.get('high')
    rr_text = (rr.get('formattedReferenceRange') or '').strip()
    if low is not None or high is not None or rr_text:
        entry = {}
        if isinstance(low, (int, float)):
            entry['low'] = {'value': low, 'unit': units} if units else {'value': low}
        if isinstance(high, (int, float)):
            entry['high'] = {'value': high, 'unit': units} if units else {'value': high}
        if rr_text:
            entry['text'] = rr_text
        obs['referenceRange'] = [entry]

    # Interpretation (only if mappable)
    if flag in FLAG_MAP:
        obs['interpretation'] = [{
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation',
                'code': FLAG_MAP[flag],
                'display': flag,
            }],
            'text': flag,
        }]

    if comment_txt:
        obs['note'] = [{'text': comment_txt}]

    return obs


def merge_dr(existing_dr: dict, result_refs: list, effective_iso: str,
             stanford_status: str) -> dict:
    """Take HAPI's existing DR and PUT-update it with our new result refs,
    status, and effective datetime. Preserves other fields untouched."""
    dr = dict(existing_dr)  # shallow copy is fine — we replace whole fields

    # Replace result with our refs (any prior refs from other ingests are dropped;
    # the deterministic IDs mean re-running this script doesn't churn them).
    dr['result'] = result_refs

    # Set status if mappable
    new_status = STATUS_MAP.get(stanford_status)
    if new_status:
        dr['status'] = new_status

    # Set effective datetime if we have one and the DR doesn't already
    if effective_iso and not dr.get('effectiveDateTime') and not dr.get('effectivePeriod'):
        dr['effectiveDateTime'] = effective_iso

    # Tag the meta to mark this DR has had bodies ingested
    meta = dr.get('meta') or {}
    tags = list(meta.get('tag') or [])
    has_body_tag = any(t.get('system') == 'urn:bina:lab-body-ingested' for t in tags)
    if not has_body_tag:
        tags.append({'system': 'urn:bina:lab-body-ingested', 'code': SCRAPER_VERSION})
        meta['tag'] = tags
        dr['meta'] = meta

    return dr


def build_transaction_bundle(dr_updated: dict, observations: list) -> dict:
    """One transaction Bundle: PUT the DR + all child Observations. Atomic."""
    entries = []
    for obs in observations:
        entries.append({
            'fullUrl': f"urn:uuid:{obs['id']}",
            'resource': obs,
            'request': {'method': 'PUT', 'url': f"Observation/{obs['id']}"},
        })
    entries.append({
        'fullUrl': f"DiagnosticReport/{dr_updated['id']}",
        'resource': dr_updated,
        'request': {'method': 'PUT', 'url': f"DiagnosticReport/{dr_updated['id']}"},
    })
    return {'resourceType': 'Bundle', 'type': 'transaction', 'entry': entries}


def post_bundle(bundle: dict) -> dict:
    req = urllib.request.Request(
        HAPI_BASE,
        data=json.dumps(bundle).encode('utf-8'),
        headers={'Content-Type': 'application/fhir+json',
                 'Accept': 'application/fhir+json'},
        method='POST',
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def wipe_existing_observations():
    """Delete every Observation with our urn:stanford:myhealth:component-result
    identifier. Used when the deterministic-id scheme changes so we don't
    leave orphans alongside the new resources."""
    print(f'Wiping existing {OBS_IDENT_SYSTEM} Observations…')
    sys_param = urllib.parse.quote(f'{OBS_IDENT_SYSTEM}|', safe=':')
    ids = []
    next_url = f'/Observation?identifier={sys_param}&_elements=id&_count=200'
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
    if not ids: return
    BATCH = 100
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i+BATCH]
        bundle = {
            'resourceType': 'Bundle', 'type': 'transaction',
            'entry': [{'request': {'method': 'DELETE', 'url': f'Observation/{rid}'}} for rid in chunk],
        }
        post_bundle(bundle)
    print(f'  deleted {len(ids)}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='Build bundles, do not POST')
    ap.add_argument('--limit', type=int, default=None, help='Process only the first N lab files')
    ap.add_argument('--portal', default='stanford',
                    help='Portal id from mobile/assets/portals/*.json (default: stanford)')
    ap.add_argument('--wipe',  action='store_true',
                    help='Delete all existing component-result Observations for this portal before re-import')
    args = ap.parse_args()
    if args.wipe and args.dry_run:
        print('--wipe and --dry-run are mutually exclusive'); sys.exit(2)

    global PORTAL, LABS_DIR, SRC_PORTAL_TAG, DR_IDENT_SYSTEM, OBS_IDENT_SYSTEM, COMPONENT_SYSTEM
    PORTAL = get_portal(args.portal)
    LABS_DIR = PORTAL.input_dir(V3_OUT, 'labs')
    SRC_PORTAL_TAG = PORTAL.src_portal_tag
    DR_IDENT_SYSTEM = PORTAL.identifier_system('order')
    OBS_IDENT_SYSTEM = PORTAL.identifier_system('component-result')
    # component-id is a portal-native identifier (Epic's internal component id).
    # System URN uses just the portal id since it's not scoped to any product.
    COMPONENT_SYSTEM = f'urn:{PORTAL.id}:component-id'
    print(f'Portal: {PORTAL.name} ({PORTAL.id})')
    print(f'  labs dir: {LABS_DIR}')

    if args.wipe:
        wipe_existing_observations()

    files = sorted(glob.glob(str(LABS_DIR / f'{PORTAL.id}-lab-*.json')))
    if args.limit:
        files = files[: args.limit]
    print(f'Lab files to process: {len(files)}')
    if not files:
        print('No lab JSON files found — sync them first via the simulator.')
        sys.exit(1)

    dr_index = build_existing_dr_index()

    stats = Counter()
    obs_per_lab = []
    failures = []

    for fp in files:
        try:
            doc = json.load(open(fp))
        except Exception as e:
            stats['parse-error'] += 1
            failures.append({'file': Path(fp).name, 'reason': f'json-parse:{e}'})
            continue
        eorderid = doc.get('eorderid')
        details  = doc.get('details') or {}
        results  = details.get('results') or []
        if not eorderid:
            stats['no-eorderid'] += 1; continue
        if not results:
            stats['no-results'] += 1; continue
        r0 = results[0]
        components = r0.get('resultComponents') or []
        if not components:
            stats['empty-components'] += 1; continue
        meta = r0.get('orderMetadata') or {}
        effective_iso = meta.get('prioritizedInstantISO') or ''
        stanford_status = meta.get('resultStatus') or ''

        # DR anchor: use existing HAPI DR if we can find one for this
        # eorderid (Stanford's normal path — v3 ingest wrote DR shells
        # in advance). Otherwise SYNTHESIZE a fresh DR from the body's
        # own metadata (UCSF path — no pre-existing DRs because their
        # per-session order keys rotate, so a labs-list step done in a
        # different session would produce mismatched anchors).
        if eorderid in dr_index:
            dr_entry = dr_index[eorderid]
        else:
            synth_dr_id = f"{PORTAL.id}-labdr-{hashlib.sha1(eorderid.encode()).hexdigest()[:12]}"
            synth_dr = {
                'resourceType': 'DiagnosticReport',
                'id': synth_dr_id,
                'identifier': [{'system': DR_IDENT_SYSTEM, 'value': eorderid}],
                'status': 'final',
                'code': {'text': (r0.get('name') or '').strip() or 'Lab result'},
                'subject': {'reference': f'Patient/{PORTAL.patient_ref.split("/")[-1]}'},
                'meta': {'tag': [
                    {'system': 'urn:bina:src-portal',      'code': SRC_PORTAL_TAG},
                    {'system': 'urn:bina:src-org',         'code': PORTAL.id},
                    {'system': 'urn:bina:scraper-version', 'code': SCRAPER_VERSION},
                ]},
            }
            if effective_iso:
                synth_dr['effectiveDateTime'] = effective_iso
                synth_dr['issued'] = effective_iso
            prov = (meta.get('authorizingProviderName') or meta.get('orderProviderName') or '').strip()
            if prov:
                synth_dr['performer'] = [{'display': prov}]
            # POST it now so subsequent lookups on the fly see it.
            try:
                # PUT is idempotent by id; upsert.
                req = urllib.request.Request(
                    f'{HAPI_BASE}/DiagnosticReport/{synth_dr_id}',
                    data=json.dumps(synth_dr).encode('utf-8'),
                    headers={'Content-Type': 'application/fhir+json'},
                    method='PUT',
                )
                urllib.request.urlopen(req).read()
                stats['dr-synthesized'] += 1
            except urllib.error.HTTPError as e:
                stats['dr-synth-failed'] += 1
                failures.append({'eorderid': eorderid[:30] + '…',
                                 'reason': f'dr-synth:{e.code}'})
                continue
            dr_index[eorderid] = {'fhirId': synth_dr_id,
                                  'subjectRef': synth_dr['subject']['reference']}
            dr_entry = dr_index[eorderid]

        # Build Observations
        obs_list = []
        for row_index, comp in enumerate(components):
            obs = build_observation(eorderid, dr_entry, comp, effective_iso, row_index)
            if obs:
                obs_list.append(obs)
        stats['labs-processed'] += 1
        obs_per_lab.append(len(obs_list))
        if not obs_list:
            stats['all-components-empty'] += 1
            continue

        # Fetch existing DR for merge
        try:
            existing_dr = hapi_get(f"/DiagnosticReport/{dr_entry['fhirId']}")
        except Exception as e:
            stats['dr-fetch-failed'] += 1
            failures.append({'eorderid': eorderid[:30] + '…', 'reason': f'dr-fetch:{e}'})
            continue
        result_refs = [{'reference': f"Observation/{o['id']}"} for o in obs_list]
        dr_updated = merge_dr(existing_dr, result_refs, effective_iso, stanford_status)

        bundle = build_transaction_bundle(dr_updated, obs_list)

        if args.dry_run:
            stats['dry-run-bundle-ok'] += 1
            continue
        try:
            post_bundle(bundle)
            stats['bundles-posted'] += 1
            stats['observations-written'] += len(obs_list)
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:300]
            stats['bundles-failed'] += 1
            failures.append({'eorderid': eorderid[:30] + '…',
                             'reason': f'http-{e.code}', 'body': body})
        except Exception as e:
            stats['bundles-failed'] += 1
            failures.append({'eorderid': eorderid[:30] + '…',
                             'reason': f'exception:{e}'})

    print()
    print('=== Summary ===')
    for k in sorted(stats):
        print(f'  {k:30s} {stats[k]}')
    if obs_per_lab:
        print(f'  observations/lab: min={min(obs_per_lab)} '
              f'median={sorted(obs_per_lab)[len(obs_per_lab)//2]} '
              f'max={max(obs_per_lab)} total={sum(obs_per_lab)}')
    if failures:
        print(f'\nFailures ({len(failures)}):')
        for f in failures[:15]:
            print(f'  {f}')
        if len(failures) > 15:
            print(f'  …and {len(failures)-15} more')


if __name__ == '__main__':
    main()
