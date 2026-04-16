#!/usr/bin/env python3
"""Convert Stanford MyHealth visits (from LoadPast API) to FHIR R4 Encounter resources."""

import json
import re
from datetime import datetime
from fhir_utils import make_id, make_narrative, make_meta_tag, make_encounter_class


def parse_epic_date(date_str):
    """Parse Epic /Date(epoch)/ format to ISO datetime string."""
    if not date_str:
        return None
    m = re.search(r'/Date\((\d+)\)/', date_str)
    if m:
        epoch_ms = int(m.group(1))
        dt = datetime.fromtimestamp(epoch_ms / 1000)
        return dt.strftime('%Y-%m-%dT%H:%M:%S')
    return None


def parse_display_date(date_str, time_str=''):
    """Parse display date like '2/13/2024', 'Friday March 27, 2026', etc. with optional time."""
    if not date_str:
        return None
    # Strip leading day-of-week if present (e.g., "Friday March 27, 2026")
    cleaned = re.sub(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+', '', date_str.strip(), flags=re.IGNORECASE)
    combined = cleaned.strip()
    if time_str:
        combined += ' ' + time_str.strip()
    for fmt in ['%B %d, %Y %I:%M %p', '%B %d, %Y', '%m/%d/%Y %I:%M %p', '%m/%d/%Y',
                '%b %d, %Y %I:%M %p', '%b %d, %Y']:
        try:
            return datetime.strptime(combined, fmt).strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            continue
    return None


def main():
    with open('stanford_visits_raw.json') as f:
        visits = json.load(f)

    bundle = {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': []
    }

    for visit in visits:
        # Extract key fields from the LoadPast API response
        # Csn is an encrypted string token in Stanford's API
        csn_id = visit.get('Csn', '') or visit.get('Id', '')

        visit_type = visit.get('VisitTypeName', '') or visit.get('EncounterType', '')
        provider = visit.get('PrimaryProviderName', '')
        department = visit.get('PrimaryDepartment', {})
        dept_name = department.get('Name', '') if isinstance(department, dict) else str(department)
        specialty = ''
        if isinstance(department, dict) and department.get('Specialty'):
            specialty = department['Specialty'].get('Title', '')

        # Parse dates - prefer Instant (epoch), fall back to display Date/Time
        start_dt = parse_epic_date(visit.get('Instant'))
        if not start_dt:
            start_dt = parse_display_date(visit.get('Date', ''), visit.get('Time', ''))

        if not start_dt:
            continue  # Skip visits without any parseable date

        # Generate deterministic ID
        enc_id = make_id('stanford-enc', csn_id or start_dt, visit_type, provider)

        # Map to FHIR encounter class
        enc_class = make_encounter_class(visit_type)

        # Build encounter resource
        encounter = {
            'resourceType': 'Encounter',
            'id': enc_id,
            'status': 'finished',
            'meta': {
                'tag': [make_meta_tag('stanford-myhealth-visits', 'Stanford MyHealth Visits Scrape')]
            },
            'class': enc_class,
            'type': [{
                'text': visit_type
            }] if visit_type else [],
            'period': {
                'start': start_dt
            },
            'participant': [],
            'serviceProvider': {
                'display': 'Stanford Health Care'
            }
        }

        # Add identifier if we have a CSN
        if csn_id:
            encounter['identifier'] = [{
                'system': 'urn:stanford:myhealth:csn',
                'value': str(csn_id)
            }]

        # Add provider as participant
        if provider:
            encounter['participant'].append({
                'individual': {'display': provider},
                'type': [{'coding': [{
                    'system': 'http://terminology.hl7.org/CodeSystem/v3-ParticipationType',
                    'code': 'ATND',
                    'display': 'attender'
                }]}]
            })

        # Add department/specialty as serviceType
        if dept_name:
            service_text = dept_name
            if specialty:
                service_text += f' ({specialty})'
            encounter['serviceType'] = {'text': service_text}

        # Add reason if available
        reason_text = visit.get('ReasonForVisit', '') or visit.get('ChiefComplaint', '')
        if reason_text:
            encounter['reasonCode'] = [{'text': reason_text}]

        # Note about clinical notes availability
        notes = []
        if visit.get('IsClinicalNoteAvailable'):
            notes.append('Clinical note available')
        if visit.get('IsLocal') is False:
            notes.append('Non-local visit')
        org_key = visit.get('_orgKey', '')
        if org_key:
            notes.append(f'Org: {org_key}')

        # Build narrative for full-text search
        narrative_parts = [
            f'Visit Type: {visit_type}',
            f'Date: {start_dt}',
            f'Provider: {provider}' if provider else '',
            f'Department: {dept_name}' if dept_name else '',
            f'Specialty: {specialty}' if specialty else '',
            f'Reason: {reason_text}' if reason_text else '',
        ]
        if notes:
            narrative_parts.append(f'Notes: {"; ".join(notes)}')
        narrative_text = '\n'.join(p for p in narrative_parts if p)

        encounter['text'] = make_narrative(narrative_text, title=f'{visit_type} - {provider}')

        # Clean up empty lists
        if not encounter['participant']:
            del encounter['participant']
        if not encounter.get('type'):
            del encounter['type']

        bundle['entry'].append({
            'resource': encounter,
            'request': {'method': 'PUT', 'url': f'Encounter/{enc_id}'}
        })

    with open('stanford_visits_fhir_bundle.json', 'w') as f:
        json.dump(bundle, f, indent=2)

    print(f'Converted {len(bundle["entry"])} Stanford visits to FHIR Encounter resources')
    print(f'Output: stanford_visits_fhir_bundle.json')

    # Show sample
    if bundle['entry']:
        sample = bundle['entry'][0]['resource']
        print(f'\nSample:')
        print(f'  ID: {sample["id"]}')
        print(f'  Class: {sample["class"]["display"]}')
        print(f'  Type: {sample.get("type", [{}])[0].get("text", "N/A")}')
        print(f'  Period: {sample["period"]["start"]}')
        if sample.get('participant'):
            print(f'  Provider: {sample["participant"][0]["individual"]["display"]}')
        if sample.get('serviceType'):
            print(f'  Service: {sample["serviceType"]["text"]}')


if __name__ == '__main__':
    main()
