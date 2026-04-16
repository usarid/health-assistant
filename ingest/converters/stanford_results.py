#!/usr/bin/env python3
"""Convert Stanford MyHealth test results (from GetDetails API) to FHIR R4 resources.

Produces:
  - DiagnosticReport per order (panel-level)
  - Observation per result component (individual lab values)
"""

import json
import re
import hashlib
from datetime import datetime
from fhir_utils import (make_id, make_narrative, make_meta_tag, strip_html,
                        sanitize_for_xhtml, make_provenance_meta)

SCRIPT_NAME = 'convert_stanford_results_to_fhir.py'
SOURCE_FILE = 'stanford_test_results_raw.json'
SOURCE_CODE = 'stanford-myhealth-results'
SOURCE_DISPLAY = 'Stanford MyHealth Test Results Scrape'
# Legacy SOURCE_TAG kept for backward-compatible use outside observations
SOURCE_TAG = make_meta_tag(SOURCE_CODE, SOURCE_DISPLAY)


def parse_iso_date(iso_str):
    """Parse ISO date string like '2026-03-11T09:50:00-07:00' to FHIR dateTime."""
    if not iso_str:
        return None
    # Return as-is if already ISO format
    if re.match(r'\d{4}-\d{2}-\d{2}T', iso_str):
        return iso_str
    return None


def parse_display_date(date_str):
    """Parse display date like 'Mar 11, 2026 6:02 PM'."""
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
    """Map Stanford abnormal flag to FHIR interpretation code."""
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
    # Try to parse numeric low/high
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

    # Use componentID + sequence for guaranteed uniqueness
    comp_id = comp_info.get('componentID', '')
    obs_id = make_id('stanford-obs', comp_id or result_id_base, name, effective_dt or '', str(_obs_seq))

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

    # Set effective date
    if effective_dt:
        observation['effectiveDateTime'] = effective_dt

    # Set value
    if numeric_value is not None and units:
        observation['valueQuantity'] = {
            'value': numeric_value,
            'unit': units,
            'system': 'http://unitsofmeasure.org'
        }
    elif value:
        # Text value (e.g., interpretive comments)
        observation['valueString'] = value.strip()

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
        'system': 'urn:stanford:myhealth:component',
        'value': comp_info.get('componentID', obs_id)
    }]

    # Narrative
    narrative_parts = [f'{name}: {value} {units}'.strip()]
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

    provider = meta.get('authorizingProviderName') or meta.get('orderProviderName', '')
    status_str = (meta.get('resultStatus') or 'final').lower()
    fhir_status = 'final' if status_str in ('final', 'completed') else \
                  'preliminary' if status_str == 'preliminary' else \
                  'corrected' if status_str in ('corrected', 'amended') else 'final'

    order_key = order_data.get('key', '')
    report_id = make_id('stanford-dr', order_key or order_name, effective_dt or '')

    report = {
        'resourceType': 'DiagnosticReport',
        'id': report_id,
        'status': fhir_status,
        'meta': {'tag': [SOURCE_TAG]},
        'category': [{
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/v2-0074',
                'code': 'LAB',
                'display': 'Laboratory'
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
            'system': 'urn:stanford:myhealth:order',
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

    # Study results (narrative/impression for imaging)
    study = result.get('studyResult', {})
    narrative_text = study.get('narrative', {}).get('contentAsString', '')
    impression_text = study.get('impression', {}).get('contentAsString', '')

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
        parts.append(f'Narrative: {strip_html(narrative_text)[:500]}')
    if impression_text:
        parts.append(f'Impression: {strip_html(impression_text)[:500]}')

    report['text'] = make_narrative('\n'.join(parts), title=order_name)

    # Add conclusion from narrative/impression
    if narrative_text or impression_text:
        conclusion = strip_html(narrative_text or impression_text)
        if conclusion:
            report['conclusion'] = conclusion[:2000]

    return report, report_id


def main():
    with open('stanford_test_results_raw.json') as f:
        test_results = json.load(f)

    bundle = {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': []
    }

    obs_count = 0
    dr_count = 0
    skipped = 0

    for order_data in test_results:
        results = order_data.get('results', [])
        if not results:
            skipped += 1
            continue

        result = results[0]
        meta = result.get('orderMetadata', {})
        components = result.get('resultComponents', [])
        order_name = order_data.get('orderName', 'Unknown')
        order_key = order_data.get('key', '')

        # Effective date for observations
        effective_dt = parse_iso_date(meta.get('prioritizedInstantISO')) or \
                       parse_iso_date(meta.get('latestUpdateInstantISO')) or \
                       parse_display_date(meta.get('resultTimestampDisplay'))

        provider = meta.get('authorizingProviderName') or meta.get('orderProviderName', '')

        # Build observations for each component
        observation_ids = []
        result_id_base = order_key or f'{order_name}|{effective_dt}'

        for comp in components:
            obs = build_observation(comp, order_name, effective_dt, provider, result_id_base)
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

    # Split into manageable batches (HAPI can struggle with huge bundles)
    BATCH_SIZE = 500
    entries = bundle['entry']
    batches = [entries[i:i+BATCH_SIZE] for i in range(0, len(entries), BATCH_SIZE)]

    for i, batch_entries in enumerate(batches):
        batch_bundle = {
            'resourceType': 'Bundle',
            'type': 'transaction',
            'entry': batch_entries
        }
        filename = f'stanford_results_fhir_batch_{i+1}.json'
        with open(filename, 'w') as f:
            json.dump(batch_bundle, f, indent=2)
        print(f'  Batch {i+1}: {len(batch_entries)} resources -> {filename}')

    print(f'\nConverted {len(test_results)} Stanford test results:')
    print(f'  {dr_count} DiagnosticReport resources')
    print(f'  {obs_count} Observation resources')
    print(f'  {dr_count + obs_count} total resources in {len(batches)} batch(es)')
    if skipped:
        print(f'  {skipped} orders skipped (no results)')

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
