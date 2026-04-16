"""Shared utilities for converting scraped MyChart data to FHIR R4 resources.

All conversion scripts should import from here to ensure consistent
HTML cleaning, XHTML sanitization, ID generation, and narrative building.
"""

import re
import hashlib
import base64
from datetime import datetime


# ── HTML / Text Cleaning ─────────────────────────────────────────────

def strip_html(html):
    """Strip HTML tags, styles, scripts, and entities. Returns plain text.

    Handles Epic MyChart report HTML which includes:
    - <style> blocks with CSS class definitions
    - <script> blocks with report-loading JavaScript
    - Rich text formatting spans/divs
    - HTML entities (&nbsp;, &amp;, etc.)
    """
    if not html:
        return ''
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'&\w+;', '', text)  # catch any remaining entities
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def sanitize_for_xhtml(text):
    """Escape text for safe embedding in FHIR XHTML narrative div."""
    if not text:
        return ''
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    text = text.replace('"', '&quot;')
    return text


# ── FHIR Narrative Builder ───────────────────────────────────────────

def make_narrative(plain_text, title=''):
    """Build a FHIR text element with XHTML div for OpenSearch full-text indexing.

    Args:
        plain_text: The full text content (already stripped of HTML)
        title: Optional bold title line at the top of the narrative

    Returns:
        dict with 'status' and 'div' keys suitable for resource['text']
    """
    escaped = sanitize_for_xhtml(plain_text)
    title_html = f'<p><b>{sanitize_for_xhtml(title)}</b></p>' if title else ''
    return {
        'status': 'generated',
        'div': f'<div xmlns="http://www.w3.org/1999/xhtml">{title_html}<p>{escaped}</p></div>'
    }


# ── ID Generation ────────────────────────────────────────────────────

def make_id(prefix, *parts):
    """Generate a deterministic FHIR resource ID from parts.

    Uses MD5 hash of joined parts to ensure:
    - Same input always produces same ID (idempotent loads)
    - Different inputs produce different IDs

    Args:
        prefix: Resource type prefix (e.g., 'ucsf-enc', 'mskcc-msg')
        *parts: Values to hash (e.g., CSN, date, thread ID)
    """
    raw = '|'.join(str(p) for p in parts)
    return prefix + '-' + hashlib.md5(raw.encode()).hexdigest()[:12]


# ── Date Parsing ─────────────────────────────────────────────────────

def parse_date(date_str):
    """Parse various date formats to FHIR date (YYYY-MM-DD).

    Handles formats from Epic MyChart:
    - "February 19 2026"
    - "Feb 19, 2026"
    - "2/19/2026"
    """
    if not date_str:
        return None
    for fmt in ['%B %d %Y', '%b %d, %Y', '%b %d %Y', '%m/%d/%Y', '%Y-%m-%d']:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


# ── Base64 Encoding ──────────────────────────────────────────────────

def text_to_base64(text):
    """Encode plain text to base64 for FHIR attachment data."""
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


# ── FHIR Resource Builders ───────────────────────────────────────────

def make_meta_tag(code, display, system='http://example.org/source'):
    """Build a FHIR meta.tag entry for source tracking."""
    return {'system': system, 'code': code, 'display': display}


# ── Provenance Helpers ──────────────────────────────────────────────

# Tag systems — use a proper URI namespace so they're queryable via HAPI's
# _tag search parameter:  GET /Observation?_tag=urn:phv:tag|raw-name:WBC
PHV_TAG_SYSTEM = 'urn:phv:tag'

# Pipeline version — bump this when the conversion logic changes materially.
# Lets you find all resources produced by a given pipeline version and
# re-process them if needed.
PIPELINE_VERSION = '2'


def make_provenance_meta(source_file, source_tag_code, source_tag_display,
                         raw_name=None, order_name=None,
                         convert_script=None):
    """Build a complete FHIR meta block with provenance fields.

    Args:
        source_file:        Filename of the raw input (e.g., 'stanford_test_results_raw.json')
        source_tag_code:    Institution source code (e.g., 'stanford-myhealth-results')
        source_tag_display: Human-readable source label
        raw_name:           Original test name before any mapping (e.g., 'WBC', 'M-Protein (monoclonal)')
        order_name:         Panel/order context (e.g., 'CBC with Differential')
        convert_script:     Name of the conversion script (e.g., 'convert_stanford_results_to_fhir.py')

    Returns:
        dict suitable for resource['meta']

    The fields are:
      meta.source   — URI of the source file (HAPI indexes this; queryable via _source=)
      meta.tag[]    — structured tags:
                        - source institution (existing pattern)
                        - phv:raw-name:<name>   — original test name
                        - phv:order:<name>      — panel/order context
                        - phv:pipeline:v<N>     — pipeline version
                        - phv:script:<name>     — conversion script
    """
    meta = {
        'source': f'file:{source_file}',
        'tag': [
            make_meta_tag(source_tag_code, source_tag_display),
            make_meta_tag(f'pipeline:v{PIPELINE_VERSION}',
                          f'PHV Pipeline v{PIPELINE_VERSION}',
                          system=PHV_TAG_SYSTEM),
        ],
    }

    if convert_script:
        meta['tag'].append(
            make_meta_tag(f'script:{convert_script}',
                          convert_script,
                          system=PHV_TAG_SYSTEM))

    if raw_name:
        meta['tag'].append(
            make_meta_tag(f'raw-name:{raw_name}',
                          raw_name,
                          system=PHV_TAG_SYSTEM))

    if order_name:
        meta['tag'].append(
            make_meta_tag(f'order:{order_name}',
                          order_name,
                          system=PHV_TAG_SYSTEM))

    return meta


def add_provenance_tag(resource, tag_code, tag_display, system=PHV_TAG_SYSTEM):
    """Add a provenance tag to an existing resource's meta.tag list.

    Safe to call multiple times — checks for duplicates by (system, code).
    Used by post-processing scripts (loinc_mapper, quality patches) to
    layer additional provenance onto resources that already have meta.
    """
    if 'meta' not in resource:
        resource['meta'] = {}
    if 'tag' not in resource['meta']:
        resource['meta']['tag'] = []

    # Don't add duplicates
    existing = {(t.get('system'), t.get('code')) for t in resource['meta']['tag']}
    if (system, tag_code) not in existing:
        resource['meta']['tag'].append(
            make_meta_tag(tag_code, tag_display, system=system))


def make_encounter_class(visit_type):
    """Map a visit type string to FHIR encounter class coding."""
    vt = (visit_type or '').lower()
    system = 'http://terminology.hl7.org/CodeSystem/v3-ActCode'
    if 'emergency' in vt:
        return {'system': system, 'code': 'EMER', 'display': 'emergency'}
    if 'hospital' in vt or 'admission' in vt or 'surgery' in vt:
        return {'system': system, 'code': 'IMP', 'display': 'inpatient encounter'}
    if 'telephone' in vt or 'video' in vt or 'message' in vt or 'telemedicine' in vt or 'e-consult' in vt:
        return {'system': system, 'code': 'VR', 'display': 'virtual'}
    return {'system': system, 'code': 'AMB', 'display': 'ambulatory'}
