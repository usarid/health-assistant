#!/usr/bin/env python3
"""Translate a mobile-app stanford-message-batch-*.json file into FHIR R4
Communication resources, optionally wipe the existing stanford-msg
records from v2 HAPI, and POST the new batch.

Per-message mapping (from Stanford's myHealthMessage shape captured via
the /Private/Ajax/V1/Mailbox/Message endpoint):

  Communication {
    id:           deterministic from "stanford-msg-<messageId>"
    identifier:   urn:stanford:myhealth:message + messageId
    status:       completed
    sent:         dateSent (ISO 8601)
    sender:       inbound  → {display: senderName}    (practitioner)
                  outbound → {display: "Uri Sarid"}   (patient)
    recipient:    inverse of sender, in single-element array
    payload[0]:   contentString = body
    topic:        {text: title}
    extension:    urn:bina:thread-key (synthesized for grouping)
    meta.tag:     src-portal=stanford.mychart, src-org=stanford,
                  src-folder=inbox|outbox, scraper-version, src-file
  }

Thread key synthesis: (other_party + normalized_title + month_cluster)
groups reply chains across the same correspondent and subject within a
~month window. Real per-thread linkage via inResponseTo can be added
later when Stanford's API exposes it (not in the per-message endpoint
we've seen so far — bodies just contain inlined quoted text).

Per P-PHI-STAYS-LOCAL: this script reads + transforms message bodies
locally, POSTs them to v2 HAPI on localhost. Nothing leaves the host.

Usage:
  python3 convert_mobile_messages_to_fhir.py <batch.json> [--wipe] [--dry-run]
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
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'tools'))
from portal_registry import get_portal  # noqa: E402

HAPI_BASE = 'http://localhost:8090/fhir'

# Portal-derived constants populated in main() from --portal.
PORTAL = None
IDENT_SYSTEM = None
SRC_PORTAL_TAG = None
ID_PREFIX_MSG = None

NS_SRC_PORTAL = 'http://binahealth.org/ns/src-portal'
NS_SRC_ORG = 'http://binahealth.org/ns/src-org'
NS_SRC_FILE = 'http://binahealth.org/ns/src-file'
NS_SCRAPER_VER = 'http://binahealth.org/ns/scraper-version'
NS_SRC_FOLDER = 'http://binahealth.org/ns/src-folder'
NS_THREAD_KEY = 'http://binahealth.org/ns/thread-key'

PATIENT_DISPLAY = 'Uri Sarid'  # the only user; keeps things readable in HAPI

SCRAPER_VERSION = 'mobile-flutter-2026-06-17'


def det_id(prefix: str, *parts: str) -> str:
    """Stable resource ID — sha1 of the concatenated parts, prefix + 12 hex."""
    h = hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


def normalize_title(t: str) -> str:
    """Strip RE: / FW: prefixes and lowercase + collapse whitespace.
    Used for thread-key synthesis."""
    s = re.sub(r'^(re:|fw:|fwd:)\s*', '', t.strip(), flags=re.I)
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s


def month_cluster(date_iso: str) -> str:
    """Coarse time bucket: YYYY-MM. Two messages on the same subject
    from the same correspondent within ~30 days are almost always one
    thread; cross-month splits are usually intentional new threads."""
    if not date_iso:
        return 'unknown'
    return date_iso[:7]  # 'YYYY-MM'


def thread_key(folder: str, msg: dict) -> str:
    """Group messages into conversations by (other-party + normalized
    subject + month). Best-effort heuristic until Stanford exposes a
    real thread ID."""
    sender = (msg.get('senderName') or '').strip()
    recipient = (msg.get('recipientName') or '').strip()
    other = recipient if folder == 'outbox' else sender
    subj = normalize_title(msg.get('title') or '')
    mc = month_cluster(msg.get('dateSent') or '')
    return f'{other}|{subj}|{mc}'


def message_to_communication(record: dict, src_file: str) -> dict:
    """Build one FHIR Communication from a captured message record."""
    folder = record['folder']
    msg = record['data']['myHealthMessage']
    msg_id = msg['id']

    sender_name = (msg.get('senderName') or '').strip()
    recipient_name = (msg.get('recipientName') or '').strip()
    title = (msg.get('title') or '').strip()
    body = msg.get('body') or ''
    date_sent = msg.get('dateSent') or ''

    if folder == 'inbox':
        sender = {'display': sender_name or 'Unknown clinician'}
        recipient = [{'display': PATIENT_DISPLAY}]
    else:  # outbox
        sender = {'display': PATIENT_DISPLAY}
        recipient = [{'display': recipient_name or 'Unknown clinician'}]

    rid = det_id(ID_PREFIX_MSG, msg_id)

    tags = [
        {'system': NS_SRC_PORTAL, 'code': SRC_PORTAL_TAG},
        {'system': NS_SRC_ORG, 'code': PORTAL.id},
        {'system': NS_SRC_FOLDER, 'code': folder},
        {'system': NS_SCRAPER_VER, 'code': SCRAPER_VERSION},
        {'system': NS_SRC_FILE, 'code': src_file},
    ]
    if msg.get('myHealthAttachments'):
        tags.append({'system': NS_SRC_FOLDER, 'code': 'has-attachments'})

    comm = {
        'resourceType': 'Communication',
        'id': rid,
        'status': 'completed',
        'identifier': [{'system': IDENT_SYSTEM, 'value': msg_id}],
        'sent': date_sent or None,
        'sender': sender,
        'recipient': recipient,
        'topic': {'text': title} if title else None,
        'payload': [{'contentString': body}] if body else [],
        'extension': [
            {'url': NS_THREAD_KEY, 'valueString': thread_key(folder, msg)},
        ],
        'meta': {'tag': tags},
    }
    # Strip null-valued top-level keys (FHIR doesn't allow them)
    return {k: v for k, v in comm.items() if v is not None}


def build_bundle(captured: list, src_file: str) -> dict:
    """Wrap all Communications in a FHIR Bundle of type 'transaction'
    using PUT (upsert by ID) so re-running is idempotent."""
    entries = []
    for rec in captured:
        if not rec.get('ok'):
            continue
        if not rec.get('data') or not rec['data'].get('myHealthMessage'):
            continue
        comm = message_to_communication(rec, src_file)
        entries.append({
            'resource': comm,
            'request': {'method': 'PUT', 'url': f'Communication/{comm["id"]}'},
        })
    return {'resourceType': 'Bundle', 'type': 'transaction', 'entry': entries}


def hapi_request(method: str, path: str, body=None, content_type='application/fhir+json'):
    url = f'{HAPI_BASE}{path}'
    data = None
    headers = {'Accept': 'application/fhir+json'}
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode('utf-8')
        else:
            data = body if isinstance(body, bytes) else body.encode('utf-8')
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req) as resp:
        text = resp.read().decode('utf-8')
        try:
            return resp.getcode(), json.loads(text)
        except json.JSONDecodeError:
            return resp.getcode(), text


def existing_stanford_msg_ids() -> list:
    """Return the FHIR IDs of every Communication whose identifier
    system is urn:stanford:myhealth:message. Walks paginated search.

    The `|` in `system|` must be URL-encoded as `%7C` — HAPI returns
    400 on the bare character. Without the trailing `|`, the query
    matches by identifier VALUE instead of SYSTEM (returns 0 results).
    """
    ids = []
    sys_param = urllib.parse.quote(f'{IDENT_SYSTEM}|', safe=':')
    next_url = (f'/Communication?identifier={sys_param}&'
                f'_elements=id,identifier&_count=200')
    while next_url:
        status, data = hapi_request('GET', next_url)
        if not isinstance(data, dict):
            break
        for e in data.get('entry', []) or []:
            r = e.get('resource') or {}
            if r.get('id'):
                ids.append(r['id'])
        # Find next link
        next_url = None
        for link in data.get('link', []) or []:
            if link.get('relation') == 'next' and link.get('url'):
                # Strip the base URL prefix to get a relative path
                u = link['url']
                if u.startswith(HAPI_BASE):
                    next_url = u[len(HAPI_BASE):]
                break
    return ids


def wipe_stanford_messages():
    """Delete every existing Communication with our identifier system."""
    print('Querying existing stanford-msg Communications…')
    ids = existing_stanford_msg_ids()
    print(f'  found {len(ids)} to delete')
    if not ids:
        return 0
    # Batch via transaction Bundle of DELETE entries
    BATCH = 100
    total_deleted = 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        bundle = {
            'resourceType': 'Bundle',
            'type': 'transaction',
            'entry': [
                {'request': {'method': 'DELETE', 'url': f'Communication/{rid}'}}
                for rid in chunk
            ],
        }
        status, data = hapi_request('POST', '/', bundle)
        if status >= 400:
            print(f'  DELETE batch {i}: HTTP {status}')
            print(f'    {json.dumps(data)[:500]}')
            break
        ok = sum(1 for e in (data.get('entry') or [])
                 if str(e.get('response', {}).get('status', '')).startswith(('200', '204')))
        total_deleted += ok
        print(f'  deleted batch {i // BATCH + 1}/{(len(ids) + BATCH - 1) // BATCH}  ({ok}/{len(chunk)} ok, running total {total_deleted})')
    return total_deleted


def main():
    global HAPI_BASE, PORTAL, IDENT_SYSTEM, SRC_PORTAL_TAG, ID_PREFIX_MSG
    default_base = HAPI_BASE
    ap = argparse.ArgumentParser()
    ap.add_argument('batch', help='Path to <portal>-message-batch-*.json')
    ap.add_argument('--portal', default='stanford',
                    help='Portal id from mobile/assets/portals/*.json (default: stanford)')
    ap.add_argument('--wipe', action='store_true',
                    help='Delete all existing Communications with this portal\'s system before POSTing')
    ap.add_argument('--dry-run', action='store_true',
                    help='Build the bundle but skip wipe + POST')
    ap.add_argument('--base-url', default=default_base,
                    help=f'HAPI base URL (default: {default_base})')
    args = ap.parse_args()
    HAPI_BASE = args.base_url.rstrip('/')

    PORTAL = get_portal(args.portal)
    IDENT_SYSTEM = PORTAL.identifier_system('message')
    SRC_PORTAL_TAG = PORTAL.src_portal_tag
    ID_PREFIX_MSG = f'comm-{PORTAL.id}'
    print(f'Portal: {PORTAL.name} ({PORTAL.id})')

    batch_path = Path(args.batch)
    if not batch_path.exists():
        print(f'ERROR: {batch_path} not found', file=sys.stderr)
        sys.exit(1)

    print(f'Loading: {batch_path}')
    with open(batch_path) as f:
        batch = json.load(f)
    print(f'  captured={batch.get("capturedCount", 0)}  errors={batch.get("errorCount", 0)}')

    src_file = batch_path.name
    bundle = build_bundle(batch.get('captured', []), src_file)
    print(f'\nBundle: {len(bundle["entry"])} Communication entries')

    # Folder + thread-key distribution
    folder_dist = Counter()
    thread_keys = set()
    for rec in batch.get('captured', []):
        if rec.get('ok'):
            folder_dist[rec['folder']] += 1
            msg = (rec.get('data') or {}).get('myHealthMessage') or {}
            thread_keys.add(thread_key(rec['folder'], msg))
    print(f'  folders: {dict(folder_dist)}')
    print(f'  distinct thread keys: {len(thread_keys)}')

    if args.dry_run:
        out = batch_path.with_suffix('.bundle.json')
        with open(out, 'w') as f:
            json.dump(bundle, f)
        print(f'\nDry run — wrote {out} ({out.stat().st_size / 1024:.0f} KB)')
        return

    if args.wipe:
        wiped = wipe_stanford_messages()
        print(f'\nWiped {wiped} existing Communications.')

    print('\nPOSTing bundle…')
    status, resp = hapi_request('POST', '/', bundle)
    print(f'  status: {status}')
    if isinstance(resp, dict):
        by = Counter()
        for e in resp.get('entry') or []:
            by[e.get('response', {}).get('status', '?')] += 1
        for k, v in by.most_common():
            print(f'  {v:>4d}  {k}')

    # Verify total count
    _, count_resp = hapi_request('GET', '/Communication?_summary=count')
    if isinstance(count_resp, dict):
        print(f'\nTotal Communications now in HAPI: {count_resp.get("total")}')


if __name__ == '__main__':
    main()
