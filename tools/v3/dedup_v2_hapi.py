#!/usr/bin/env python3
"""Delete v2-rebuild + v3 duplicates that compete with v1's canonical resources.

After tools/v3/copy_v1_to_v2.py wholesale-mirrors v1 → v2, several resource
types end up with duplicates:
  - Communications: 692 v1 + 692 v2-rebuild (tagged converter-version|v2.0.0)
  - Encounters:     396 v1 + 139 v2-rebuild Stanford + 384 v3 (UCSF+Stanford)
  - DocRefs:        673 v1 + 108 v2-rebuild UCSF + 215 v3 (UCSF+Stanford)

This script deletes the v2-rebuild + v3-UCSF duplicates that v1 already
covers. v3 Stanford resources (Encounter + DocumentReference) are KEPT
because they carry new content (no Stanford notes existed in v1 with
real body content; C-017/C-018/C-019).

Identification is by tag, so v1 resources (which have no urn:bina:*
tags at all) are not touched. Verified pre-dedup with:
  $ curl http://localhost:8080/fhir/Encounter?_tag=urn:bina:source-org|UCSF&_count=0
  → total: 0   (v1 has no source-org tags)

Per P-PHI-STAYS-LOCAL: only counts are printed.

Usage:
  python3 tools/v3/dedup_v2_hapi.py            # do the deletions
  python3 tools/v3/dedup_v2_hapi.py --dry-run  # just report
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

V2 = 'http://localhost:8090/fhir'

# Resources to delete: (label, resource_type, tag_query_params_dict)
# Each tag query produces a search like ?_tag=ns%7Cvalue&_tag=ns2%7Cvalue2
# Multiple _tag params AND together.
PASSES = [
    ('v2-rebuild Communications',
     'Communication',
     {'_tag': ['urn:bina:converter-version|v2.0.0']}),

    ('v2-from-raw Stanford Encounters',
     'Encounter',
     {'_tag': ['urn:bina:converter-version|v2.0.0']}),

    ('v2-from-raw UCSF DocumentReferences (orphans, no encounter link)',
     'DocumentReference',
     {'_tag': ['urn:bina:converter-version|v2.0.0']}),

    ('v3 UCSF Encounters (v1 already has these)',
     'Encounter',
     {'_tag': ['urn:bina:converter-version|v3-to-v2.1.0',
               'urn:bina:source-org|UCSF']}),

    ('v3 UCSF DocumentReferences (v1 already has these notes)',
     'DocumentReference',
     {'_tag': ['urn:bina:converter-version|v3-to-v2.1.0',
               'urn:bina:source-org|UCSF']}),

    # Stanford v3 resources are KEPT — they carry the new content
    # (Stanford notes never previously scraped; closes C-017/C-018).
]


def http_request(method, url, body=None, headers=None):
    h = dict(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read()
            try:
                return r.status, json.loads(txt) if txt else None
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = None
        return e.code, body


def fetch_ids(resource_type, tag_params, page_size=200):
    """Return all resource IDs matching the tag filter."""
    qs = urllib.parse.urlencode([('_tag', t) for t in tag_params['_tag']] +
                                [('_count', str(page_size))])
    url = f'{V2}/{resource_type}?{qs}'
    ids = []
    while url:
        status, bundle = http_request('GET', url)
        if not bundle:
            break
        for entry in bundle.get('entry', []) or []:
            r = entry.get('resource')
            if r and r.get('id'):
                ids.append(r['id'])
        url = None
        for link in bundle.get('link', []) or []:
            if link.get('relation') == 'next':
                url = link.get('url')
                break
    return ids


def delete_in_batches(resource_type, ids, batch_size=100):
    """Delete using transaction bundles of N entries each."""
    deleted = 0
    failed = 0
    failures = []
    for i in range(0, len(ids), batch_size):
        chunk = ids[i:i + batch_size]
        bundle = {
            'resourceType': 'Bundle',
            'type': 'transaction',
            'entry': [
                {'request': {'method': 'DELETE', 'url': f'{resource_type}/{rid}'}}
                for rid in chunk
            ],
        }
        status, body = http_request('POST', V2 + '/', body=bundle,
                                    headers={'Content-Type': 'application/fhir+json'})
        if body and body.get('resourceType') == 'OperationOutcome':
            failed += len(chunk)
            diag = (body.get('issue') or [{}])[0].get('diagnostics', '')[:200]
            failures.append(diag)
        else:
            entries = (body or {}).get('entry', []) or []
            for e in entries:
                rs = e.get('response', {}).get('status', '')
                if rs.startswith('204') or rs.startswith('200'):
                    deleted += 1
                else:
                    failed += 1
        sys.stdout.write(f'\r    deleted {deleted}/{len(ids)}')
        sys.stdout.flush()
    sys.stdout.write('\r')
    return deleted, failed, failures


def fetch_total(resource_type, tag_params):
    qs = urllib.parse.urlencode([('_tag', t) for t in tag_params['_tag']] +
                                [('_count', '0')])
    status, body = http_request('GET', f'{V2}/{resource_type}?{qs}')
    if not body:
        return -1
    return body.get('total', 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    print(f'=== v2 HAPI dedup ===')
    print(f'Target: {V2}')
    if args.dry_run:
        print('(dry run — no deletes)')
    print()

    grand_deleted = 0
    grand_failed = 0
    for label, rtype, tag_params in PASSES:
        before = fetch_total(rtype, tag_params)
        print(f'--- {label} ---')
        print(f'  resource: {rtype}  match: {before}')
        if args.dry_run:
            print(f'  [dry-run] would delete {before}')
            continue
        if before == 0:
            print(f'  (nothing to delete)')
            continue
        t0 = time.time()
        ids = fetch_ids(rtype, tag_params)
        print(f'  fetched {len(ids)} IDs in {time.time()-t0:.1f}s; deleting...')
        deleted, failed, failures = delete_in_batches(rtype, ids)
        grand_deleted += deleted
        grand_failed += failed
        after = fetch_total(rtype, tag_params)
        elapsed = time.time() - t0
        print(f'  deleted={deleted}  failed={failed}  remaining-matching={after}  time={elapsed:.1f}s')
        if failures:
            print(f'  first failure: {failures[0]}')
    print()
    print(f'Total: deleted={grand_deleted}  failed={grand_failed}')


if __name__ == '__main__':
    main()
