#!/usr/bin/env python3
"""Consolidate HAPI's 7 ad-hoc Patient resources into a single canonical
Bina Patient + N sub-identity Patients (one per data source), then
backfill .subject on the ~43k resources currently missing it.

Architecture (user-confirmed 2026-06-23):

  Patient/bina-user-urisarid                            ← NEW canonical anchor
    identifier: urn:bina:source|bina-user
    name: Uri Sarid, DOB 1964-05-23
    link[seealso]: 10 references to the sub-identity Patients below.

  Sub-identity Patients (each = one data source; data resources point HERE):
    Patient/eLnGIsbP3w5y…       (existing, Stanford Epic)   → labeled stanford
    Patient/063dd131-…          (existing, UCSF via Apple)  → labeled ucsf
    Patient/06b73042-…          (existing, Mayo via Apple)  → labeled mayo
    Patient/Z2016019            (existing, MSKCC)           → labeled mskcc
    Patient/bina-sutter         (NEW)                       → labeled sutter
    Patient/bina-labcorp        (NEW)                       → labeled labcorp
    Patient/bina-doctorsdata    (NEW)                       → labeled doctorsdata
    Patient/bina-cerner-unknown (NEW)                       → labeled cerner
    Patient/bina-unknown-oid    (NEW)                       → labeled unknown-oid
    Patient/bina-untagged       (NEW)                       → labeled untagged

Resource → sub-identity assignment uses the resource's identifier-system
(see SOURCE_RULES below). The fallback bucket (no identifier at all) gets
its own sub-identity rather than dumping into the canonical anchor —
symmetric treatment of every data source.

Operations (in order):
  1. CREATE 6 new sub-identity Patients + 1 canonical anchor (deterministic ids)
  2. UPDATE 4 existing Patients to carry the Bina-namespace identifier
  3. MERGE: rewrite refs from Apple-via-Stanford duplicate (5e8a7a88) →
     Stanford canonical; from mskcc-patient stub → Z2016019
  4. BACKFILL: ~43k missing-subject resources → matched sub-identity
  5. WIRE seealso links on bina-user-urisarid → all 10 sub-identities
  6. DELETE: 5e8a7a88, mskcc-patient, patient-urisarid (orphan)

Per P-PHI-STAYS-LOCAL: all operations against localhost HAPI, no PHI
leaves the host.

Usage:
  python3 consolidate_patients.py --dry-run    # plan only, no writes
  python3 consolidate_patients.py --apply      # execute
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from pathlib import Path

HAPI_BASE = 'http://localhost:8090/fhir'

BINA_SOURCE_SYSTEM = 'urn:bina:source'

# Canonical anchor + sub-identity Patient resource IDs
CANONICAL_PID = 'bina-user-urisarid'
SUB_PIDS = {
    'stanford':              'eLnGIsbP3w5yfjWlLiwicjQI2Qqhzi1Zkub17YmdqWg03',
    'ucsf':                  '063dd131-6132-47de-8495-d2d666636a74',
    'mayo':                  '06b73042-fbbc-4742-b62b-b706f2b5abdb',
    'mskcc':                 'Z2016019',
    'sutter':                'bina-sutter',
    'labcorp':               'bina-labcorp',
    'doctorsdata':           'bina-doctorsdata',
    'cerner':                'bina-cerner-unknown',
    'apple-wearable':        'bina-apple-wearable',         # HealthKit device streams (incl. ECG)
    'apple-health-records':  'bina-apple-health-records',   # Apple's portal-import layer
    'historical-import':     'bina-historical-import',      # the "FINAL (UCSF+Mayo+Sutter+MSKCC)" bucket
    'sibocenter':            'bina-sibocenter',             # SIBO Center (breath-test lab vendor)
    'genova':                'bina-genova',                 # Genova Diagnostics (lab vendor)
    'unknown-oid':           'bina-unknown-oid',
    'untagged':              'bina-untagged',
}
NEW_SUB_KEYS = ['sutter', 'labcorp', 'doctorsdata', 'cerner',
                'apple-wearable', 'apple-health-records', 'historical-import',
                'sibocenter', 'genova',
                'unknown-oid', 'untagged']
EXISTING_SUB_KEYS = ['stanford', 'ucsf', 'mayo', 'mskcc']

# Patient IDs to DELETE after their refs have been rewritten
PATIENTS_TO_DELETE = [
    '5e8a7a88-70c8-4674-81a2-18913ad201cc',  # Stanford via Apple Health — merge into stanford
    'mskcc-patient',                          # MSKCC stub — merge into Z2016019
    'patient-urisarid',                       # zero-ref orphan
]
# Refs from these duplicates get rewritten to these canonical sub-ids
MERGE_MAP = {
    '5e8a7a88-70c8-4674-81a2-18913ad201cc': SUB_PIDS['stanford'],
    'mskcc-patient':                         SUB_PIDS['mskcc'],
}

# Resource-identifier system patterns → sub-identity key. Evaluated in order;
# first match wins. Regexes intentionally permissive.
SOURCE_RULES = [
    (re.compile(r'^urn:stanford:'),                                    'stanford'),
    (re.compile(r'^urn:oid:1\.2\.840\.114350\.1\.13\.71(\.|$)'),        'stanford'),
    (re.compile(r'open\.epic\.com/FHIR/71/'),                          'stanford'),
    (re.compile(r'^urn:ucsf:'),                                         'ucsf'),
    (re.compile(r'^urn:oid:1\.2\.840\.114350\.1\.13\.266(\.|$)'),       'ucsf'),
    (re.compile(r'open\.epic\.com/FHIR/266/'),                         'ucsf'),
    (re.compile(r'^urn:mayo:'),                                         'mayo'),
    (re.compile(r'^urn:oid:1\.2\.840\.114350\.1\.13\.171(\.|$)'),       'mayo'),
    (re.compile(r'^urn:oid:1\.2\.840\.114350\.1\.13\.451(\.|$)'),       'mayo'),
    (re.compile(r'open\.epic\.com/FHIR/(171|451)/'),                   'mayo'),
    (re.compile(r'^urn:mskcc:'),                                        'mskcc'),
    (re.compile(r'^http://mskcc\.org'),                                 'mskcc'),
    (re.compile(r'^urn:sutter'),                                        'sutter'),
    (re.compile(r'^urn:oid:1\.2\.840\.114350\.1\.13\.76(\.|$)'),        'sutter'),
    (re.compile(r'open\.epic\.com/FHIR/76/'),                          'sutter'),
    (re.compile(r'labcorp', re.I),                                      'labcorp'),
    (re.compile(r'doctorsdata', re.I),                                  'doctorsdata'),
    (re.compile(r'fhir\.cerner\.com', re.I),                            'cerner'),
    (re.compile(r'^urn:sibocenter', re.I),                              'sibocenter'),
    (re.compile(r'^urn:genova', re.I),                                  'genova'),
    (re.compile(r'^urn:oid:1\.2\.840\.114350\.1\.72\.'),                'unknown-oid'),
]


EXTENSION_SOURCE_MAP = {
    'Apple Wearable':                       'apple-wearable',
    'Apple Health ECG':                     'apple-wearable',     # same HealthKit family
    'Apple Health Records':                 'apple-health-records',
    'FINAL (UCSF+Mayo+Sutter+MSKCC)':       'historical-import',
}

def classify(resource: dict) -> str:
    """Return the sub-identity key matching the resource's identifier(s),
    then falling back to meta.extension[source-system].valueString, and
    finally 'untagged' if nothing matches. The 'untagged' bucket is still
    a real sub-identity Patient (symmetric treatment), not a fallback to
    the canonical anchor."""
    for ident in (resource.get('identifier') or []):
        sysn = ident.get('system') or ''
        for pat, key in SOURCE_RULES:
            if pat.search(sysn):
                return key
    for ext in ((resource.get('meta') or {}).get('extension') or []):
        if 'source-system' in (ext.get('url') or ''):
            val = ext.get('valueString') or ''
            if val in EXTENSION_SOURCE_MAP:
                return EXTENSION_SOURCE_MAP[val]
            # Unknown extension value — keep as untagged so it's visible
            break
    return 'untagged'


# ── HAPI helpers ──────────────────────────────────────────────────────

def hapi_get(path: str):
    url = path if path.startswith('http') else f'{HAPI_BASE}{path}'
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())

def hapi_put(rtype: str, rid: str, resource: dict) -> dict:
    req = urllib.request.Request(
        f'{HAPI_BASE}/{rtype}/{rid}',
        data=json.dumps(resource).encode('utf-8'),
        headers={'Content-Type': 'application/fhir+json'},
        method='PUT')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def hapi_delete(rtype: str, rid: str):
    req = urllib.request.Request(f'{HAPI_BASE}/{rtype}/{rid}', method='DELETE')
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code in (404, 410): return e.code
        raise

def post_transaction(entries: list) -> dict:
    bundle = {'resourceType': 'Bundle', 'type': 'transaction', 'entry': entries}
    req = urllib.request.Request(
        HAPI_BASE,
        data=json.dumps(bundle).encode('utf-8'),
        headers={'Content-Type': 'application/fhir+json'},
        method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


# ── Resource type table (param name + how subject is expressed) ───────
# Most resources expose `.subject` (Reference). AllergyIntolerance and
# Immunization use `.patient`. We touch both.
RESOURCE_TYPES = [
    ('Encounter',          'subject', 'subject'),
    ('Condition',          'subject', 'subject'),
    ('Procedure',          'subject', 'subject'),
    ('Observation',        'subject', 'subject'),
    ('DiagnosticReport',   'subject', 'subject'),
    ('DocumentReference',  'subject', 'subject'),
    ('Communication',      'subject', 'subject'),
    ('MedicationRequest',  'subject', 'subject'),
    ('MedicationStatement','subject', 'subject'),
    ('AllergyIntolerance', 'patient', 'patient'),
    ('Immunization',       'patient', 'patient'),
    ('CarePlan',           'subject', 'subject'),
    ('CareTeam',           'subject', 'subject'),
]


# ── Step 1+2: Build / update Patient resources ────────────────────────

def build_canonical_patient() -> dict:
    return {
        'resourceType': 'Patient',
        'id': CANONICAL_PID,
        'identifier': [
            {'system': BINA_SOURCE_SYSTEM, 'value': 'bina-user'},
            {'system': 'urn:bina:user', 'value': 'urisarid'},
        ],
        'active': True,
        'name': [{'use': 'official', 'family': 'Sarid', 'given': ['Uri']}],
        'birthDate': '1964-05-23',
        'meta': {'tag': [
            {'system': 'urn:bina:patient-role', 'code': 'canonical-anchor'},
        ]},
    }

def build_new_sub_patient(key: str) -> dict:
    return {
        'resourceType': 'Patient',
        'id': SUB_PIDS[key],
        'identifier': [{'system': BINA_SOURCE_SYSTEM, 'value': key}],
        'active': True,
        'name': [{'use': 'official', 'family': 'Sarid', 'given': ['Uri']}],
        'birthDate': '1964-05-23',
        'meta': {'tag': [
            {'system': 'urn:bina:patient-role', 'code': 'sub-identity'},
            {'system': 'urn:bina:source',       'code': key},
        ]},
    }

def label_existing_sub_patient(existing: dict, key: str) -> dict:
    """Add the Bina identifier + role tag to an already-existing Patient
    without losing its institution-native identifiers."""
    p = dict(existing)
    p.setdefault('identifier', [])
    if not any(i.get('system') == BINA_SOURCE_SYSTEM and i.get('value') == key
               for i in p['identifier']):
        p['identifier'].append({'system': BINA_SOURCE_SYSTEM, 'value': key})
    meta = p.get('meta') or {}
    tags = list(meta.get('tag') or [])
    if not any(t.get('system') == 'urn:bina:patient-role' for t in tags):
        tags.append({'system': 'urn:bina:patient-role', 'code': 'sub-identity'})
    if not any(t.get('system') == 'urn:bina:source' for t in tags):
        tags.append({'system': 'urn:bina:source', 'code': key})
    meta['tag'] = tags
    p['meta'] = meta
    return p


# ── Step 3+4: Subject merge + backfill ────────────────────────────────

def walk_resources(rtype: str, param: str, missing: bool, ref: str = None):
    """Iterate every resource matching the filter. missing=True yields
    resources where `<param>` is missing. ref=Patient/<id> yields
    resources where `<param>=Patient/<id>`."""
    if missing:
        q = f'/{rtype}?{param}:missing=true&_count=200'
    else:
        q = f'/{rtype}?{param}={urllib.parse.quote(ref, safe="/")}&_count=200'
    next_url = q
    while next_url:
        b = hapi_get(next_url)
        for e in b.get('entry') or []:
            r = e.get('resource')
            if r: yield r
        next_url = None
        for link in b.get('link') or []:
            if link.get('relation') == 'next' and link.get('url'):
                next_url = link['url']; break


def reassign_subject(resource: dict, new_pid: str, ref_field: str):
    """Set resource.<ref_field> = Reference(Patient/<new_pid>). Returns the
    full updated resource (caller PUTs it)."""
    r = dict(resource)
    r[ref_field] = {'reference': f'Patient/{new_pid}'}
    return r


# ── Main orchestration ───────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument('--dry-run', action='store_true', help='Plan only; no writes')
    grp.add_argument('--apply',   action='store_true', help='Execute the consolidation')
    ap.add_argument('--batch',    type=int, default=100,
                    help='Per-bundle PUT batch size for backfill (default 100)')
    args = ap.parse_args()
    apply = args.apply

    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print()

    # ── Plan: counts per sub-identity, by type ────────────────────────
    plan = defaultdict(lambda: defaultdict(int))   # plan[key][type] = count
    rewrite_plan = defaultdict(lambda: defaultdict(int))  # rewrite_plan[oldPid][type] = count

    print("Phase A: classify missing-subject resources by identifier system…")
    for rtype, search_param, ref_field in RESOURCE_TYPES:
        total = (hapi_get(f'/{rtype}?{search_param}:missing=true&_summary=count') or {}).get('total', 0)
        if total == 0: continue
        for r in walk_resources(rtype, search_param, missing=True):
            key = classify(r)
            plan[key][rtype] += 1
        print(f"  {rtype:22s}: {sum(plan[k].get(rtype,0) for k in plan):>6d} classified out of {total}")

    print("\nPhase B: count refs pointing at duplicate Patients (to be rewritten)…")
    for old_pid, new_pid in MERGE_MAP.items():
        for rtype, search_param, ref_field in RESOURCE_TYPES:
            n = (hapi_get(f'/{rtype}?{search_param}=Patient/{old_pid}&_summary=count') or {}).get('total', 0)
            if n:
                rewrite_plan[old_pid][rtype] = n

    print("\n=== Planned changes ===\n")
    print(f"Canonical Patient to CREATE: Patient/{CANONICAL_PID}")
    print(f"New sub-identity Patients to CREATE: {len(NEW_SUB_KEYS)}  →  {[SUB_PIDS[k] for k in NEW_SUB_KEYS]}")
    print(f"Existing Patients to LABEL with Bina identifier: {len(EXISTING_SUB_KEYS)}  →  {[SUB_PIDS[k] for k in EXISTING_SUB_KEYS]}")
    print(f"Patient.link[seealso] entries to add on canonical: {len(SUB_PIDS)}")
    print()
    print("Subject BACKFILL by sub-identity (missing → assigned):")
    total_backfill = 0
    for key in list(SUB_PIDS.keys()):
        per_type = plan.get(key, {})
        n = sum(per_type.values())
        total_backfill += n
        if n:
            breakdown = ', '.join(f'{t}:{c}' for t, c in sorted(per_type.items(), key=lambda x: -x[1]) if c)
            print(f"  {key:14s} → Patient/{SUB_PIDS[key]:42s} {n:>6d}  ({breakdown})")
    print(f"  {'TOTAL':14s} {'':>57s} {total_backfill:>6d}")
    print()
    print("Subject MERGE (rewrite duplicate-Patient refs):")
    total_merge = 0
    for old_pid, per_type in rewrite_plan.items():
        n = sum(per_type.values())
        total_merge += n
        breakdown = ', '.join(f'{t}:{c}' for t, c in per_type.items() if c)
        print(f"  {old_pid[:30]:30s} → {MERGE_MAP[old_pid]:42s} {n:>6d}  ({breakdown})")
    print(f"  {'TOTAL':30s} {'':>45s} {total_merge:>6d}")
    print()
    print(f"Patients to DELETE after consolidation: {len(PATIENTS_TO_DELETE)}  →  {PATIENTS_TO_DELETE}")
    print()
    print(f"GRAND TOTAL writes: {total_backfill + total_merge} resources reassigned, "
          f"{len(NEW_SUB_KEYS)+1} created, {len(EXISTING_SUB_KEYS)+1} updated (canonical + 4 labels), "
          f"{len(PATIENTS_TO_DELETE)} deleted")
    if not apply:
        print("\n--dry-run: no changes written. Re-run with --apply.")
        return

    # ── EXECUTE ───────────────────────────────────────────────────────

    print("\n=== EXECUTING ===\n")

    # Step 1: CREATE sub-identity Patients (deterministic IDs; PUT is idempotent)
    print("[1] Creating new sub-identity Patients + canonical anchor…")
    for key in NEW_SUB_KEYS:
        hapi_put('Patient', SUB_PIDS[key], build_new_sub_patient(key))
        print(f"    ✓ Patient/{SUB_PIDS[key]} ({key})")
    hapi_put('Patient', CANONICAL_PID, build_canonical_patient())
    print(f"    ✓ Patient/{CANONICAL_PID} (canonical)")

    # Step 2: LABEL existing sub-identity Patients
    print("\n[2] Labeling existing Patients with Bina identifiers…")
    for key in EXISTING_SUB_KEYS:
        existing = hapi_get(f'/Patient/{SUB_PIDS[key]}')
        labeled = label_existing_sub_patient(existing, key)
        hapi_put('Patient', SUB_PIDS[key], labeled)
        print(f"    ✓ Patient/{SUB_PIDS[key]} ← urn:bina:source|{key}")

    # Step 3: MERGE (rewrite duplicate-Patient refs)
    print("\n[3] Rewriting duplicate-Patient refs…")
    for old_pid, new_pid in MERGE_MAP.items():
        for rtype, search_param, ref_field in RESOURCE_TYPES:
            count = 0
            for r in walk_resources(rtype, search_param, missing=False, ref=f'Patient/{old_pid}'):
                updated = reassign_subject(r, new_pid, ref_field)
                hapi_put(rtype, r['id'], updated)
                count += 1
            if count:
                print(f"    ✓ {rtype} ({count}): {old_pid[:20]}… → {new_pid[:20]}…")

    # Step 4: BACKFILL — walk missing-subject resources, assign by classify()
    print("\n[4] Backfilling missing subjects…")
    for rtype, search_param, ref_field in RESOURCE_TYPES:
        per_type_counts = defaultdict(int)
        for r in walk_resources(rtype, search_param, missing=True):
            key = classify(r)
            updated = reassign_subject(r, SUB_PIDS[key], ref_field)
            hapi_put(rtype, r['id'], updated)
            per_type_counts[key] += 1
        if per_type_counts:
            kvs = ', '.join(f'{k}:{v}' for k, v in per_type_counts.items())
            print(f"    ✓ {rtype}: {sum(per_type_counts.values())} ({kvs})")

    # Step 5: WIRE Patient.link[seealso] on canonical
    print("\n[5] Wiring Patient.link[seealso] on canonical anchor…")
    canonical = hapi_get(f'/Patient/{CANONICAL_PID}')
    canonical['link'] = [
        {'other': {'reference': f'Patient/{SUB_PIDS[k]}'}, 'type': 'seealso'}
        for k in SUB_PIDS
    ]
    hapi_put('Patient', CANONICAL_PID, canonical)
    print(f"    ✓ Patient/{CANONICAL_PID} now links to {len(SUB_PIDS)} sub-identities")

    # Step 6: DELETE obsolete Patients
    print("\n[6] Deleting obsolete Patient resources…")
    for pid in PATIENTS_TO_DELETE:
        s = hapi_delete('Patient', pid)
        print(f"    ✓ DELETE Patient/{pid}  (status={s})")

    # Verify
    print("\n=== VERIFY ===")
    everything = hapi_get(f'/Patient/{CANONICAL_PID}/$everything?_summary=count')
    print(f"Patient/$everything on canonical: total = {everything.get('total')}")
    plist = hapi_get('/Patient?_summary=count')
    print(f"Patient resources remaining: {plist.get('total')}")


if __name__ == '__main__':
    main()
