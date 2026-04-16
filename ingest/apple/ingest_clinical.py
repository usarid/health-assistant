#!/usr/bin/env python3
"""
Apple Health Clinical Records Ingestion Script

This script ingests 12,313 FHIR clinical records exported from Apple Health Records
and consolidates them into a unified dataset, deduplicating against existing data.

Requirements:
- Reads all JSON files from apple_health_export/clinical-records/
- Groups by resource type
- Deduplicates using identifiers, dates, and codes
- Tags provenance as "Apple Health Records"
- Creates a FHIR Bundle
- Generates comprehensive report
"""

import json
import os
import sys
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime
from typing import Dict, List, Set, Tuple, Any
import hashlib

def load_existing_bundle(bundle_path: str) -> Dict[str, Any]:
    """Load the existing consolidated FHIR bundle."""
    if not os.path.exists(bundle_path):
        return {"entry": []}

    try:
        with open(bundle_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading existing bundle: {e}")
        return {"entry": []}

def create_resource_identifier(resource: Dict[str, Any]) -> str:
    """
    Create a canonical identifier for a resource to detect duplicates.
    Uses resource type, identifiers, codes, dates, and subject.
    """
    parts = []

    resource_type = resource.get('resourceType', '')
    parts.append(resource_type)

    # Add identifiers if available
    if 'identifier' in resource:
        identifiers = resource['identifier']
        if isinstance(identifiers, list):
            for ident in identifiers:
                if isinstance(ident, dict):
                    system = ident.get('system', '')
                    value = ident.get('value', '')
                    if system and value:
                        parts.append(f"{system}#{value}")
        elif isinstance(identifiers, dict):
            system = identifiers.get('system', '')
            value = identifiers.get('value', '')
            if system and value:
                parts.append(f"{system}#{value}")

    # Add code if available (for Observations, Conditions, etc.)
    if 'code' in resource and isinstance(resource['code'], dict):
        code_obj = resource['code']
        if 'coding' in code_obj and isinstance(code_obj['coding'], list):
            for coding in code_obj['coding']:
                if isinstance(coding, dict):
                    system = coding.get('system', '')
                    code = coding.get('code', '')
                    if system and code:
                        parts.append(f"{system}#{code}")

    # Add subject reference
    if 'subject' in resource and isinstance(resource['subject'], dict):
        subject_ref = resource['subject'].get('reference', '')
        if subject_ref:
            parts.append(f"subject#{subject_ref}")

    # Add effective date(s) for temporal deduplication
    if 'effectiveDateTime' in resource:
        parts.append(f"date#{resource['effectiveDateTime']}")
    elif 'effectivePeriod' in resource and isinstance(resource['effectivePeriod'], dict):
        start = resource['effectivePeriod'].get('start', '')
        if start:
            parts.append(f"date#{start}")
    elif 'issued' in resource:
        parts.append(f"date#{resource['issued']}")
    elif 'onsetDateTime' in resource:
        parts.append(f"date#{resource['onsetDateTime']}")

    # For MedicationRequest/MedicationStatement, include medication
    if 'medicationReference' in resource and isinstance(resource['medicationReference'], dict):
        med_ref = resource['medicationReference'].get('reference', '')
        if med_ref:
            parts.append(f"medication#{med_ref}")
    elif 'medicationCodeableConcept' in resource and isinstance(resource['medicationCodeableConcept'], dict):
        med_obj = resource['medicationCodeableConcept']
        if 'coding' in med_obj and isinstance(med_obj['coding'], list):
            for coding in med_obj['coding']:
                if isinstance(coding, dict):
                    code = coding.get('code', '')
                    if code:
                        parts.append(f"medication#{code}")

    # Create canonical identifier
    canonical = "|".join(parts) if parts else resource.get('id', '')
    return hashlib.sha256(canonical.encode()).hexdigest()

def deduplicate_resources(apple_resources: List[Dict], existing_bundle: Dict) -> Tuple[List[Dict], Dict[str, int]]:
    """
    Deduplicate Apple Health resources against existing data.
    Returns (deduplicated_resources, overlap_stats)
    """
    # Build a set of existing identifiers
    existing_identifiers = set()
    existing_by_type = defaultdict(set)

    for entry in existing_bundle.get('entry', []):
        resource = entry.get('resource', {})
        resource_type = resource.get('resourceType', '')

        # Create identifier for existing resource
        identifier = create_resource_identifier(resource)
        existing_identifiers.add(identifier)
        existing_by_type[resource_type].add(identifier)

    # Filter Apple resources that are not in existing data
    unique_apple_resources = []
    overlap_count = defaultdict(int)

    for resource in apple_resources:
        resource_type = resource.get('resourceType', '')
        identifier = create_resource_identifier(resource)

        if identifier not in existing_identifiers:
            unique_apple_resources.append(resource)
        else:
            overlap_count[resource_type] += 1

    overlap_stats = {
        'total_apple_resources': len(apple_resources),
        'unique_apple_resources': len(unique_apple_resources),
        'duplicate_resources': len(apple_resources) - len(unique_apple_resources),
        'overlap_by_type': dict(overlap_count),
        'existing_total': len(existing_identifiers),
        'existing_by_type': {k: len(v) for k, v in existing_by_type.items()}
    }

    return unique_apple_resources, overlap_stats

def add_apple_health_provenance(resource: Dict[str, Any]) -> Dict[str, Any]:
    """Add Apple Health Records provenance information to a resource."""
    # Initialize extension if not present
    if 'extension' not in resource:
        resource['extension'] = []

    # Add Apple Health provenance extension
    provenance_extension = {
        "url": "http://example.org/fhir/StructureDefinition/data-source",
        "valueString": "Apple Health Records"
    }

    # Add ingest date extension
    ingest_extension = {
        "url": "http://example.org/fhir/StructureDefinition/ingest-date",
        "valueDateTime": datetime.now().isoformat()
    }

    resource['extension'].append(provenance_extension)
    resource['extension'].append(ingest_extension)

    return resource

def read_apple_health_records(source_dir: str) -> Tuple[List[Dict], Dict[str, int], Dict[str, List[str]]]:
    """
    Read all Apple Health FHIR records from the specified directory.
    Returns (resources, file_counts_by_type, date_ranges_by_type)
    """
    resources = []
    file_counts = defaultdict(int)
    date_ranges = defaultdict(lambda: {'min': None, 'max': None})

    source_path = Path(source_dir)

    if not source_path.exists():
        print(f"Error: Source directory not found: {source_dir}")
        return [], {}, {}

    json_files = list(source_path.glob('*.json'))
    print(f"Found {len(json_files)} JSON files in {source_dir}")

    for i, json_file in enumerate(json_files):
        if (i + 1) % 1000 == 0:
            print(f"Processing file {i + 1}/{len(json_files)}...")

        try:
            with open(json_file, 'r') as f:
                resource = json.load(f)

            resource_type = resource.get('resourceType', 'Unknown')
            file_counts[resource_type] += 1

            # Track date ranges
            dates = extract_dates_from_resource(resource)
            if dates:
                for date in dates:
                    if date_ranges[resource_type]['min'] is None or date < date_ranges[resource_type]['min']:
                        date_ranges[resource_type]['min'] = date
                    if date_ranges[resource_type]['max'] is None or date > date_ranges[resource_type]['max']:
                        date_ranges[resource_type]['max'] = date

            resources.append(resource)

        except Exception as e:
            print(f"Error reading {json_file}: {e}")
            continue

    return resources, dict(file_counts), dict(date_ranges)

def extract_dates_from_resource(resource: Dict[str, Any]) -> List[str]:
    """Extract all dates from a FHIR resource."""
    dates = []

    # Common date fields
    date_fields = [
        'effectiveDateTime', 'issued', 'onsetDateTime', 'created', 'authoredOn',
        'occurrenceDateTime', 'recordedDate', 'assertedDate'
    ]

    for field in date_fields:
        if field in resource and isinstance(resource[field], str):
            dates.append(resource[field][:10])  # Extract date part only

    # Handle date periods
    period_fields = [
        'effectivePeriod', 'occurencePeriod', 'assertionPeriod', 'periodInterval'
    ]

    for field in period_fields:
        if field in resource and isinstance(resource[field], dict):
            period = resource[field]
            if 'start' in period and isinstance(period['start'], str):
                dates.append(period['start'][:10])
            if 'end' in period and isinstance(period['end'], str):
                dates.append(period['end'][:10])

    return sorted(list(set(dates)))

def create_fhir_bundle(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Create a FHIR Bundle from a list of resources."""
    bundle_entries = []

    for resource in entries:
        entry = {
            "fullUrl": f"{resource.get('resourceType', 'Resource')}/{resource.get('id', 'unknown')}",
            "resource": resource,
            "request": {
                "method": "POST",
                "url": resource.get('resourceType', 'Resource')
            }
        }
        bundle_entries.append(entry)

    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "timestamp": datetime.now().isoformat(),
        "total": len(bundle_entries),
        "entry": bundle_entries
    }

    return bundle

def generate_report(
    file_counts: Dict[str, int],
    apple_stats: Dict[str, int],
    overlap_stats: Dict[str, Any],
    date_ranges: Dict[str, Any],
    output_file: str
) -> None:
    """Generate a comprehensive ingestion report."""

    with open(output_file, 'w') as f:
        f.write("# Apple Health Clinical Records Ingestion Report\n\n")
        f.write(f"**Report Generated:** {datetime.now().isoformat()}\n\n")

        # Overview
        f.write("## Overview\n\n")
        f.write(f"- **Total Apple Health Records:** {overlap_stats.get('total_apple_resources', 0):,}\n")
        f.write(f"- **Unique Records (not in existing data):** {overlap_stats.get('unique_apple_resources', 0):,}\n")
        f.write(f"- **Duplicate Records (overlap with existing):** {overlap_stats.get('duplicate_resources', 0):,}\n")
        f.write(f"- **Existing Records in System:** {overlap_stats.get('existing_total', 0):,}\n\n")

        # File counts by resource type
        f.write("## Records by FHIR Resource Type\n\n")
        f.write("| Resource Type | Apple Health Count | Existing Count | Overlap | Unique |\n")
        f.write("|---|---:|---:|---:|---:|\n")

        for resource_type in sorted(file_counts.keys()):
            apple_count = file_counts[resource_type]
            existing_count = overlap_stats['existing_by_type'].get(resource_type, 0)
            overlap = overlap_stats['overlap_by_type'].get(resource_type, 0)
            unique = apple_count - overlap

            f.write(f"| {resource_type} | {apple_count:,} | {existing_count:,} | {overlap:,} | {unique:,} |\n")

        f.write("\n")

        # Date ranges
        f.write("## Data Date Ranges by Resource Type\n\n")
        f.write("| Resource Type | Earliest Date | Latest Date | Span |\n")
        f.write("|---|---|---|---|\n")

        for resource_type in sorted(date_ranges.keys()):
            date_info = date_ranges[resource_type]
            min_date = date_info.get('min', 'N/A')
            max_date = date_info.get('max', 'N/A')

            if min_date != 'N/A' and max_date != 'N/A':
                try:
                    min_dt = datetime.fromisoformat(min_date.replace('Z', '+00:00'))
                    max_dt = datetime.fromisoformat(max_date.replace('Z', '+00:00'))
                    span = (max_dt - min_dt).days
                    span_str = f"{span} days"
                except:
                    span_str = "N/A"
            else:
                span_str = "N/A"

            f.write(f"| {resource_type} | {min_date} | {max_date} | {span_str} |\n")

        f.write("\n")

        # Data quality observations
        f.write("## Data Quality Observations\n\n")

        total_apple = overlap_stats.get('total_apple_resources', 0)
        duplicate_pct = (overlap_stats.get('duplicate_resources', 0) / total_apple * 100) if total_apple > 0 else 0

        f.write(f"- **Deduplication Rate:** {duplicate_pct:.1f}% of Apple Health records were duplicates\n")
        f.write(f"- **Net New Records:** {overlap_stats.get('unique_apple_resources', 0):,} records added to dataset\n")
        f.write(f"- **Largest Resource Type:** {max(file_counts, key=file_counts.get) if file_counts else 'N/A'} ")
        f.write(f"({file_counts.get(max(file_counts, key=file_counts.get), 0):,} records)\n")

        # Most common overlaps
        f.write("\n### Overlap by Resource Type (Top Sources of Duplicates)\n\n")
        overlap_by_type = overlap_stats.get('overlap_by_type', {})
        sorted_overlaps = sorted(overlap_by_type.items(), key=lambda x: x[1], reverse=True)

        for resource_type, count in sorted_overlaps[:5]:
            pct = (count / file_counts.get(resource_type, 1)) * 100
            f.write(f"- **{resource_type}:** {count:,} duplicates ({pct:.1f}% of type)\n")

        f.write("\n")

        # Data additions
        f.write("## New Data Contributions from Apple Health Records\n\n")

        new_data_by_type = {}
        for resource_type in sorted(file_counts.keys()):
            apple_count = file_counts[resource_type]
            overlap = overlap_stats['overlap_by_type'].get(resource_type, 0)
            unique = apple_count - overlap
            new_data_by_type[resource_type] = unique

        total_new = sum(new_data_by_type.values())

        f.write(f"**Total New Records Added:** {total_new:,}\n\n")
        f.write("New records by type:\n\n")

        for resource_type in sorted(new_data_by_type.keys(), key=lambda x: new_data_by_type[x], reverse=True):
            unique = new_data_by_type[resource_type]
            if unique > 0:
                pct = (unique / total_new * 100) if total_new > 0 else 0
                f.write(f"- **{resource_type}:** {unique:,} new records ({pct:.1f}%)\n")

        f.write("\n")

        # Methodology
        f.write("## Deduplication Methodology\n\n")
        f.write("""
The following algorithm was used to identify duplicate resources:

1. **Canonical Identifier Creation:** For each resource, a canonical identifier was created using:
   - Resource type
   - System and value pairs from all identifier fields
   - Clinical codes (from code/coding fields)
   - Subject reference (patient)
   - Effective/recorded dates
   - Medication references (for medication-related resources)

2. **Hash-based Matching:** Canonical identifiers were hashed using SHA-256 to create a fingerprint

3. **Duplicate Detection:** Resources from Apple Health were compared against existing dataset:
   - Exact hash match = duplicate (same clinical data, same patient, same timing)
   - No match = unique record (added to consolidated dataset)

4. **Provenance Tagging:** All ingested records were tagged with:
   - Data source: "Apple Health Records"
   - Ingest timestamp: ISO 8601 format
   - Custom FHIR extension: data-source
   - Custom FHIR extension: ingest-date

This approach balances sensitivity (catching true duplicates from Epic/other EHR exports)
with specificity (not removing legitimately different clinical events).
""")

        f.write("\n")
        f.write("## Output Files\n\n")
        f.write("- **FHIR Bundle:** `apple_clinical_records_fhir_bundle.json`\n")
        f.write("- **Report:** `apple_clinical_records_report.md`\n")
        f.write("- **Script:** `ingest_apple_clinical_records.py`\n")

def main():
    """Main execution function."""

    # Paths
    source_dir = "/sessions/admiring-vigilant-brown/mnt/Medical/New exports/apple_health_export/clinical-records"
    output_dir = "/sessions/admiring-vigilant-brown/mnt/Medical/Synthesis"
    existing_bundle_path = os.path.join(output_dir, "FINAL_consolidated_health_record.json")
    output_bundle_path = os.path.join(output_dir, "apple_clinical_records_fhir_bundle.json")
    report_path = os.path.join(output_dir, "apple_clinical_records_report.md")

    print("=" * 80)
    print("Apple Health Clinical Records Ingestion")
    print("=" * 80)

    # Step 1: Read Apple Health records
    print("\n[1/5] Reading Apple Health records...")
    apple_resources, file_counts, date_ranges = read_apple_health_records(source_dir)
    print(f"Successfully read {len(apple_resources):,} records")

    # Step 2: Load existing bundle
    print("\n[2/5] Loading existing consolidated data...")
    existing_bundle = load_existing_bundle(existing_bundle_path)
    print(f"Loaded {len(existing_bundle.get('entry', []))} existing records")

    # Step 3: Deduplicate
    print("\n[3/5] Deduplicating records...")
    unique_resources, overlap_stats = deduplicate_resources(apple_resources, existing_bundle)
    print(f"Found {overlap_stats['unique_apple_resources']:,} unique records")
    print(f"Found {overlap_stats['duplicate_resources']:,} duplicate records")

    # Step 4: Add provenance
    print("\n[4/5] Adding Apple Health Records provenance...")
    for resource in unique_resources:
        add_apple_health_provenance(resource)
    print(f"Provenance added to {len(unique_resources):,} records")

    # Step 5: Create bundle and save
    print("\n[5/5] Creating FHIR Bundle...")
    bundle = create_fhir_bundle(unique_resources)

    with open(output_bundle_path, 'w') as f:
        json.dump(bundle, f, indent=2)

    print(f"Saved FHIR Bundle to {output_bundle_path}")
    print(f"Bundle contains {bundle['total']:,} entries")

    # Generate report
    print("\n[Report] Generating ingestion report...")
    generate_report(file_counts, apple_resources, overlap_stats, date_ranges, report_path)
    print(f"Report saved to {report_path}")

    # Summary statistics
    print("\n" + "=" * 80)
    print("INGESTION SUMMARY")
    print("=" * 80)
    print(f"\nApple Health Records Ingestion Complete:")
    print(f"  Total records processed: {len(apple_resources):,}")
    print(f"  Unique records (new data): {overlap_stats['unique_apple_resources']:,}")
    print(f"  Duplicate records: {overlap_stats['duplicate_resources']:,}")
    print(f"  Deduplication rate: {(overlap_stats['duplicate_resources']/len(apple_resources)*100):.1f}%")
    print(f"\nRecords by type:")
    for resource_type in sorted(file_counts.keys(), key=lambda x: file_counts[x], reverse=True):
        count = file_counts[resource_type]
        unique = count - overlap_stats['overlap_by_type'].get(resource_type, 0)
        print(f"  {resource_type:25s} {count:6,} total  {unique:6,} new")
    print("\n" + "=" * 80)

if __name__ == "__main__":
    main()
