#!/usr/bin/env python3
"""Re-link v3 Stanford notes' context.encounter from v3-style IDs to v1's.

Background:
  - v3 produced 142 Stanford Encounters with det IDs like `enc-stanford-XXX`.
  - The dedup pass (tools/v3/dedup_v2_hapi.py) deleted the 71 of those
    whose det IDs collided with v2-from-raw's earlier `enc-stanford-XXX`
    set (PUT semantics had merged the resources; the v2.0.0 tag stuck
    around alongside the v3-to-v2.1.0 tag, and the dedup query found
    them). The notes that referenced those 71 now have dangling refs.
  - v1's Stanford encounters were copied wholesale into v2 by
    tools/v3/copy_v1_to_v2.py. They carry identifier
    `urn:stanford:myhealth:csn|<csn>` (from
    ingest/converters/stanford_visits.py).

This script:
  1. Indexes v1 Stanford encounters by CSN.
  2. Walks v3 Stanford notes (tag=v3-to-v2.1.0 & source-org=Stanford),
     pulls the CSN from their identifier
     `urn:bina:portal:stanford:note|<csn>`, looks up v1's Encounter
     for the same CSN, and PUTs the note back with
     context.encounter rewritten to `Encounter/<v1 id>`.
  3. For v3 Stanford Encounters still in v2 HAPI whose CSN is in
     v1 (so they're duplicates of v1's canonical encounter and
     nothing references them after re-link), deletes them. The few
     v3 Stanford Encounters with CSNs NOT in v1 (truly new visits
     since the v1 scrape) are kept.

Per P-PHI-STAYS-LOCAL: only counts are printed.
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

V2 = 'http://localhost:8090/fhir'

# Tag filters reused
TAG_V3 = 'urn:bina:converter-version|v3-to-v2.1.0'
TAG_SRC_STANFORD = 'urn:bina:source-org|Stanford'

# Identifier systems
V1_STANFORD_CSN_SYS = 'urn:stanford:myhealth:csn'
V3_STANFORD_NOTE_SYS = 'urn:bina:portal:stanford:note'
V3_STANFORD_ENC_SYS = 'urn:bina:portal:stanford:encounter'


def http_request(method, url, body=None, headers=None):
    h = dict(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read()
            return r.status, json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = None
        return e.code, body


def page_resources(url):
    while url:
        status, bundle = http_request('GET', url)
        if not bundle:
            break
        for entry in bundle.get('entry', []) or []:
            r = entry.get('resource')
            if r:
                yield r
        url = None
        for link in bundle.get('link', []) or []:
            if link.get('relation') == 'next':
                url = link.get('url')
                break


def index_v1_stanford_encounters():
    """Return {csn: encounter_id} for v1's Stanford Encounters."""
    qs = urllib.parse.urlencode([
        ('identifier', f'{V1_STANFORD_CSN_SYS}|'),
        ('_count', '200'),
    ])
    url = f'{V2}/Encounter?{qs}'
    out = {}
    for enc in page_resources(url):
        for ident in enc.get('identifier', []) or []:
            if ident.get('system') == V1_STANFORD_CSN_SYS and ident.get('value'):
                out[ident['value']] = enc['id']
                break
    return out


def csn_from_identifiers(resource, system):
    for ident in resource.get('identifier', []) or []:
        if ident.get('system') == system and ident.get('value'):
            return ident['value']
    return None


def fetch_v3_stanford(resource_type):
    qs = urllib.parse.urlencode([
        ('_tag', TAG_V3),
        ('_tag', TAG_SRC_STANFORD),
        ('_count', '200'),
    ])
    return list(page_resources(f'{V2}/{resource_type}?{qs}'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    print('=== Indexing v1 Stanford encounters by CSN ===')
    v1_map = index_v1_stanford_encounters()
    print(f'  {len(v1_map)} v1 Stanford Encounters with CSN identifier')

    print()
    print('=== v3 Stanford notes — re-linking context.encounter ===')
    notes = fetch_v3_stanford('DocumentReference')
    print(f'  fetched {len(notes)} v3 Stanford DocumentReferences')

    relinked = 0
    already_v1 = 0
    no_csn = 0
    no_v1_match = 0
    failed = 0
    for note in notes:
        csn = csn_from_identifiers(note, V3_STANFORD_NOTE_SYS)
        if not csn:
            no_csn += 1
            continue
        v1_enc_id = v1_map.get(csn)
        if not v1_enc_id:
            no_v1_match += 1
            continue
        new_ref = f'Encounter/{v1_enc_id}'
        cur = (((note.get('context') or {}).get('encounter') or [{}])[0].get('reference') or '')
        if cur == new_ref:
            already_v1 += 1
            continue
        if args.dry_run:
            relinked += 1
            continue
        note.setdefault('context', {})['encounter'] = [{'reference': new_ref}]
        status, body = http_request(
            'PUT',
            f'{V2}/DocumentReference/{note["id"]}',
            body=note,
            headers={'Content-Type': 'application/fhir+json'},
        )
        if status in (200, 201):
            relinked += 1
            sys.stdout.write(f'\r  re-linked {relinked}/{len(notes)}')
            sys.stdout.flush()
        else:
            failed += 1
    sys.stdout.write('\r')
    print(f'  re-linked: {relinked}')
    print(f'  already_pointing_at_v1: {already_v1}')
    print(f'  no_csn_in_identifier: {no_csn}')
    print(f'  no_v1_match (truly-new visits): {no_v1_match}')
    print(f'  failed: {failed}')

    print()
    print('=== v3 Stanford encounters — delete those whose CSN is now covered by v1 ===')
    encs = fetch_v3_stanford('Encounter')
    print(f'  fetched {len(encs)} v3 Stanford Encounter resources')
    deleted = 0
    kept_new = 0
    no_csn_enc = 0
    failed_del = 0
    for enc in encs:
        csn = csn_from_identifiers(enc, V3_STANFORD_ENC_SYS) or \
              csn_from_identifiers(enc, 'urn:bina:epic:encounter')
        if not csn:
            no_csn_enc += 1
            continue
        if csn not in v1_map:
            kept_new += 1
            continue
        if args.dry_run:
            deleted += 1
            continue
        status, body = http_request('DELETE', f'{V2}/Encounter/{enc["id"]}')
        if status in (200, 204):
            deleted += 1
            sys.stdout.write(f'\r  deleted {deleted}/{len(encs)}')
            sys.stdout.flush()
        else:
            failed_del += 1
    sys.stdout.write('\r')
    print(f'  deleted (CSN in v1 — was duplicate): {deleted}')
    print(f'  kept_new (CSN not in v1 — truly new visits): {kept_new}')
    print(f'  no_csn_in_identifier: {no_csn_enc}')
    print(f'  failed_to_delete: {failed_del}')


if __name__ == '__main__':
    main()
