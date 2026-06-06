#!/usr/bin/env python3
"""Bulk-copy resources from v1 HAPI (port 8080) to v2 HAPI (port 8090).

Preserves resource IDs (PUTs each resource at its original ID) so internal
references between v1 resources (Encounter → Patient, Observation → Encounter,
MedicationRequest → Patient, DocumentReference → Encounter, etc.) keep
working in v2.

PUT semantics → idempotent: re-running this script just upserts the same
resources at the same IDs.

Per P-PHI-STAYS-LOCAL: only counts are printed; resource content stays on
the wire between v1 HAPI and v2 HAPI, never enters this script's stdout.

Usage:
  python3 tools/v3/copy_v1_to_v2.py                      # all missing types
  python3 tools/v3/copy_v1_to_v2.py Observation          # one or more types
  python3 tools/v3/copy_v1_to_v2.py --dry-run            # report what would copy
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

V1_BASE = 'http://localhost:8080/fhir'
V2_BASE = 'http://localhost:8090/fhir'

# Resource types to copy by default. Order matters for referential integrity:
# Patient first (referenced by all), then encounters, then everything else.
DEFAULT_TYPES = [
    'Patient',
    'Encounter',
    'Binary',
    'Observation',
    'DiagnosticReport',
    'MedicationRequest',
    'MedicationStatement',
    'Condition',
    'AllergyIntolerance',
    'Procedure',
    'Immunization',
    'CarePlan',
    'ServiceRequest',
    'DocumentReference',
    'Communication',
]

# How many resources per transaction bundle when PUTting to v2.
BATCH_SIZE = 100
# How many to ask v1 for at a time when paginating.
PAGE_SIZE = 200


def http_get_json(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read())


def http_post_json(url, payload):
    """POST, returning (status, body|None, err_text|None). Doesn't raise on HTTP errors."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/fhir+json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = None
        return e.code, body, str(e)
    except Exception as e:
        return 0, None, str(e)


def fetch_count(base, rtype):
    try:
        d = http_get_json(f'{base}/{rtype}?_summary=count')
        return d.get('total', 0)
    except Exception:
        return 0


def iter_resources(base, rtype, page_size=PAGE_SIZE):
    """Yield resources from v1 by paging through search results."""
    next_url = f'{base}/{rtype}?_count={page_size}'
    while next_url:
        bundle = http_get_json(next_url)
        for entry in bundle.get('entry', []) or []:
            resource = entry.get('resource')
            if resource and resource.get('id'):
                yield resource
        next_url = None
        for link in bundle.get('link', []) or []:
            if link.get('relation') == 'next':
                next_url = link.get('url')
                break


def transaction_bundle(resources):
    """Build a FHIR transaction bundle of PUTs preserving each resource's ID."""
    return {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': [
            {'resource': r, 'request': {'method': 'PUT', 'url': f'{r["resourceType"]}/{r["id"]}'}}
            for r in resources
        ],
    }


def post_batch(resources):
    payload = transaction_bundle(resources)
    status, body, err = http_post_json(V2_BASE + '/', payload)
    if body is None:
        return status, 0, 0, (err or 'no body')[:200]
    if body.get('resourceType') == 'OperationOutcome':
        # Report the first error diagnostic without leaking PHI
        diag = (body.get('issue') or [{}])[0].get('diagnostics', '')[:200]
        return status, 0, 0, diag
    entries = body.get('entry', []) or []
    created = sum(1 for e in entries if e.get('response', {}).get('status', '').startswith('201'))
    updated = sum(1 for e in entries if e.get('response', {}).get('status', '').startswith('200'))
    return status, created, updated, None


def post_batch_with_split_fallback(resources):
    """Try the full batch; if it errors as a transaction (all-or-nothing),
    binary-split until we identify the culprit(s) and skip only those."""
    status, c, u, err = post_batch(resources)
    if not err:
        return c, u, 0, []
    if len(resources) == 1:
        # Single resource, still errors → log + skip
        rid = f'{resources[0]["resourceType"]}/{resources[0].get("id","?")}'
        return 0, 0, 1, [(rid, err)]
    # Split and recurse
    mid = len(resources) // 2
    c1, u1, f1, errs1 = post_batch_with_split_fallback(resources[:mid])
    c2, u2, f2, errs2 = post_batch_with_split_fallback(resources[mid:])
    return c1 + c2, u1 + u2, f1 + f2, errs1 + errs2


def copy_resource_type(rtype, dry_run=False):
    v1_count = fetch_count(V1_BASE, rtype)
    v2_before = fetch_count(V2_BASE, rtype)
    print(f'\n--- {rtype} ---')
    print(f'  v1: {v1_count}  v2-before: {v2_before}')
    if v1_count == 0:
        print('  (nothing to copy)')
        return
    if dry_run:
        print(f'  [dry-run] would copy {v1_count} resources')
        return

    batch = []
    total_created = 0
    total_updated = 0
    total_failed = 0
    failures = []
    t0 = time.time()
    for resource in iter_resources(V1_BASE, rtype):
        batch.append(resource)
        if len(batch) >= BATCH_SIZE:
            c, u, f, errs = post_batch_with_split_fallback(batch)
            total_created += c
            total_updated += u
            total_failed += f
            failures.extend(errs)
            batch = []
            sys.stdout.write(f'\r  {total_created + total_updated}/{v1_count} done...')
            sys.stdout.flush()
    if batch:
        c, u, f, errs = post_batch_with_split_fallback(batch)
        total_created += c
        total_updated += u
        total_failed += f
        failures.extend(errs)
    sys.stdout.write('\r')
    elapsed = time.time() - t0
    v2_after = fetch_count(V2_BASE, rtype)
    print(f'  copied: created={total_created}  updated={total_updated}  failed={total_failed}  '
          f'time={elapsed:.1f}s')
    print(f'  v2-after: {v2_after}')
    if failures:
        # Show first few failure diagnostics (no resource bodies — IDs + error text only)
        print(f'  failures (first 3 of {len(failures)}):')
        for rid, err in failures[:3]:
            print(f'    {rid}: {err[:120]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('types', nargs='*', help='resource types to copy (default: all in DEFAULT_TYPES)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    types = args.types or DEFAULT_TYPES
    print(f'=== Bulk copy v1 ({V1_BASE}) → v2 ({V2_BASE}) ===')
    print(f'Types: {types}')
    if args.dry_run:
        print('(dry run — no writes)')
    for t in types:
        copy_resource_type(t, dry_run=args.dry_run)
    print('\nDone.')


if __name__ == '__main__':
    main()
