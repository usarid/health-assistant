#!/usr/bin/env python3
"""v3 → v2-bundle converter.

Takes v3 scrape output (separate visits.json + notes.json files saved via the
in-browser save dialog) and produces a FHIR transaction Bundle of Encounter +
DocumentReference resources, ready to PUT into v2 HAPI.

Per P-PHI-STAYS-LOCAL: this script runs purely on local files; nothing it
prints includes embedded clinical text.

Per P-STRUCTURED-FIRST: the input is already structured JSON from Epic's
APIs. No regex parsing required.

Uses the same deterministic ID scheme as the existing v2 raw-export
converters (tools/v2/convert_stanford_visits.py, tools/v2/convert_notes.py),
so v3-derived resources idempotently overwrite their v2-from-raw counterparts
(PUT semantics) rather than duplicating.

Schema notes:
  v3 visit.item       = Epic RenderedData entry (Csn, IsLocal, Date, etc.)
  v3 visit.response   = visit details API response (csn, encounterType, dat,
                        notesInfo, avsInfo, visitSummaryInfo {provider,
                        department, encounterDate}, externalDocUrl, orgID)
  v3 note.item        = the visits-result row (so note.item.item = RD entry,
                        note.item.response = visit details)
  v3 note.response    = LoadReportContent response (reportContent has HTML)

Output: tools/v3/out/{portal}-v3-bundle.json

Usage:
  python3 tools/v3/convert_to_v2_bundle.py stanford
  python3 tools/v3/convert_to_v2_bundle.py ucsf
"""

import argparse
import base64
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'lib'))
from fhir_utils import strip_html  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / 'out'

# Provenance namespaces — match tools/v2/convert_*.py exactly
NS_SRC_PORTAL    = 'urn:bina:source-portal'
NS_SRC_ORG       = 'urn:bina:source-org'
NS_SRC_ORG_ID    = 'urn:bina:source-org-id'
NS_SRC_FILE      = 'urn:bina:source-file'
NS_SCRAPER_VER   = 'urn:bina:scraper-version'
NS_CONVERTER_VER = 'urn:bina:converter-version'
NS_RUNTIME_VER   = 'urn:bina:runtime-version'
NS_EPIC_ENCOUNTER = 'urn:bina:epic:encounter'
NS_ENCOUNTER_FLAG = 'urn:bina:encounter-flag'

CONVERTER_VERSION = 'v3-to-v2.1.0'


# ── Portal config ──────────────────────────────────────────────────────
PORTAL_CFG = {
    'stanford': {
        'src_portal': 'stanford.mychart',
        'src_org': 'Stanford',
        'enc_prefix': 'enc-stanford',                              # MATCH convert_stanford_visits.py
        'docref_prefix': 'docref-stanford',
        'portal_enc_ns': 'urn:bina:portal:stanford:encounter',     # MATCH convert_stanford_visits.py
        'portal_note_ns': 'urn:bina:portal:stanford:note',
    },
    'ucsf': {
        'src_portal': 'ucsf.mychart',
        'src_org': 'UCSF',
        'enc_prefix': 'enc-ucsf',
        'docref_prefix': 'docref-ucsf',
        'portal_enc_ns': 'urn:bina:portal:ucsf:encounter',
        'portal_note_ns': 'urn:bina:portal:ucsf:note',
    },
}


# ── Helpers ────────────────────────────────────────────────────────────
def det_id(prefix, *parts):
    """Same 12-char MD5 scheme as tools/v2/convert_*.py."""
    raw = '|'.join(str(p) for p in parts if p)
    h = hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


_DATE_FORMATS = (
    '%A %B %d, %Y',                # "Friday March 27, 2026"
    '%B %d, %Y',
    '%b %d, %Y',                   # "Feb 19, 2026"
    '%b %d %Y',
    '%B %d %Y',
    '%m/%d/%Y %I:%M:%S %p',
    '%m/%d/%Y',
    '%Y-%m-%d',
)


def parse_epic_instant(s):
    if not s:
        return None
    m = re.search(r'/Date\((\d+)\)/', s)
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000).strftime('%Y-%m-%dT%H:%M:%S')


def parse_display_date(s):
    if not s:
        return None
    s = s.strip()
    s = re.sub(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+', '', s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            continue
    return None


def make_encounter_class(visit_type):
    vt = (visit_type or '').lower()
    if 'tele' in vt or 'video' in vt:
        return {'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode', 'code': 'VR', 'display': 'virtual'}
    if 'emergency' in vt or 'ed visit' in vt:
        return {'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode', 'code': 'EMER', 'display': 'emergency'}
    if 'hospital' in vt or 'inpatient' in vt or 'admission' in vt:
        return {'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode', 'code': 'IMP', 'display': 'inpatient encounter'}
    if 'refill' in vt or 'message' in vt or 'orders' in vt or 'rx' in vt:
        return {'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode', 'code': 'AMB', 'display': 'ambulatory'}
    return {'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode', 'code': 'AMB', 'display': 'ambulatory'}


# ── Visit-result accessors ─────────────────────────────────────────────
def v_rd(visit):
    """The RenderedData entry (Csn, IsLocal, Date, etc.)."""
    return visit.get('item') or {}


def v_resp(visit):
    """The visit-details API response (csn, notesInfo, visitSummaryInfo, …)."""
    return visit.get('response') or {}


def v_csn(visit):
    return v_rd(visit).get('Csn') or v_resp(visit).get('csn') or ''


def v_provider(visit):
    return ((v_resp(visit).get('visitSummaryInfo') or {}).get('provider') or
            v_rd(visit).get('PrimaryProviderName') or '').strip()


def v_dept(visit):
    return ((v_resp(visit).get('visitSummaryInfo') or {}).get('department') or
            (v_rd(visit).get('PrimaryDepartment') or {}).get('Name') or '').strip()


def v_type(visit):
    return (v_resp(visit).get('encounterType') or v_rd(visit).get('VisitTypeName') or '').strip()


def v_date(visit):
    """Best ISO date+time for the encounter start."""
    iso = (parse_epic_instant(v_rd(visit).get('Instant')) or
           parse_display_date((v_resp(visit).get('visitSummaryInfo') or {}).get('encounterDate')) or
           parse_display_date(v_rd(visit).get('Date')))
    return iso or ''


# ── Conversion ─────────────────────────────────────────────────────────
def convert_visit(visit, portal, src_file, scraper_ver):
    cfg = PORTAL_CFG[portal]
    rd = v_rd(visit)
    csn = v_csn(visit)
    is_local = rd.get('IsLocal')
    visit_type = v_type(visit)
    provider = v_provider(visit)
    dept = v_dept(visit)
    org_key = rd.get('_orgKey') or ''
    start_dt = v_date(visit)
    chief = ((v_resp(visit).get('visitSummaryInfo') or {}).get('chiefComplaint') or '').strip()
    org_name = (v_rd(visit).get('Organization') or {}).get('OrganizationName', '') or ''

    rid = det_id(cfg['enc_prefix'], csn or visit_type, start_dt or '', provider)

    identifiers = []
    if csn:
        identifiers.append({'system': cfg['portal_enc_ns'], 'value': csn})
        identifiers.append({'system': NS_EPIC_ENCOUNTER, 'value': csn})

    tags = [
        {'system': NS_SRC_PORTAL,    'code': cfg['src_portal']},
        {'system': NS_SRC_ORG,       'code': cfg['src_org']},
        {'system': NS_CONVERTER_VER, 'code': CONVERTER_VERSION},
        {'system': NS_SCRAPER_VER,   'code': scraper_ver},
        {'system': NS_RUNTIME_VER,   'code': (visit.get('_provenance') or {}).get('runtime_version', '')},
        {'system': NS_SRC_FILE,      'code': src_file},
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
                'code': 'ATND', 'display': 'attender',
            }]}],
        }]
    if dept:
        enc['serviceType'] = {'text': dept}
    if org_name:
        enc['serviceProvider'] = {'display': org_name}
    if chief:
        enc['reasonCode'] = [{'text': chief}]

    # Flags via meta.tag (FHIR R4 Encounter has no native field for these)
    if is_local is True:
        enc['meta']['tag'].append({'system': NS_ENCOUNTER_FLAG, 'code': 'is-local'})
    elif is_local is False:
        enc['meta']['tag'].append({'system': NS_ENCOUNTER_FLAG, 'code': 'cross-institution'})
    if rd.get('IsClinicalNoteAvailable'):
        enc['meta']['tag'].append({'system': NS_ENCOUNTER_FLAG, 'code': 'clinical-note-available'})
    return enc


def _has_visible_text(html):
    """Return True if the HTML has any visible (non-whitespace) text after tag
    stripping. Stanford's LoadReportContent returns a 755-char rendering
    skeleton with zero visible text for every note (see C-021 / task #19);
    we use this to detect that case and skip producing a content attachment,
    so the frontend's contentAvailable check correctly says 'no content yet'
    instead of showing a misleading 'View full note' link on an empty body."""
    if not html:
        return False
    plain = re.sub(r'<[^>]+>', '', html)
    plain = plain.replace('&nbsp;', ' ').strip()
    return bool(plain)


def convert_note(note, portal, src_file, scraper_ver):
    """v3 note → DocumentReference. Links to the Encounter via context.encounter
    using the same det_id derivation as the visit converter."""
    cfg = PORTAL_CFG[portal]
    visit = note.get('item') or {}             # the visits-result row
    resp = note.get('response') or {}
    report_html = resp.get('reportContent') or resp.get('html') or resp.get('reportHtml') or ''
    has_real_body = _has_visible_text(report_html)
    csn = v_csn(visit)
    provider = v_provider(visit)
    dept = v_dept(visit)
    visit_type = v_type(visit)
    start_dt = v_date(visit)
    date_iso = start_dt.split('T')[0] + 'T00:00:00Z' if start_dt else None

    # Same det_id scheme as the encounter — guarantees the linkage matches.
    # On multi-note visits (mobile list-view capture: one CSN, N notes
    # each behind its own VIEW NOTE button), _provenance.subIndex makes
    # each sub-note's DocumentReference rid distinct from its siblings
    # while sharing the same encounter rid.
    sub_index = (note.get('_provenance') or {}).get('subIndex')
    sub_key = f'__{sub_index}' if sub_index is not None else ''
    enc_rid = det_id(cfg['enc_prefix'], csn or visit_type, start_dt or '', provider)
    docref_rid = det_id(cfg['docref_prefix'], csn or visit_type, start_dt or '', provider + sub_key)

    # For multi-note (list-view) visits, prefer the per-note row label
    # over the visit type so a hospital stay's 12 notes render as
    # "Discharge Summary", "Care Plan Note", "Progress Notes" etc. rather
    # than 12 identical "Clinical Note - Hospital Encounter" rows.
    note_label = (note.get('_provenance') or {}).get('noteLabel') or ''
    note_title = ''
    note_author = ''
    if note_label:
        note_title = re.split(r'\s*signed\s+by\s+', note_label, maxsplit=1, flags=re.I)[0].strip()
        author_match = re.search(r'signed\s+by\s+(.+?)\s+on\s+', note_label, flags=re.I)
        if author_match:
            note_author = author_match.group(1).strip()

    type_text = 'Clinical Note'
    if note_title:
        type_text = f'Clinical Note - {note_title}'
    elif visit_type:
        type_text = f'Clinical Note - {visit_type}'

    tags = [
        {'system': NS_SRC_PORTAL,    'code': cfg['src_portal']},
        {'system': NS_SRC_ORG,       'code': cfg['src_org']},
        {'system': NS_CONVERTER_VER, 'code': CONVERTER_VERSION},
        {'system': NS_SCRAPER_VER,   'code': scraper_ver},
        {'system': NS_RUNTIME_VER,   'code': (note.get('_provenance') or {}).get('runtime_version', '')},
        {'system': NS_SRC_FILE,      'code': src_file},
    ]

    identifiers = []
    if csn:
        identifiers.append({'system': cfg['portal_note_ns'], 'value': csn})

    content = []
    if report_html and has_real_body:
        title_subject = note_title or visit_type
        content.append({
            'attachment': {
                'contentType': 'text/html',
                'data': base64.b64encode(report_html.encode('utf-8')).decode('ascii'),
                'title': f'{title_subject} - {start_dt}' if title_subject and start_dt else (title_subject or start_dt or ''),
            }
        })
    elif report_html:
        # Skeleton-only — mark the DocRef so a future re-scrape with the
        # real content endpoint (task #19) can identify these.
        # Frontend sees no content array → contentAvailable=false → no
        # misleading "View full note" toggle.
        tags.append({'system': NS_ENCOUNTER_FLAG, 'code': 'content-skeleton-only'})

    docref = {
        'resourceType': 'DocumentReference',
        'id': docref_rid,
        'status': 'current',
        'type': {'text': type_text},
        'meta': {'tag': tags},
        'context': {'encounter': [{'reference': f'Encounter/{enc_rid}'}]},
    }
    if identifiers:
        docref['identifier'] = identifiers
    if date_iso:
        docref['date'] = date_iso
    # Prefer the per-note signer (parsed from the multi-note label) over
    # the visit-level provider, so each sub-note's DocRef shows the actual
    # signing clinician.
    author_display = note_author or provider
    if author_display:
        docref['author'] = [{'display': author_display}]
    if dept:
        docref['custodian'] = {'display': dept}
    if content:
        docref['content'] = content
    if report_html and has_real_body:
        try:
            docref['description'] = strip_html(report_html)[:200]
        except Exception:
            pass
    return docref


# ── Main ───────────────────────────────────────────────────────────────
def convert_upcoming_visit(rd, portal, src_file, scraper_ver):
    """Upcoming visit → Encounter (status='planned'). Input is a flat
    RenderedData-style entry pulled from Epic's UpcomingVisits component
    (instance 5's Data.NextNDaysVisits + Data.LaterVisitsList + InProgressVisits).
    No visit-details API response — we work from the RD entry alone."""
    cfg = PORTAL_CFG[portal]
    csn = rd.get('Csn') or rd.get('Id') or ''
    visit_type = (rd.get('VisitTypeName') or '').strip()

    # PrimaryProviderName on upcoming visits is sometimes an object {Name: …}
    # not a string — guard.
    pp = rd.get('PrimaryProviderName')
    if isinstance(pp, str):
        provider = pp.strip()
    elif isinstance(pp, dict):
        provider = (pp.get('Name') or pp.get('FullName') or '').strip()
    else:
        provider = ''

    dept = ((rd.get('PrimaryDepartment') or {}).get('Name') or '').strip()
    org_key = rd.get('_orgKey') or ''
    org_name = (rd.get('Organization') or {}).get('OrganizationName', '') or ''
    start_dt = (parse_epic_instant(rd.get('Instant')) or
                parse_display_date(rd.get('PrimaryDate')) or
                parse_display_date(rd.get('Date')) or '')

    rid = det_id(cfg['enc_prefix'], csn or visit_type, start_dt or '', provider)

    identifiers = []
    if csn:
        identifiers.append({'system': cfg['portal_enc_ns'], 'value': csn})
        identifiers.append({'system': NS_EPIC_ENCOUNTER, 'value': csn})

    tags = [
        {'system': NS_SRC_PORTAL,    'code': cfg['src_portal']},
        {'system': NS_SRC_ORG,       'code': cfg['src_org']},
        {'system': NS_CONVERTER_VER, 'code': CONVERTER_VERSION},
        {'system': NS_SCRAPER_VER,   'code': scraper_ver},
        {'system': NS_SRC_FILE,      'code': src_file},
    ]
    if org_key:
        tags.append({'system': NS_SRC_ORG_ID, 'code': org_key})

    enc = {
        'resourceType': 'Encounter',
        'id': rid,
        'status': 'planned',     # FHIR R4 status for upcoming visits
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
                'code': 'ATND', 'display': 'attender',
            }]}],
        }]
    if dept:
        enc['serviceType'] = {'text': dept}
    if org_name:
        enc['serviceProvider'] = {'display': org_name}
    enc['meta']['tag'].append({'system': NS_ENCOUNTER_FLAG, 'code': 'upcoming'})
    if rd.get('IsLocal') is True:
        enc['meta']['tag'].append({'system': NS_ENCOUNTER_FLAG, 'code': 'is-local'})
    return enc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('portal', choices=['stanford', 'ucsf'])
    ap.add_argument('--visits', default=None, help='override visits file path')
    ap.add_argument('--notes', default=None, help='override notes file path')
    ap.add_argument('--upcoming', default=None,
                    help='upcoming-visits input file (shape: {upcoming: [rd, ...]}). '
                         'When this is the only input, the script emits only an '
                         'Encounter Bundle for the upcoming visits (no notes).')
    args = ap.parse_args()

    # Upcoming-only mode
    if args.upcoming and not args.visits and not args.notes:
        cfg = PORTAL_CFG[args.portal]
        with open(args.upcoming) as f:
            payload = json.load(f)
        rds = payload.get('upcoming') or payload.get('visits') or []
        print(f'=== v3 upcoming → v2-bundle: {args.portal} ===')
        print(f'  upcoming: {args.upcoming}')
        print(f'  entries to convert: {len(rds)}')
        src_file = f'{args.portal}-v3-upcoming-{datetime.now().strftime("%Y-%m-%d")}'
        scraper_ver = f'{cfg["src_portal"]}-v3-upcoming-2026-06'
        encounters = []
        seen = set()
        for rd in rds:
            e = convert_upcoming_visit(rd, args.portal, src_file, scraper_ver)
            if e['id'] in seen:
                continue
            seen.add(e['id'])
            encounters.append(e)
        class_dist = Counter(e['class']['display'] for e in encounters)
        print()
        print('Encounter class distribution:')
        for k, n in class_dist.most_common():
            print(f'  {n:>4d}  {k}')
        bundle = {
            'resourceType': 'Bundle', 'type': 'transaction',
            'entry': [
                {'resource': e, 'request': {'method': 'PUT', 'url': f'Encounter/{e["id"]}'}}
                for e in encounters
            ],
        }
        out = OUT_DIR / f'{args.portal}-v3-upcoming-bundle.json'
        with open(out, 'w') as f:
            json.dump(bundle, f)
        print(f'\nWrote: {out}  ({out.stat().st_size/1024:.0f} KB, {len(encounters)} entries)')
        return

    cfg = PORTAL_CFG[args.portal]
    visits_path = Path(args.visits) if args.visits else OUT_DIR / f'{args.portal}-v3-visits.json'
    notes_path = Path(args.notes) if args.notes else OUT_DIR / f'{args.portal}-v3-notes.json'

    print(f'=== v3 → v2-bundle: {args.portal} ===')
    print(f'  visits: {visits_path}')
    print(f'  notes:  {notes_path}')

    with open(visits_path) as f:
        visits = json.load(f)['visits']
    with open(notes_path) as f:
        notes = json.load(f)['notes']

    src_file = f'{args.portal}-v3-{datetime.now().strftime("%Y-%m-%d")}'
    scraper_ver = f'{cfg["src_portal"]}-v3-2026-06'

    print(f'  visits to convert: {len(visits)}')
    print(f'  notes to convert:  {len(notes)}')

    # Convert + dedup by deterministic ID. Epic's RenderedData occasionally
    # surfaces the same encounter twice (same CSN, different RD entries —
    # likely UI-driven duplicates such as a visit row + a linked-refill row).
    # PUT semantics would make these collide as a transaction-bundle error,
    # so we drop dupes here, preferring the first occurrence.
    encounters = []
    seen_enc_ids = set()
    enc_dupes = 0
    for v in visits:
        e = convert_visit(v, args.portal, src_file, scraper_ver)
        if e['id'] in seen_enc_ids:
            enc_dupes += 1
            continue
        seen_enc_ids.add(e['id'])
        encounters.append(e)
    if enc_dupes:
        print(f'  deduped {enc_dupes} duplicate Encounter IDs (same CSN+date+provider)')

    docrefs = []
    seen_docref_ids = set()
    docref_dupes = 0
    for n in notes:
        r = n.get('response') or {}
        if not (r.get('reportContent') or r.get('html') or r.get('reportHtml')):
            continue
        d = convert_note(n, args.portal, src_file, scraper_ver)
        if d['id'] in seen_docref_ids:
            docref_dupes += 1
            continue
        seen_docref_ids.add(d['id'])
        docrefs.append(d)
    if docref_dupes:
        print(f'  deduped {docref_dupes} duplicate DocumentReference IDs')

    # Summary
    class_dist = Counter(e['class']['display'] for e in encounters)
    print()
    print('Encounter class distribution:')
    for k, n in class_dist.most_common():
        print(f'  {n:>4d}  {k}')

    print()
    print(f'Encounters produced: {len(encounters)}')
    print(f'DocumentReferences produced: {len(docrefs)}')

    # Bundle
    entries = []
    for e in encounters:
        entries.append({'resource': e, 'request': {'method': 'PUT', 'url': f'Encounter/{e["id"]}'}})
    for d in docrefs:
        entries.append({'resource': d, 'request': {'method': 'PUT', 'url': f'DocumentReference/{d["id"]}'}})

    bundle = {'resourceType': 'Bundle', 'type': 'transaction', 'entry': entries}
    out_file = OUT_DIR / f'{args.portal}-v3-bundle.json'
    with open(out_file, 'w') as f:
        json.dump(bundle, f)
    sz = out_file.stat().st_size
    print(f'\nWrote: {out_file}  ({sz/1024:.0f} KB, {len(entries)} entries)')


if __name__ == '__main__':
    main()
