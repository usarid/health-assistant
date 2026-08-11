#!/usr/bin/env python3
"""Convert Stanford orion-endpoint JSON (Procedures + Appointments) to
FHIR R4 and POST to v2 HAPI.

Inputs (from Phase 6a orion fetcher, written by the after-auth
orchestrator):
  tools/v3/out/stanford-clinical/
    stanford-procedures-<ts>.json    — top.list.surgeryCaseList[]
    stanford-appointments-<ts>.json  — top.list.appointments[]

Mappings:

  Procedure (one per item in surgeryCaseList[].procedureList — a single
             surgery case can bundle multiple billable procedures) {
    id:           stanford-proc-<sha1(caseId + procedureName + providerNPI)>
    identifier:   urn:stanford:myhealth:surgery-case + "<caseId>:<procedureName>:<NPI>"
    status:       mapped from case.surgeryStatus
    code.text:    procedureList[i].procedureName
    subject:      Patient/eLnGIsbP3w5y…
    performedDateTime: parsed from case.surgeryDate + surgeryTime
    performer:    [{actor: {display: providerName,
                            identifier: {system: 'http://hl7.org/fhir/sid/us-npi', value: NPI}}}]
    location:     {display: surgeryLocation.name}
    note.text:    surgeryLocation.address one-line (city, state, zip)
  }

  Appointment (one per appointments[] entry) {
    id:           stanford-appt-<sha1(csn)>
    identifier:   urn:stanford:myhealth:appointment + csn
    status:       future dates → 'booked'; past → 'fulfilled' (best-effort)
    start:        parsed from appointmentDateTime (epoch millis)
    minutesDuration: duration
    participant:  [{actor: Patient/eLnGIsbP3w5y…, status: accepted},
                   {actor: {display: provider.name,
                            identifier: NPI}, status: accepted},
                   {actor: {display: department.name}, status: accepted}]
    description:  patientInstructions (if present)
  }

Per P-PHI-STAYS-LOCAL: reads local disk, POSTs to localhost HAPI.

Usage:
  python3 convert_mobile_orion_to_fhir.py --dry-run
  python3 convert_mobile_orion_to_fhir.py --apply [--wipe]
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
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLINICAL_DIR = REPO_ROOT / 'tools' / 'v3' / 'out' / 'stanford-clinical'
HAPI_BASE = 'http://localhost:8090/fhir'

STANFORD_PATIENT_REF = 'Patient/eLnGIsbP3w5yfjWlLiwicjQI2Qqhzi1Zkub17YmdqWg03'

IDENT_PROCEDURE   = 'urn:stanford:myhealth:surgery-case'
IDENT_APPOINTMENT = 'urn:stanford:myhealth:appointment'

SCRAPER_VERSION = 'mobile-flutter-orion-2026-06-25'

# Stanford surgery status → FHIR R4 Procedure.status
PROC_STATUS_MAP = {
    'Completed':  'completed',
    'Cancelled':  'not-done',
    'Scheduled':  'preparation',
    'In Progress': 'in-progress',
    'Rescheduled': 'preparation',
}


def det_id(prefix, *parts):
    h = hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


def parse_mdy_time(date_str, time_str):
    """Parse 'M/D/YYYY' + 'H:MM AM/PM' → ISO 8601 (no timezone; Stanford
    surgery times are local — best we can do without a zone offset)."""
    if not date_str: return None
    md = re.match(r'^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$', date_str)
    if not md: return None
    mo, dy, yr = md.groups()
    if not time_str:
        return f'{yr}-{int(mo):02d}-{int(dy):02d}'
    tm = re.match(r'^\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*$', time_str, re.I)
    if not tm:
        return f'{yr}-{int(mo):02d}-{int(dy):02d}'
    h, mnt, ampm = tm.groups()
    h = int(h); mnt = int(mnt)
    if ampm and ampm.upper() == 'PM' and h != 12: h += 12
    if ampm and ampm.upper() == 'AM' and h == 12: h = 0
    return f'{yr}-{int(mo):02d}-{int(dy):02d}T{h:02d}:{mnt:02d}:00'


def epoch_millis_to_iso(millis_str):
    try:
        n = int(millis_str)
        return datetime.fromtimestamp(n / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


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


def stanford_meta_tags():
    return [
        {'system': 'urn:bina:src-portal',     'code': 'stanford.mychart'},
        {'system': 'urn:bina:src-org',        'code': 'stanford'},
        {'system': 'urn:bina:scraper-version','code': SCRAPER_VERSION},
    ]


def build_procedures(case):
    """One surgery case → 1..N Procedure resources (one per item in
    procedureList; the case itself groups them but FHIR wants distinct
    Procedures for each billable code)."""
    case_id = (case.get('caseId') or '').strip()
    surgery_date = case.get('surgeryDate')
    surgery_time = case.get('surgeryTime')
    when_iso = parse_mdy_time(surgery_date, surgery_time)
    status = PROC_STATUS_MAP.get(case.get('surgeryStatus'), 'unknown')
    loc = case.get('surgeryLocation') or {}
    loc_name = loc.get('name')
    loc_addr = loc.get('address') or {}
    addr_line = ', '.join(filter(None, [
        ', '.join(loc_addr.get('addressLines') or []),
        loc_addr.get('city'),
        loc_addr.get('usState'),
        loc_addr.get('zipCode'),
    ]))
    procs = case.get('procedureList') or []
    out = []
    for p in procs:
        name = (p.get('procedureName') or '').strip()
        if not name: continue
        provider = (p.get('providerName') or '').strip()
        npi = (p.get('providerNPI') or '').strip()
        fhir_id = det_id('stanford-proc', case_id or 'nocase', name, npi)
        r = {
            'resourceType': 'Procedure',
            'id': fhir_id,
            'identifier': [{'system': IDENT_PROCEDURE,
                            'value': f'{case_id}:{name}:{npi}'}],
            'status': status,
            'code': {'text': name},
            'subject': {'reference': STANFORD_PATIENT_REF},
            'meta': {'tag': stanford_meta_tags()},
        }
        if when_iso: r['performedDateTime'] = when_iso
        if provider:
            actor = {'display': provider}
            if npi:
                actor['identifier'] = {'system': 'http://hl7.org/fhir/sid/us-npi', 'value': npi}
            r['performer'] = [{'actor': actor}]
        if loc_name:
            r['location'] = {'display': loc_name}
        if addr_line:
            r.setdefault('note', []).append({'text': f'Location: {loc_name}. Address: {addr_line}'})
        out.append(r)
    return out


def build_appointment(a):
    csn = (a.get('csn') or '').strip()
    if not csn: return None
    fhir_id = det_id('stanford-appt', csn)
    when_iso = epoch_millis_to_iso(a.get('appointmentDateTime'))
    duration = a.get('duration')
    now = datetime.now(tz=timezone.utc).isoformat()
    status = 'booked'
    if when_iso and when_iso < now:
        status = 'fulfilled'
    participants = [{
        'actor': {'reference': STANFORD_PATIENT_REF},
        'status': 'accepted',
        'required': 'required',
    }]
    for pd in (a.get('providerDepartments') or []):
        prov = pd.get('provider') or {}
        dept = pd.get('department') or {}
        if prov.get('name'):
            actor = {'display': prov['name']}
            npi = prov.get('npi')
            if npi:
                actor['identifier'] = {'system': 'http://hl7.org/fhir/sid/us-npi', 'value': npi}
            participants.append({'actor': actor, 'status': 'accepted', 'required': 'required'})
        if dept.get('name'):
            participants.append({
                'actor': {'display': dept['name']},
                'status': 'accepted',
                'required': 'required',
            })
    r = {
        'resourceType': 'Appointment',
        'id': fhir_id,
        'identifier': [{'system': IDENT_APPOINTMENT, 'value': csn}],
        'status': status,
        'participant': participants,
        'meta': {'tag': stanford_meta_tags()},
    }
    if when_iso: r['start'] = when_iso
    if isinstance(duration, int) and duration > 0:
        r['minutesDuration'] = duration
    # Description from patientInstructions (strip HTML tags for a
    # readable text field; the full HTML rides along via note.text)
    pi = a.get('patientInstructions')
    if pi:
        # Some Stanford patientInstructions are strings, some are lists
        raw = pi if isinstance(pi, str) else ' '.join(str(x) for x in pi)
        # Strip HTML tags — keep it simple
        plain = re.sub(r'<[^>]+>', ' ', raw)
        plain = re.sub(r'\s+', ' ', plain).strip()
        if plain: r['description'] = plain[:2000]
    return r


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
    ap.add_argument('--wipe', action='store_true')
    args = ap.parse_args()
    apply = args.apply

    if args.wipe and apply:
        wipe_by_identifier_system('Procedure',   IDENT_PROCEDURE)
        wipe_by_identifier_system('Appointment', IDENT_APPOINTMENT)

    stats = Counter()
    bundle_entries = []

    # PROCEDURES
    f = latest_file('stanford-procedures-*.json')
    if f:
        d = json.load(open(f))
        cases = (d['list'].get('surgeryCaseList') or [])
        print(f'\nProcedures: {len(cases)} surgery cases  ({f.name})')
        for case in cases:
            for r in build_procedures(case):
                bundle_entries.append(put_entry(r))
                stats['procedures'] += 1
            stats['surgery-cases'] += 1
    else:
        print('  (no stanford-procedures-*.json found)')

    # APPOINTMENTS
    f = latest_file('stanford-appointments-*.json')
    if f:
        d = json.load(open(f))
        appts = (d['list'].get('appointments') or [])
        print(f'\nAppointments: {len(appts)} entries  ({f.name})')
        for a in appts:
            r = build_appointment(a)
            if r:
                bundle_entries.append(put_entry(r))
                stats['appointments'] += 1
    else:
        print('  (no stanford-appointments-*.json found)')

    print(f'\n=== Plan ===')
    for k, v in sorted(stats.items()):
        print(f'  {k:25s} {v}')
    print(f'  bundle entries total     {len(bundle_entries)}')

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
            body = e.read().decode('utf-8', errors='replace')[:500]
            print(f'  HTTP-{e.code}: {body}')
    print(f'  posted={posted}  failed={failed}')


if __name__ == '__main__':
    main()
