#!/usr/bin/env python3
"""v2 messages converter — rebuilds FHIR Communication resources from raw scrapes.

Addresses CONCLUSIONS_LOG.md:
  C-002: preserve organizationId as a structured tag
  C-006: extract Stanford eMid from body as a per-message WP-24 identifier
  C-007: preserves the per-thread (MSKCC) vs per-message (Stanford) grain of v1
         so the v1-vs-v2 diff is meaningful (different grain would obscure all other diffs)
  C-008: applies the organizationId → institution mapping with evidence-driven rules
  C-009: uses platform sender labels + user names from `users` dict as primary signal
         for institutional attribution; never falls back on body-keyword classification

Outputs a FHIR transaction Bundle that can be POSTed to the v2 HAPI on port 8090.
Loading is a separate step — review this script's output before loading.

Run from any directory:
    python3 tools/v2/convert_messages.py
"""

import json
import re
import hashlib
import sys
from pathlib import Path
from collections import Counter, defaultdict

# Make lib/ importable
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'lib'))
from fhir_utils import strip_html  # noqa: E402


# ── Constants ──────────────────────────────────────────────────────────
RAW_DIR = Path('/Users/urisarid/usarid@gmail.com/Medical/Synthesis/health-assistant/data/raw-exports')
OUT_DIR = Path(__file__).resolve().parent / 'out'

CONVERTER_VERSION = 'v2.0.0'
SCRAPER_VERSION_MSKCC = 'mskcc-messages-v8'      # from scrape_messages.js header
SCRAPER_VERSION_STAN = 'stanford-messages-2026-04'  # from raw file mtime

# Provenance namespaces (per CONCLUSIONS_LOG provenance contract)
NS_SRC_PORTAL    = 'urn:bina:source-portal'
NS_SRC_ORG       = 'urn:bina:source-org'
NS_SRC_ORG_ID    = 'urn:bina:source-org-id'
NS_SCRAPER_VER   = 'urn:bina:scraper-version'
NS_CONVERTER_VER = 'urn:bina:converter-version'

# Identifier namespaces — two-key discipline
NS_PORTAL_THREAD_MSKCC = 'urn:bina:portal:mskcc:thread'        # portal-local
NS_PORTAL_MSG_STANFORD = 'urn:bina:portal:stanford:message'    # portal-local
NS_EPIC_THREAD         = 'urn:bina:epic:thread'                # canonical (cross-portal)
NS_EPIC_MESSAGE        = 'urn:bina:epic:message'               # canonical (cross-portal)

# Regex for extracting Stanford's per-message WP-24 token (eMid) from body
EMID_RE = re.compile(r'eMid=(WP-24[A-Za-z0-9_\-+=/.%]+)')

# Regex for extracting clean subject from MSKCC preview text.
# MSKCC preview shape: "<Subject><SenderName><Date><body snippet>"
# Constraints learned from the v1↔v2 diff:
#   1. Subject and sender often concatenate without a space ("DepartureCindy K"),
#      so we cannot use whitespace as a boundary. The sender's first token
#      starts at a *non-uppercase* lookbehind: either string-start, punctuation,
#      whitespace, or lowercase (the camelCase boundary).
#   2. The sender's *first* token is conservative — Cap+lowercase (like "Cindy")
#      or AllCaps (like "UCSF"). This prevents matching "DepartureCindy" as a
#      single first-token (greedy across the camelCase boundary).
#   3. The sender's *subsequent* tokens (after whitespace) are more permissive:
#      they can be a single capital ("K" as an initial), mixed-case ("MyChart"),
#      or AllCaps. This lets "Cindy K", "Marilyn B", and "UCSF MyChart Messaging
#      User" all match as senders.
#   4. The sender pattern requires at least 2 tokens — single capitalized words
#      at the boundary aren't senders. ("You" is special-cased separately.)
#   5. The sender pattern is immediately followed (lookahead) by a date — no
#      space consumed. This disambiguates "Insurance" (subject word followed
#      by a sender) from "Cindy K" (sender followed by date).
# Both first and subsequent token patterns exclude the literal standalone "You"
# (i.e., "You" not part of a longer name like "Yours") via negative lookahead.
# Without this, "Insurance You02/28" would match as Cap+lower("Insurance") +
# Cap+lower("You") + date — eating the subject. By blocking standalone-"You" from
# the token alternative, "You" can only match as the dedicated "You" branch,
# which preserves "Insurance" as the subject.
#
# Note: we use `(?!You[^a-zA-Z])` rather than `(?!You\b)` because `\b` does not
# fire between letter and digit (both are word chars in Python's regex), so
# `You\b` does NOT match in "You02" — the lookahead would pass spuriously.
_NOT_STANDALONE_YOU = r'(?!You[^a-zA-Z])'
_FIRST_TOKEN = r'(?:' + _NOT_STANDALONE_YOU + r'(?:[A-Z][a-z]+|[A-Z]+))'
# Conservative subsequent: Cap+lower (Cindy), AllCaps (UCSF), or single-cap initial (K, K.)
# Deliberately NOT permitting mixed-case run-on like [A-Z][a-zA-Z]*  — that would let
# "LosartanYou" match as one sender token and swallow the subject's last word.
# Trade-off: multi-word brand names like "MyChart" won't match as a single sender
# token; we accept that on the long tail.
_SUBSEQUENT_TOKEN = r'(?:' + _NOT_STANDALONE_YOU + r'(?:[A-Z][a-z]+|[A-Z]+|[A-Z]\.?))'

_SUBJECT_BREAK = re.compile(
    r'(?:'
    r'You'                                                              # patient label
    r'|'
    r'(?<![A-Z])' + _FIRST_TOKEN + r'(?:\s+' + _SUBSEQUENT_TOKEN + r')+'  # 2+ tokens, camelCase-safe
    r')'
    r'(?=(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|\d{1,2}/\d{1,2})'
)


def det_id(prefix, *parts):
    """Deterministic short ID from any string parts. Stable across runs."""
    raw = '|'.join(str(p) for p in parts if p)
    h = hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{h}'


def extract_emid(body):
    """Extract WP-24 eMid token from Stanford message body (C-006)."""
    if not body:
        return None
    m = EMID_RE.search(body)
    return m.group(1) if m else None


def extract_subject(preview):
    """Extract clean topic from MSKCC preview blob.

    v1 stored the raw preview as topic. v2 cleans it to just the subject.
    This is one of the deliberate v2 improvements — the diff will show it.
    """
    if not preview:
        return ''
    m = _SUBJECT_BREAK.search(preview)
    return (preview[:m.start()] if m else preview[:80]).rstrip()


def resolve_author_name(author, users, patient_label='Patient'):
    """Resolve a message author to a display name.

    Two-field discrimination (anchored on data structure per C-009):
      - `author.wprKey` present → this is the patient. Patient is not in
        the users dict at all; we use `patient_label` as the rendered name.
      - `author.empKey` present → this is a provider. Look up empKey in
        users dict (or `ser_<empKey>` — Epic indexes each provider twice).

    `patient_label` is parameterizable for multi-tenant. The runtime resolves
    it from patient_config (eventually); current default is generic "Patient".
    """
    if not author:
        return 'Unknown'
    name = (author.get('displayName') or '').strip()
    if name:
        return name
    if author.get('wprKey'):
        return patient_label
    emp_key = (author.get('empKey') or '').strip()
    if emp_key:
        u = users.get(emp_key) or users.get(f'ser_{emp_key}')
        if u:
            return (u.get('name') or '').strip() or 'Unknown'
    return 'Unknown'


# ── C-008/C-009: organizationId → institution mapping ──────────────────
# We don't hardcode the WP-24 organizationId strings — they're inspected at
# runtime from the raw data, then assigned an institution code based on
# evidence-driven rules loaded from patient_config/org_mapping.json.
#
# This separation keeps patient-specific clinician names out of the code
# (the public commit). The .example.json template documents the rule schema.
# Per CONCLUSIONS_LOG.md C-009: signal priority is
# platform_sender_label > user_name > body_phrase. Rules are evaluated in
# array order; first match wins. Unmatched orgs map to the configured default
# (typically "UNKNOWN") and should trigger manual triage + a new rule.

PATIENT_CONFIG_DIR = Path(__file__).resolve().parent / 'patient_config'


def load_org_mapping_config():
    """Load the patient-specific config; fall back to the .example template."""
    private = PATIENT_CONFIG_DIR / 'org_mapping.json'
    example = PATIENT_CONFIG_DIR / 'org_mapping.example.json'
    path = private if private.exists() else example
    with open(path) as f:
        return json.load(f), path


def _signal_value_present(rule_value, *, author_display, user_names, body_blob):
    """Helper: check if any of the three signal types matches the rule value."""
    return rule_value in author_display or rule_value in user_names


def _rule_matches(rule, *, author_display, user_names, body_blob):
    """Evaluate a single rule against the aggregated signals from one org's threads."""
    for clause in rule.get('any_of', []):
        signal = clause.get('signal')
        value = clause.get('value', '')
        if signal == 'platform_sender_label' and value in author_display:
            return True
        if signal == 'user_name' and value in user_names:
            return True
        if signal == 'body_phrase' and value in body_blob:
            return True
    return False


def assign_institution(threads, config):
    """Apply config rules to one orgId's worth of threads → (code, reason)."""
    user_names = Counter()
    author_display = Counter()
    body_sample = []

    for t in threads:
        for uinfo in (t.get('users') or {}).values():
            n = uinfo.get('name', '') or ''
            if n:
                user_names[n] += 1
        for m in t.get('messages', []):
            an = (m.get('author') or {}).get('displayName', '') or ''
            if an:
                author_display[an] += 1
            body_sample.append((m.get('body') or '')[:400])

    body_blob = ' '.join(body_sample)

    for rule in config.get('rules', []):
        if _rule_matches(rule, author_display=author_display,
                         user_names=user_names, body_blob=body_blob):
            return rule['code'], rule.get('reason', 'matched-rule')

    return config.get('default', 'UNKNOWN'), 'no-rule-matched'


def build_org_map(mskcc_data, config):
    """Build {organizationId: (institution_code, reason)} using the loaded config."""
    by_org = defaultdict(list)
    for t in mskcc_data['threads']:
        by_org[t.get('organizationId', '')].append(t)

    primary_code = config.get('primary_portal_code', 'PRIMARY')

    mapping = {}
    for org_id, threads in by_org.items():
        if not org_id:
            # Empty organizationId = thread is native to the source portal
            mapping[org_id] = (primary_code, 'empty-org-id-means-native')
        else:
            mapping[org_id] = assign_institution(threads, config)
    return mapping


# ── MSKCC thread → Communication ───────────────────────────────────────
def convert_mskcc_thread(t, org_map):
    thread_id = t.get('id') or ''
    org_id = t.get('organizationId') or ''
    institution, _reason = org_map.get(org_id, ('UNKNOWN', 'unmapped'))

    messages = t.get('messages') or []
    users = t.get('users') or {}

    sent_iso = messages[0].get('deliveryInstantISO', '') if messages else ''

    payload = []
    for m in messages:
        body_text = strip_html(m.get('body', '') or '')
        if not body_text:
            continue
        msg_date = m.get('deliveryInstantISO', '')
        sender = resolve_author_name(m.get('author'), users) or 'Unknown'
        payload.append({'contentString': f'[{msg_date}] {sender}: {body_text}'[:10000]})

    topic_text = extract_subject(t.get('preview', ''))

    # Identifiers — portal-local first, canonical second, per-message third
    identifiers = [{'system': NS_PORTAL_THREAD_MSKCC, 'value': thread_id}]
    if thread_id:
        identifiers.append({'system': NS_EPIC_THREAD, 'value': thread_id})
    for m in messages:
        wmg = m.get('wmgId') or ''
        if wmg:
            identifiers.append({'system': NS_EPIC_MESSAGE, 'value': wmg})

    # Tags (provenance contract)
    tags = [
        {'system': NS_SRC_PORTAL,    'code': 'mskcc.mychart'},
        {'system': NS_SRC_ORG,       'code': institution},
        {'system': NS_CONVERTER_VER, 'code': CONVERTER_VERSION},
        {'system': NS_SCRAPER_VER,   'code': SCRAPER_VERSION_MSKCC},
    ]
    if org_id:
        tags.append({'system': NS_SRC_ORG_ID, 'code': org_id})

    rid = det_id('comm-mskcc', thread_id)

    comm = {
        'resourceType': 'Communication',
        'id': rid,
        'status': 'completed',
        'identifier': identifiers,
        'meta': {'tag': tags},
    }
    if topic_text:
        comm['topic'] = {'text': topic_text}
    if sent_iso:
        comm['sent'] = sent_iso
    if payload:
        comm['payload'] = payload

    # Participants from users dict (providers only — patient is implicit).
    # Deduplicate by empId since users dict double-indexes each provider.
    seen_emp_ids = set()
    participants = []
    for u in users.values():
        emp_id = u.get('empId', '')
        n = (u.get('name') or '').strip()
        if n and emp_id not in seen_emp_ids:
            seen_emp_ids.add(emp_id)
            participants.append(n)
    # Add patient if any message is from the patient
    has_patient = any((m.get('author') or {}).get('wprKey') for m in messages)
    if has_patient:
        participants.insert(0, 'Patient')
    if participants:
        comm['note'] = [{'text': f'Participants: {"; ".join(participants)}'}]

    return comm


# ── Stanford message → Communication ───────────────────────────────────
def convert_stanford_message(m):
    msg_id = str(m.get('id', ''))
    raw_body = m.get('body', '') or ''
    emid = extract_emid(raw_body)

    # Remove the eMid-bearing URL fragment so the cleaned body doesn't include it
    body_no_emid = re.sub(r'\S*eMid=\S+', '', raw_body)
    body_text = strip_html(body_no_emid).strip()

    sender = m.get('senderName', '') or ''
    title = m.get('title', '') or ''
    sent_iso = m.get('dateSent', '') or ''

    payload = []
    if body_text:
        payload.append({'contentString': f'[{sent_iso}] {sender}: {body_text}'[:10000]})

    identifiers = [{'system': NS_PORTAL_MSG_STANFORD, 'value': msg_id}]
    if emid:
        identifiers.append({'system': NS_EPIC_MESSAGE, 'value': emid})

    tags = [
        {'system': NS_SRC_PORTAL,    'code': 'stanford.myhealth'},
        {'system': NS_SRC_ORG,       'code': 'Stanford'},
        {'system': NS_CONVERTER_VER, 'code': CONVERTER_VERSION},
        {'system': NS_SCRAPER_VER,   'code': SCRAPER_VERSION_STAN},
    ]

    rid = det_id('comm-stan', emid or msg_id)

    comm = {
        'resourceType': 'Communication',
        'id': rid,
        'status': 'completed',
        'identifier': identifiers,
        'meta': {'tag': tags},
    }
    if title:
        comm['topic'] = {'text': title}
    if sent_iso:
        comm['sent'] = sent_iso
    if sender:
        comm['sender'] = {'display': sender}
    if payload:
        comm['payload'] = payload

    return comm


# ── Main ───────────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(exist_ok=True)

    print(f'Loading {RAW_DIR}/mskcc_messages_full.json')
    with open(RAW_DIR / 'mskcc_messages_full.json') as f:
        mskcc = json.load(f)
    print(f'  threads in raw: {len(mskcc["threads"])}')

    print(f'Loading {RAW_DIR}/stanford_messages_full.json')
    with open(RAW_DIR / 'stanford_messages_full.json') as f:
        stanford = json.load(f)
    print(f'  messages in raw: {len(stanford)}')

    print()
    config, config_path = load_org_mapping_config()
    is_example = config_path.name.endswith('.example.json')
    print(f'Loaded org-mapping config from: {config_path.name}'
          f'{" (EXAMPLE — copy to org_mapping.json and edit for real mapping)" if is_example else ""}')

    print()
    print('=== Building organizationId → institution mapping (C-008) ===')
    org_map = build_org_map(mskcc, config)
    print(f'{"organizationId":50s}  {"threads":>8s}  {"institution":12s}  reason')
    print('-' * 100)
    for org_id, (inst, reason) in sorted(
        org_map.items(),
        key=lambda kv: -sum(1 for t in mskcc['threads'] if t.get('organizationId','') == kv[0])
    ):
        n = sum(1 for t in mskcc['threads'] if t.get('organizationId','') == org_id)
        disp = (org_id[:47] + '…') if len(org_id) > 48 else (org_id or '(empty)')
        print(f'{disp:50s}  {n:>8d}  {inst:12s}  {reason}')

    print()
    print('=== Converting MSKCC threads ===')
    mskcc_comms = [convert_mskcc_thread(t, org_map) for t in mskcc['threads']]
    print(f'  {len(mskcc_comms)} Communication resources')

    print('=== Converting Stanford messages ===')
    stan_comms = [convert_stanford_message(m) for m in stanford]
    with_emid = sum(1 for c in stan_comms if any(i['system'] == NS_EPIC_MESSAGE for i in c['identifier']))
    print(f'  {len(stan_comms)} Communication resources ({with_emid} with canonical eMid)')

    all_comms = mskcc_comms + stan_comms

    # Build transaction bundle
    bundle = {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': [
            {'resource': c, 'request': {'method': 'PUT', 'url': f'Communication/{c["id"]}'}}
            for c in all_comms
        ],
    }

    out_file = OUT_DIR / 'messages_v2_bundle.json'
    with open(out_file, 'w') as f:
        json.dump(bundle, f, indent=2)

    # Per-institution summary
    inst_counts = Counter()
    for c in all_comms:
        for tag in c['meta']['tag']:
            if tag['system'] == NS_SRC_ORG:
                inst_counts[tag['code']] += 1
                break

    print()
    print('=== Output summary ===')
    print(f'Bundle: {out_file}  ({out_file.stat().st_size / 1024:.0f} KB, {len(all_comms)} entries)')
    print('Communications by institution:')
    for inst, n in inst_counts.most_common():
        print(f'  {inst:12s} {n:>4d}')

    print()
    print('Sample resource IDs (first 3 MSKCC, first 3 Stanford):')
    for c in mskcc_comms[:3]:
        print(f'  {c["id"]}  (mskcc)  topic={(c.get("topic") or {}).get("text","")[:50]!r}')
    for c in stan_comms[:3]:
        print(f'  {c["id"]}  (stan)   topic={(c.get("topic") or {}).get("text","")[:50]!r}')


if __name__ == '__main__':
    main()
