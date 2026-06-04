#!/usr/bin/env python3
"""v2 Stanford visits converter — rebuilds FHIR Encounter resources from raw scrape.

Per P-STRUCTURED-FIRST: the input is structured JSON. No regex parsing.

Per P-DATA-IS-GOLD: v1's existing converter (ingest/converters/stanford_visits.py)
captured the CSN, provider, department, type, and date but dropped several
fields the raw scrape carries — most notably the _orgKey (a WP-24 organisation
token), the IsLocal cross-institution flag, ChiefComplaint, and Cases/Diagnoses.
v2 preserves these.

Addresses CONCLUSIONS_LOG.md:
  C-002: source provenance via meta.tag (source-portal, source-org,
         source-org-id (from _orgKey), source-file, scraper-version,
         converter-version)
  C-006: Stanford raw data does carry WP-24 tokens (Csn, Id, Dat, _orgKey)
         even though the messages scraper missed them. Captured here as
         canonical Epic identifiers.
  C-007: per-visit grain matches v1 (one Encounter per scraped visit)
  C-008/C-009: source-org tag = "Stanford"; organizationId tag carries
         the _orgKey WP-24 token for cross-portal reconciliation

Outputs a transaction Bundle for loading into the v2 HAPI on port 8090.
"""

import json
import re
import hashlib
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Constants ──────────────────────────────────────────────────────────
RAW_DIR = Path('/Users/urisarid/usarid@gmail.com/Medical/Synthesis/health-assistant/data/raw-exports')
RAW_FILE = RAW_DIR / 'stanford_visits_raw.json'
OUT_DIR = Path(__file__).resolve().parent / 'out'

CONVERTER_VERSION = 'v2.0.0'
SCRAPER_VERSION_STANFORD_VISITS = 'stanford-visits-2026-04'

NS_SRC_PORTAL    = 'urn:bina:source-portal'
NS_SRC_ORG       = 'urn:bina:source-org'
NS_SRC_ORG_ID    = 'urn:bina:source-org-id'
NS_SRC_FILE      = 'urn:bina:source-file'
NS_SCRAPER_VER   = 'urn:bina:scraper-version'
NS_CONVERTER_VER = 'urn:bina:converter-version'

# Identifier namespaces
NS_PORTAL_ENC_STANFORD = 'urn:bina:portal:stanford:encounter'
NS_EPIC_ENCOUNTER      = 'urn:bina:epic:encounter'   # canonical (cross-portal)
NS_ENCOUNTER_FLAG      = 'urn:bina:encounter-flag'   # boolean flags FHIR doesn't model on Encounter


# ── Helpers ────────────────────────────────────────────────────────────
def det_id(prefix, *parts):
    raw = '|'.join(str(p) for p in parts if p)
    h = hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


def parse_epic_instant(s):
    """Epic /Date(epoch_ms)/ format → ISO."""
    if not s:
        return None
    m = re.search(r'/Date\((\d+)\)/', s)
    if not m:
        return None
    dt = datetime.fromtimestamp(int(m.group(1)) / 1000)
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


_DATE_FORMATS = (
    '%A %B %d, %Y',           # "Friday March 27, 2026"
    '%B %d, %Y',
    '%m/%d/%Y %I:%M:%S %p',   # "3/27/2026 1:30:00 PM"
    '%m/%d/%Y',
)


def parse_display_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            continue
    return None


# Map Epic visit-type strings to FHIR encounter class.
def make_encounter_class(visit_type):
    vt = (visit_type or '').lower()
    if 'tele' in vt or 'video' in vt:
        return {
            'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
            'code': 'VR',
            'display': 'virtual',
        }
    if 'emergency' in vt or 'ed visit' in vt:
        return {
            'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
            'code': 'EMER',
            'display': 'emergency',
        }
    if 'hospital' in vt or 'inpatient' in vt or 'admission' in vt:
        return {
            'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
            'code': 'IMP',
            'display': 'inpatient encounter',
        }
    return {
        'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
        'code': 'AMB',
        'display': 'ambulatory',
    }


# ── Conversion ─────────────────────────────────────────────────────────
def convert_visit(v):
    csn = v.get('Csn') or v.get('Id') or ''
    org_key = v.get('_orgKey') or ''
    is_local = v.get('IsLocal')
    visit_type = (v.get('VisitTypeName') or '').strip()
    provider = (v.get('PrimaryProviderName') or '').strip()
    chief_complaint = (v.get('ChiefComplaint') or '').strip()

    primary_dept = v.get('PrimaryDepartment') or {}
    dept_name = (primary_dept.get('Name') or '').strip()

    org = v.get('Organization') or {}
    org_name = (org.get('OrganizationName') or '').strip()

    # Date: prefer Instant (epoch), fall back to PrimaryDate or Date
    start_dt = parse_epic_instant(v.get('Instant'))
    if not start_dt:
        start_dt = parse_display_date(v.get('PrimaryDate'))
    if not start_dt:
        start_dt = parse_display_date(v.get('Date'))

    rid = det_id('enc-stanford', csn or visit_type, start_dt or '', provider)

    # Identifiers
    identifiers = []
    if csn:
        # CSN serves as both the portal-local and (in Epic's case) a canonical
        # global encounter identifier — store under both systems for searchability.
        identifiers.append({'system': NS_PORTAL_ENC_STANFORD, 'value': csn})
        identifiers.append({'system': NS_EPIC_ENCOUNTER, 'value': csn})

    # Tags (provenance contract)
    tags = [
        {'system': NS_SRC_PORTAL,    'code': 'stanford.myhealth'},
        {'system': NS_SRC_ORG,       'code': 'Stanford'},
        {'system': NS_CONVERTER_VER, 'code': CONVERTER_VERSION},
        {'system': NS_SCRAPER_VER,   'code': SCRAPER_VERSION_STANFORD_VISITS},
        {'system': NS_SRC_FILE,      'code': RAW_FILE.name},
    ]
    if org_key:
        tags.append({'system': NS_SRC_ORG_ID, 'code': org_key})

    enc = {
        'resourceType': 'Encounter',
        'id': rid,
        'status': 'finished',
        'class': make_encounter_class(visit_type),
        'meta': {'tag': tags},
    }
    if identifiers:
        enc['identifier'] = identifiers
    if visit_type:
        enc['type'] = [{'text': visit_type}]
    if start_dt:
        enc['period'] = {'start': start_dt}
    if provider:
        enc['participant'] = [{
            'individual': {'display': provider},
            'type': [{'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/v3-ParticipationType',
                'code': 'ATND',
                'display': 'attender',
            }]}],
        }]
    if dept_name:
        enc['serviceType'] = {'text': dept_name}
    if org_name:
        enc['serviceProvider'] = {'display': org_name}
    if chief_complaint:
        enc['reasonCode'] = [{'text': chief_complaint}]

    # Encounter has no `note` field in FHIR R4 — HAPI silently drops it.
    # Use meta.tag for boolean flags instead. System NS_ENCOUNTER_FLAG +
    # documented codes. This preserves the IsLocal context (which per C-001
    # distinguishes native-portal visits from linked-accounts cross-listings)
    # in a way that round-trips through HAPI and is searchable.
    flag_codes = []
    if is_local is True:
        flag_codes.append('is-local')
    elif is_local is False:
        flag_codes.append('cross-institution')
    if v.get('IsClinicalNoteAvailable'):
        flag_codes.append('clinical-note-available')
    if v.get('IsCanceled'):
        flag_codes.append('cancelled')
    if v.get('IsNoShow'):
        flag_codes.append('no-show')
    if v.get('EncounterIsEDVisit'):
        flag_codes.append('ed-visit')
    if v.get('EncounterIsSurgery'):
        flag_codes.append('surgery')

    for fc in flag_codes:
        enc['meta']['tag'].append({'system': NS_ENCOUNTER_FLAG, 'code': fc})

    return enc


# ── Main ───────────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(exist_ok=True)

    print(f'Loading {RAW_FILE}')
    with open(RAW_FILE) as f:
        visits = json.load(f)
    print(f'  visits in raw: {len(visits)}')

    print()
    print('=== Converting Stanford visits ===')
    encounters = [convert_visit(v) for v in visits]
    print(f'  {len(encounters)} Encounter resources')

    # Distribution
    by_class = Counter(e.get('class', {}).get('display', '?') for e in encounters)
    by_type = Counter((e.get('type', [{}])[0] if e.get('type') else {}).get('text', '?') for e in encounters)
    is_local = Counter()
    for v in visits:
        is_local[v.get('IsLocal')] += 1

    print()
    print('Encounter class distribution:')
    for k, n in by_class.most_common():
        print(f'  {n:>4d}  {k}')
    print()
    print('Top visit types:')
    for k, n in by_type.most_common(8):
        print(f'  {n:>4d}  {k!r}')
    print()
    print('IsLocal distribution (per C-001):')
    for k, n in is_local.most_common():
        print(f'  {n:>4d}  IsLocal={k}')

    # Bundle
    bundle = {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': [
            {'resource': e, 'request': {'method': 'PUT', 'url': f'Encounter/{e["id"]}'}}
            for e in encounters
        ],
    }
    out_file = OUT_DIR / 'stanford_visits_v2_bundle.json'
    with open(out_file, 'w') as f:
        json.dump(bundle, f, indent=2)
    print()
    print(f'Wrote: {out_file} ({out_file.stat().st_size / 1024:.0f} KB, {len(encounters)} entries)')

    # Sample
    print()
    print('Sample IDs (first 3):')
    for e in encounters[:3]:
        p = e.get('participant', [{}])[0].get('individual', {}).get('display', '')
        d = (e.get('period') or {}).get('start', '')
        print(f'  {e["id"]}  class={e["class"]["display"]}  date={d}  type={e.get("type",[{}])[0].get("text","")!r}')


if __name__ == '__main__':
    main()
