#!/usr/bin/env python3
"""Convert Mayo Clinic MyChart test results (from GetDetails API) to FHIR R4 resources.

Produces:
  - DiagnosticReport per order (panel-level)
  - Observation per result component (individual lab values)

Only converts results that have actual detail data (components or narrative).
Empty detail results (from linked institutions) are skipped as they're covered
by other data sources (Stanford scrapes, Apple Health clinical records).
"""

import json
import re
import hashlib
from datetime import datetime
from fhir_utils import make_id, make_narrative, make_meta_tag, strip_html, sanitize_for_xhtml, make_provenance_meta

SCRIPT_NAME = 'convert_mayo_results_to_fhir.py'
SOURCE_FILE = 'mayo_test_results_detail.json'
SOURCE_CODE = 'mayo-mychart-results'
SOURCE_DISPLAY = 'Mayo Clinic Test Results Scrape (via Sutter)'
# Legacy SOURCE_TAG kept for backward-compatible use outside observations
SOURCE_TAG = make_meta_tag(SOURCE_CODE, SOURCE_DISPLAY)


def parse_iso_date(iso_str):
    """Parse ISO date string like '2026-02-09T11:55:00-05:00' to FHIR dateTime."""
    if not iso_str:
        return None
    if re.match(r'\d{4}-\d{2}-\d{2}T', iso_str):
        return iso_str
    return None


def parse_display_date(date_str):
    """Parse display date like 'Feb 09, 2026 12:10 PM'."""
    if not date_str:
        return None
    for fmt in ['%b %d, %Y %I:%M %p', '%b %d, %Y', '%B %d, %Y %I:%M %p', '%B %d, %Y',
                '%m/%d/%Y %I:%M %p', '%m/%d/%Y']:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            continue
    return None


def parse_abnormal_flag(flag_str):
    """Map Mayo Clinic abnormal flag to FHIR interpretation code."""
    mapping = {
        'Low': ('L', 'Low'),
        'High': ('H', 'High'),
        'Abnormal': ('A', 'Abnormal'),
        'Critical Low': ('LL', 'Critical low'),
        'Critical High': ('HH', 'Critical high'),
        'Normal': ('N', 'Normal'),
    }
    if flag_str and flag_str != 'Unknown':
        code, display = mapping.get(flag_str, ('A', flag_str))
        return {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation',
                'code': code,
                'display': display
            }],
            'text': flag_str
        }
    return None


def parse_reference_range(ref_range):
    """Parse reference range to FHIR format."""
    if not ref_range:
        return None
    low = ref_range.get('displayLow', '')
    high = ref_range.get('displayHigh', '')
    formatted = ref_range.get('formattedReferenceRange', '')

    if not low and not high and not formatted:
        return None

    fhir_range = {}
    if low:
        try:
            fhir_range['low'] = {'value': float(re.sub(r'[<>= ]', '', low))}
        except ValueError:
            pass
    if high:
        try:
            fhir_range['high'] = {'value': float(re.sub(r'[<>= ]', '', high))}
        except ValueError:
            pass
    if formatted:
        fhir_range['text'] = formatted

    return fhir_range if fhir_range else None


_obs_seq = 0

def build_observation(component, order_name, effective_dt, provider, result_id_base):
    """Build a FHIR Observation from a result component."""
    global _obs_seq
    _obs_seq += 1
    comp_info = component.get('componentInfo', {})
    comp_result = component.get('componentResultInfo', {})

    name = comp_info.get('name', '') or comp_info.get('commonName', '')
    units = comp_info.get('units', '')
    value = comp_result.get('value', '')
    numeric_value = comp_result.get('numericValue')
    abnormal_flag = comp_result.get('abnormalFlagCategoryValue', '')

    if not name:
        return None

    # For HTML component values, strip to plain text for the value but keep it rich
    is_html_value = bool(value and '<' in value and '>' in value)
    plain_value = strip_html(value) if is_html_value else value

    comp_id = comp_info.get('componentID', '')
    obs_id = make_id('mayo-obs', comp_id or result_id_base, name, effective_dt or '', str(_obs_seq))

    observation = {
        'resourceType': 'Observation',
        'id': obs_id,
        'status': 'final',
        'meta': make_provenance_meta(
            source_file=SOURCE_FILE,
            source_tag_code=SOURCE_CODE,
            source_tag_display=SOURCE_DISPLAY,
            raw_name=name,
            order_name=order_name,
            convert_script=SCRIPT_NAME,
        ),
        'category': [{
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                'code': 'laboratory',
                'display': 'Laboratory'
            }]
        }],
        'code': {
            'text': name
        },
    }

    if effective_dt:
        observation['effectiveDateTime'] = effective_dt

    # Set value
    if numeric_value is not None and units:
        observation['valueQuantity'] = {
            'value': numeric_value,
            'unit': units,
            'system': 'http://unitsofmeasure.org'
        }
    elif plain_value:
        observation['valueString'] = plain_value.strip()[:5000]

    # Reference range
    ref = parse_reference_range(comp_result.get('referenceRange'))
    if ref:
        observation['referenceRange'] = [ref]

    # Abnormal flag
    interp = parse_abnormal_flag(abnormal_flag)
    if interp:
        observation['interpretation'] = [interp]

    # Provider
    if provider:
        observation['performer'] = [{'display': provider}]

    # Identifier for dedup
    observation['identifier'] = [{
        'system': 'urn:mayo:mychart:component',
        'value': comp_info.get('componentID', obs_id)
    }]

    # Narrative
    display_value = plain_value if is_html_value else value
    narrative_parts = [f'{name}: {display_value[:200]} {units}'.strip()]
    if ref and ref.get('text'):
        narrative_parts.append(f'Reference: {ref["text"]}')
    if abnormal_flag and abnormal_flag != 'Unknown':
        narrative_parts.append(f'Flag: {abnormal_flag}')
    observation['text'] = make_narrative(' | '.join(narrative_parts), title=f'{order_name} - {name}')

    return observation


def build_diagnostic_report(order_data, observation_ids):
    """Build a FHIR DiagnosticReport from an order."""
    order_name = order_data.get('orderName', 'Unknown Test')
    results = order_data.get('results', [{}])
    result = results[0] if results else {}
    meta = result.get('orderMetadata', {})

    # Dates
    effective_dt = parse_iso_date(meta.get('prioritizedInstantISO')) or \
                   parse_iso_date(meta.get('latestUpdateInstantISO')) or \
                   parse_display_date(meta.get('resultTimestampDisplay'))

    issued_dt = parse_iso_date(meta.get('latestUpdateInstantISO')) or \
                parse_display_date(meta.get('resultTimestampDisplay'))

    provider = meta.get('authorizingProviderName') or meta.get('orderProviderName') or \
               meta.get('readingProviderName', '')
    status_str = (meta.get('resultStatus') or 'final').lower()
    fhir_status = 'final' if status_str in ('final', 'completed') else \
                  'preliminary' if status_str == 'preliminary' else \
                  'corrected' if status_str in ('corrected', 'amended') else 'final'

    order_key = order_data.get('key', '')
    report_id = make_id('mayo-dr', order_key or order_name, effective_dt or '')

    # Set category based on resultType
    result_type = meta.get('resultType', 'LAB').upper()
    if result_type == 'IMAGING':
        category_code, category_display = 'RAD', 'Radiology'
    elif result_type == 'GENOMIC':
        category_code, category_display = 'GE', 'Genetics'
    else:
        category_code, category_display = 'LAB', 'Laboratory'

    report = {
        'resourceType': 'DiagnosticReport',
        'id': report_id,
        'status': fhir_status,
        'meta': {'tag': [SOURCE_TAG]},
        'category': [{
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/v2-0074',
                'code': category_code,
                'display': category_display
            }]
        }],
        'code': {'text': order_name},
    }

    if effective_dt:
        report['effectiveDateTime'] = effective_dt
    if issued_dt:
        report['issued'] = issued_dt

    if provider:
        report['performer'] = [{'display': provider}]

    # Link to observations
    if observation_ids:
        report['result'] = [{'reference': f'Observation/{oid}'} for oid in observation_ids]

    # Identifier
    if order_key:
        report['identifier'] = [{
            'system': 'urn:mayo:mychart:order',
            'value': order_key
        }]

    # Lab info
    lab = meta.get('resultingLab', {})
    if lab.get('name'):
        report['performer'] = report.get('performer', [])
        report['performer'].append({'display': lab['name']})

    # Specimen
    specimen_display = meta.get('specimensDisplay', '')
    if specimen_display:
        report['specimen'] = [{'display': specimen_display}]

    # Associated diagnoses
    diagnoses = meta.get('associatedDiagnoses', [])

    # Study results (narrative/impression for pathology, imaging, etc.)
    study = result.get('studyResult', {})
    narrative_text = study.get('narrative', {}).get('contentAsString', '') or \
                     study.get('narrative', {}).get('contentAsHtml', '')
    impression_text = study.get('impression', {}).get('contentAsString', '') or \
                      study.get('impression', {}).get('contentAsHtml', '')

    # Addenda
    addenda_texts = []
    for addendum in study.get('addenda', []):
        add_text = addendum.get('contentAsString', '') or addendum.get('contentAsHtml', '')
        if add_text:
            addenda_texts.append(strip_html(add_text))

    # Build narrative
    parts = [f'Test: {order_name}']
    if effective_dt:
        parts.append(f'Date: {effective_dt}')
    if provider:
        parts.append(f'Provider: {provider}')
    if specimen_display:
        parts.append(f'Specimen: {specimen_display}')
    if diagnoses:
        parts.append(f'Diagnoses: {"; ".join(diagnoses)}')
    if narrative_text:
        parts.append(f'Narrative: {strip_html(narrative_text)[:2000]}')
    if impression_text:
        parts.append(f'Impression: {strip_html(impression_text)[:1000]}')
    for i, add in enumerate(addenda_texts):
        parts.append(f'Addendum {i+1}: {add[:500]}')

    report['text'] = make_narrative('\n'.join(parts), title=order_name)

    # Add conclusion from narrative/impression
    conclusion_text = strip_html(narrative_text or impression_text)
    if conclusion_text:
        report['conclusion'] = conclusion_text[:5000]

    # Result note
    result_note = result.get('resultNote', {})
    if result_note.get('hasContent'):
        note_text = strip_html(result_note.get('contentAsString', '') or result_note.get('contentAsHtml', ''))
        if note_text and not report.get('conclusion'):
            report['conclusion'] = note_text[:5000]

    return report, report_id


def main():
    with open('mayo_test_results_detail.json') as f:
        all_entries = json.load(f)

    # Filter to entries with actual data
    entries_with_data = []
    entries_empty = 0
    for entry in all_entries:
        detail = entry.get('detail', {})
        results = detail.get('results', [{}])
        result = results[0] if results else {}
        components = result.get('resultComponents', [])
        has_narrative = result.get('studyResult', {}).get('narrative', {}).get('hasContent', False)
        has_impression = result.get('studyResult', {}).get('impression', {}).get('hasContent', False)
        has_addenda = any(a.get('hasContent') for a in result.get('studyResult', {}).get('addenda', []))

        if components or has_narrative or has_impression or has_addenda:
            entries_with_data.append(entry)
        else:
            entries_empty += 1

    print(f'Input: {len(all_entries)} entries ({len(entries_with_data)} with data, {entries_empty} empty/linked)')

    bundle = {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': []
    }

    obs_count = 0
    dr_count = 0

    for entry in entries_with_data:
        detail = entry.get('detail', {})
        order_data = detail  # detail IS the order data (same shape as Stanford)
        results = order_data.get('results', [])
        if not results:
            continue

        result = results[0]
        meta = result.get('orderMetadata', {})
        components = result.get('resultComponents', [])

        effective_dt = parse_iso_date(meta.get('prioritizedInstantISO')) or \
                       parse_iso_date(meta.get('latestUpdateInstantISO')) or \
                       parse_display_date(meta.get('resultTimestampDisplay'))

        provider = meta.get('authorizingProviderName') or meta.get('orderProviderName', '')

        # Build observations for each component
        observation_ids = []
        order_key = order_data.get('key', '') or entry.get('orderKey', '')
        result_id_base = order_key or f'{order_data.get("orderName")}|{effective_dt}'

        for comp in components:
            obs = build_observation(comp, order_data.get('orderName', ''), effective_dt, provider, result_id_base)
            if obs:
                observation_ids.append(obs['id'])
                bundle['entry'].append({
                    'resource': obs,
                    'request': {'method': 'PUT', 'url': f'Observation/{obs["id"]}'}
                })
                obs_count += 1

        # Build diagnostic report linking to observations
        report, report_id = build_diagnostic_report(order_data, observation_ids)
        bundle['entry'].append({
            'resource': report,
            'request': {'method': 'PUT', 'url': f'DiagnosticReport/{report_id}'}
        })
        dr_count += 1

    # Split into batches
    BATCH_SIZE = 500
    entries = bundle['entry']
    batches = [entries[i:i+BATCH_SIZE] for i in range(0, len(entries), BATCH_SIZE)]

    for i, batch_entries in enumerate(batches):
        batch_bundle = {
            'resourceType': 'Bundle',
            'type': 'transaction',
            'entry': batch_entries
        }
        filename = f'mayo_results_fhir_batch_{i+1}.json'
        with open(filename, 'w') as f:
            json.dump(batch_bundle, f, indent=2)
        print(f'  Batch {i+1}: {len(batch_entries)} resources -> {filename}')

    print(f'\nConverted Mayo Clinic test results:')
    print(f'  {dr_count} DiagnosticReport resources')
    print(f'  {obs_count} Observation resources')
    print(f'  {dr_count + obs_count} total resources in {len(batches)} batch(es)')

    # Show sample
    sample_obs = [e for e in entries if e['resource']['resourceType'] == 'Observation']
    if sample_obs:
        s = sample_obs[0]['resource']
        print(f'\nSample Observation:')
        print(f'  Code: {s["code"].get("text")}')
        if s.get('valueQuantity'):
            print(f'  Value: {s["valueQuantity"]["value"]} {s["valueQuantity"].get("unit","")}')
        elif s.get('valueString'):
            print(f'  Value: {s["valueString"][:80]}')
        print(f'  Date: {s.get("effectiveDateTime")}')


if __name__ == '__main__':
    main()
