#!/usr/bin/env python3
"""
ingest_to_hapi.py — Load FHIR resources from MASTER bundle into HAPI FHIR server.

Handles:
  - Transaction bundles (batched for performance)
  - Dependency ordering (Patient before Observations, etc.)
  - Resume from failure (tracks ingested resource IDs)
  - Progress reporting
  - Full-text indexing via HAPI's built-in search

Usage:
    python3 ingest_to_hapi.py                          # Load everything
    python3 ingest_to_hapi.py --file other_bundle.json  # Load a specific file
    python3 ingest_to_hapi.py --dry-run                 # Count without loading
    python3 ingest_to_hapi.py --reset                   # Clear server first
    python3 ingest_to_hapi.py --batch-size 50           # Adjust batch size

Requires: requests (pip install requests)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from collections import defaultdict
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("Installing requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests",
                           "--break-system-packages", "-q"])
    import requests

# ─── Configuration ──────────────────────────────────────────────────────────

DEFAULT_FHIR_BASE = "http://localhost:8080/fhir"
DEFAULT_BUNDLE = str(Path(__file__).parent / "MASTER_health_record_FINAL.json")
PROGRESS_FILE = str(Path(__file__).parent / ".ingest_progress.json")

# Resource types that must be loaded first (dependency order)
LOAD_ORDER = [
    "Organization",
    "Location",
    "Practitioner",
    "Patient",
    "Device",
    "Medication",
    "Encounter",
    "Condition",
    "AllergyIntolerance",
    "Procedure",
    "Immunization",
    "CarePlan",
    "MedicationStatement",
    "MedicationRequest",
    "Observation",
    "DiagnosticReport",
    "DocumentReference",
    "ImagingStudy",
    "Binary",
]

# ─── Helpers ────────────────────────────────────────────────────────────────

def wait_for_server(base_url, timeout=300):
    """Wait for HAPI FHIR to be ready."""
    print(f"Waiting for HAPI FHIR at {base_url}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{base_url}/metadata", timeout=10)
            if r.status_code == 200:
                print("  Server is ready.")
                return True
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(5)
        elapsed = int(time.time() - start)
        print(f"  Waiting... ({elapsed}s)")
    print("  ERROR: Server did not become ready.")
    return False


def load_progress():
    """Load set of already-ingested resource IDs for resume support."""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            data = json.load(f)
            return set(data.get("ingested", []))
    return set()


def save_progress(ingested_ids):
    """Save progress for resume."""
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"ingested": list(ingested_ids), "timestamp": time.time()}, f)


def clear_progress():
    """Remove progress file."""
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


def sanitize_id(resource_id):
    """
    Make resource IDs HAPI-compatible.
    HAPI FHIR requires IDs matching [A-Za-z0-9\\-\\.]{1,64}
    and at least one non-numeric character.
    """
    if not resource_id:
        return None
    # Replace problematic characters
    sanitized = resource_id.replace("/", "-").replace("_", "-")
    # Remove characters not in the allowed set
    sanitized = "".join(c for c in sanitized if c.isalnum() or c in "-.")
    # HAPI requires at least one non-numeric character
    if sanitized.isdigit():
        sanitized = "r-" + sanitized
    # Truncate to 64 chars
    if len(sanitized) > 64:
        # Use a hash suffix to maintain uniqueness
        import hashlib
        h = hashlib.md5(resource_id.encode()).hexdigest()[:12]
        sanitized = sanitized[:51] + "-" + h
    return sanitized or None


def prepare_resource(resource):
    """
    Clean up a resource for HAPI FHIR R4 ingestion.
    Fixes common issues from C-CDA conversions and non-standard FHIR.
    Returns the cleaned resource or None if it should be skipped.
    """
    resource = json.loads(json.dumps(resource))  # deep copy
    rt = resource.get("resourceType")

    if not rt:
        return None

    # ── Fix 1: "patient" → "subject" (DSTU2/STU3 → R4) ──────────────
    # In FHIR R4, most clinical resources use "subject" not "patient"
    SUBJECT_TYPES = {
        "Observation", "Condition", "Procedure", "DiagnosticReport",
        "MedicationRequest", "MedicationStatement", "DocumentReference",
        "CarePlan", "ImagingStudy", "Immunization", "AllergyIntolerance",
        "Encounter",
    }
    if rt in SUBJECT_TYPES and "patient" in resource and "subject" not in resource:
        resource["subject"] = resource.pop("patient")

    # ── Fix 2: Condition clinicalStatus / verificationStatus systems ──
    if rt == "Condition":
        _fix_codeable_concept_system(
            resource, "clinicalStatus",
            "http://terminology.hl7.org/CodeSystem/condition-clinical"
        )
        _fix_codeable_concept_system(
            resource, "verificationStatus",
            "http://terminology.hl7.org/CodeSystem/condition-ver-status"
        )

    # ── Fix 3: AllergyIntolerance clinicalStatus / verificationStatus ─
    if rt == "AllergyIntolerance":
        _fix_codeable_concept_system(
            resource, "clinicalStatus",
            "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
        )
        _fix_codeable_concept_system(
            resource, "verificationStatus",
            "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"
        )

    # ── Fix 4: Encounter.class must be a Coding (not CodeableConcept) ─
    if rt == "Encounter" and "class" in resource:
        cls = resource["class"]
        if isinstance(cls, dict) and "coding" in cls:
            # It's a CodeableConcept, extract the first Coding
            codings = cls["coding"]
            if codings:
                resource["class"] = codings[0]

    # ── Fix 5: MedicationRequest.reportedBoolean ─────────────────────
    # Some versions use reportedBoolean; HAPI R4 expects reported[x]
    # This is actually fine in R4, but ensure it's a boolean
    if rt == "MedicationRequest" and "reportedBoolean" in resource:
        val = resource["reportedBoolean"]
        if not isinstance(val, bool):
            resource["reportedBoolean"] = bool(val)

    # ── Fix 6: DiagnosticReport — ensure presentedForm data is valid ──
    if rt == "DiagnosticReport" and "presentedForm" in resource:
        for pf in resource["presentedForm"]:
            if "data" in pf and len(pf["data"]) > 5_000_000:
                # Too large for HAPI inline; drop it
                pf.pop("data", None)

    # ── Fix 7: DocumentReference — content.attachment validation ──────
    if rt == "DocumentReference" and "content" in resource:
        for content in resource["content"]:
            att = content.get("attachment", {})
            if "data" in att and len(att["data"]) > 5_000_000:
                att.pop("data", None)

    # ── Fix 8: Observation category system ────────────────────────────
    if rt == "Observation" and "category" in resource:
        for cat in resource["category"]:
            _fix_codeable_concept_system(
                {"field": cat}, "field",
                "http://terminology.hl7.org/CodeSystem/observation-category",
                only_if_missing=True
            )

    # ── Fix 9: Invalid date formats ──────────────────────────────────
    # Convert "M/D/YYYY H:MM:SS AM/PM" → "YYYY-MM-DDTHH:MM:SS"
    # Convert "MM/DD/YYYY" → "YYYY-MM-DD"
    _fix_dates_recursive(resource)

    # ── Fix 10: DiagnosticReport.result with full URLs instead of refs ─
    if rt == "DiagnosticReport" and "result" in resource:
        fixed_results = []
        for ref_obj in resource["result"]:
            ref_str = ref_obj.get("reference", "")
            if ref_str.startswith("https:") or ref_str.startswith("http:"):
                # Convert absolute URL to relative reference or drop it
                # Try to extract ResourceType/id from the URL
                import re as _re
                match = _re.search(r'(Observation|DiagnosticReport)/([^/\?]+)', ref_str)
                if match:
                    ref_obj["reference"] = f"{match.group(1)}/{match.group(2)}"
                    fixed_results.append(ref_obj)
                # else: drop this invalid reference
            else:
                fixed_results.append(ref_obj)
        resource["result"] = fixed_results

    # ── Fix 12: Clean up URL-style references in any reference field ──
    # HAPI rejects references like "www.questdiagnostics.com" or full URLs
    # in fields like performer, author, etc.
    _clean_bad_references_recursive(resource)

    # ── Fix 11: DocumentReference attachment.data must be base64 ──────
    if rt == "DocumentReference" and "content" in resource:
        for content in resource["content"]:
            att = content.get("attachment", {})
            if "data" in att:
                data = att["data"]
                # If data looks like plain text (not base64), encode it
                import base64 as _b64
                try:
                    _b64.b64decode(data, validate=True)
                except Exception:
                    # Not valid base64 — encode the raw text
                    att["data"] = _b64.b64encode(data.encode("utf-8")).decode("ascii")
                    if "contentType" not in att:
                        att["contentType"] = "text/plain"

    # ── Sanitize the resource ID ──────────────────────────────────────
    original_id = resource.get("id", "")
    new_id = sanitize_id(original_id)
    if new_id:
        resource["id"] = new_id

    # Fix references to use sanitized IDs
    _fix_references(resource)

    # Remove server-assigned metadata that could conflict
    if "meta" in resource:
        meta = dict(resource["meta"])
        meta.pop("versionId", None)
        meta.pop("lastUpdated", None)
        resource["meta"] = meta

    # Handle Binary resources — HAPI has a size limit for inline data
    if rt == "Binary" and "data" in resource:
        data_len = len(resource.get("data", ""))
        if data_len > 10_000_000:  # 10MB limit
            resource["data"] = ""
            if "meta" not in resource:
                resource["meta"] = {}
            resource["meta"]["tag"] = resource["meta"].get("tag", []) + [
                {"system": "http://phv/tags", "code": "truncated-binary",
                 "display": f"Original data was {data_len} bytes"}
            ]

    return resource


def _is_bad_reference(ref_str):
    """Check if a reference string is an invalid/non-FHIR reference."""
    if not ref_str or not isinstance(ref_str, str):
        return False
    # Valid FHIR references: "ResourceType/id", URNs, or #contained
    if ref_str.startswith("urn:") or ref_str.startswith("#"):
        return False
    if "/" in ref_str:
        first_part = ref_str.split("/")[0]
        # Valid resource type: starts with uppercase letter, alpha only
        if first_part and first_part[0].isupper() and first_part.isalpha():
            return False
    # Anything else (www.*, bare URLs, plain strings without ResourceType/) is bad
    return True


def _clean_bad_references_recursive(obj):
    """
    Recursively walk a resource and fix or remove invalid references.
    Handles both single reference objects and lists of references.
    """
    if isinstance(obj, dict):
        keys_to_delete = []
        for key, val in obj.items():
            if isinstance(val, dict) and "reference" in val:
                ref_str = val["reference"]
                if _is_bad_reference(ref_str):
                    # Try to extract a valid FHIR reference from a URL
                    import re as _re
                    match = _re.search(
                        r'(Patient|Observation|Practitioner|Organization|'
                        r'DiagnosticReport|Encounter|Specimen|ServiceRequest|'
                        r'Medication|Location|Device)/([^/\?\s]+)', ref_str)
                    if match:
                        val["reference"] = f"{match.group(1)}/{match.group(2)}"
                    elif "display" in val:
                        # Keep display, drop the bad reference
                        del val["reference"]
                    else:
                        # Nothing salvageable — mark for deletion
                        keys_to_delete.append(key)
                else:
                    _clean_bad_references_recursive(val)
            elif isinstance(val, list):
                # Filter list items that are bad references
                cleaned = []
                for item in val:
                    if isinstance(item, dict) and "reference" in item:
                        ref_str = item["reference"]
                        if _is_bad_reference(ref_str):
                            import re as _re
                            match = _re.search(
                                r'(Patient|Observation|Practitioner|Organization|'
                                r'DiagnosticReport|Encounter|Specimen|ServiceRequest|'
                                r'Medication|Location|Device)/([^/\?\s]+)', ref_str)
                            if match:
                                item["reference"] = f"{match.group(1)}/{match.group(2)}"
                                cleaned.append(item)
                            elif "display" in item:
                                del item["reference"]
                                cleaned.append(item)
                            # else: drop it entirely
                        else:
                            cleaned.append(item)
                    else:
                        cleaned.append(item)
                        if isinstance(item, (dict, list)):
                            _clean_bad_references_recursive(item)
                obj[key] = cleaned
            elif isinstance(val, dict):
                _clean_bad_references_recursive(val)
        for key in keys_to_delete:
            del obj[key]


def _fix_dates_recursive(obj):
    """
    Recursively find and fix non-FHIR date formats.
    Converts "M/D/YYYY H:MM:SS AM/PM" → "YYYY-MM-DDTHH:MM:SS"
    Converts "M/D/YYYY" or "MM/DD/YYYY" → "YYYY-MM-DD"
    """
    import re as _re
    from datetime import datetime as _dt

    # Fields that are FHIR dateTime or instant types
    DATE_FIELDS = {
        "issued", "effectiveDateTime", "date", "recordedDate", "authoredOn",
        "performedDateTime", "occurrenceDateTime", "sent", "received",
        "creation", "lastUpdated", "onset", "abatement",
    }

    # Pattern: M/D/YYYY with optional time
    US_DATE_PATTERN = _re.compile(
        r'^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM))?$'
    )

    if isinstance(obj, dict):
        for key in list(obj.keys()):
            val = obj[key]
            if key in DATE_FIELDS and isinstance(val, str):
                m = US_DATE_PATTERN.match(val.strip())
                if m:
                    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if m.group(4):  # Has time
                        hour = int(m.group(4))
                        minute = int(m.group(5))
                        second = int(m.group(6))
                        ampm = m.group(7)
                        if ampm == "PM" and hour != 12:
                            hour += 12
                        elif ampm == "AM" and hour == 12:
                            hour = 0
                        try:
                            dt = _dt(year, month, day, hour, minute, second)
                            obj[key] = dt.strftime("%Y-%m-%dT%H:%M:%S")
                        except ValueError:
                            pass
                    else:  # Date only
                        try:
                            dt = _dt(year, month, day)
                            obj[key] = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass
            elif isinstance(val, (dict, list)):
                _fix_dates_recursive(val)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _fix_dates_recursive(item)


def _fix_codeable_concept_system(resource, field_name, system_url,
                                  only_if_missing=False):
    """Add missing system URI to a CodeableConcept's codings."""
    cc = resource.get(field_name)
    if not cc or not isinstance(cc, dict):
        return
    codings = cc.get("coding", [])
    for coding in codings:
        if only_if_missing and "system" in coding:
            continue
        if "system" not in coding:
            coding["system"] = system_url


def _extract_diagnostics(response):
    """Extract readable error message from a HAPI OperationOutcome response."""
    try:
        body = response.json()
        issues = body.get("issue", [])
        messages = []
        for issue in issues[:3]:
            diag = issue.get("diagnostics", "")
            detail = issue.get("details", {}).get("text", "")
            msg = diag or detail
            if msg:
                messages.append(msg[:300])
        if messages:
            return f"HTTP {response.status_code}: " + "; ".join(messages)
    except Exception:
        pass
    return f"HTTP {response.status_code}: {response.text[:500]}"


def _fix_references(obj):
    """Recursively fix reference fields to use sanitized IDs."""
    if isinstance(obj, dict):
        if "reference" in obj and isinstance(obj["reference"], str):
            ref = obj["reference"]
            # Handle "ResourceType/id" format
            if "/" in ref:
                parts = ref.split("/", 1)
                if len(parts) == 2:
                    new_id = sanitize_id(parts[1])
                    if new_id:
                        obj["reference"] = f"{parts[0]}/{new_id}"
        for v in obj.values():
            _fix_references(v)
    elif isinstance(obj, list):
        for item in obj:
            _fix_references(item)


# ─── ID Mapping ────────────────────────────────────────────────────────────

def build_id_map(entries):
    """Build mapping from original IDs to sanitized IDs and fullUrls."""
    id_map = {}
    for entry in entries:
        resource = entry.get("resource", {})
        original_id = resource.get("id", "")
        full_url = entry.get("fullUrl", "")
        sanitized = sanitize_id(original_id)
        if original_id and sanitized:
            id_map[original_id] = sanitized
            if full_url:
                id_map[full_url] = sanitized
    return id_map


# ─── Ingestion ──────────────────────────────────────────────────────────────

def ingest_batch(session, base_url, resources, batch_num, total_batches):
    """Send a transaction bundle to HAPI FHIR."""
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": []
    }

    for resource in resources:
        rt = resource["resourceType"]
        rid = resource.get("id", "")
        entry = {
            "resource": resource,
            "request": {
                "method": "PUT",
                "url": f"{rt}/{rid}" if rid else rt
            }
        }
        if rid:
            entry["fullUrl"] = f"{rt}/{rid}"
        bundle["entry"].append(entry)

    try:
        r = session.post(
            base_url,
            json=bundle,
            headers={"Content-Type": "application/fhir+json"},
            timeout=120
        )
        if r.status_code in (200, 201):
            return True, len(resources), None
        else:
            # Try to extract error details
            try:
                error_body = r.json()
                issues = error_body.get("issue", [])
                error_msg = "; ".join(
                    i.get("diagnostics", i.get("details", {}).get("text", ""))
                    for i in issues[:3]
                )
            except Exception:
                error_msg = r.text[:500]
            return False, 0, f"HTTP {r.status_code}: {error_msg}"
    except requests.exceptions.Timeout:
        return False, 0, "Request timed out (120s)"
    except requests.exceptions.ConnectionError as e:
        return False, 0, f"Connection error: {e}"


def ingest_single(session, base_url, resource):
    """Fallback: ingest a single resource via PUT."""
    rt = resource["resourceType"]
    rid = resource.get("id", "")
    url = f"{base_url}/{rt}/{rid}" if rid else f"{base_url}/{rt}"
    method = "PUT" if rid else "POST"

    try:
        r = getattr(session, method.lower())(
            url,
            json=resource,
            headers={"Content-Type": "application/fhir+json"},
            timeout=60
        )
        if r.status_code in (200, 201):
            return True, None
        else:
            # Extract diagnostics from OperationOutcome
            error_msg = _extract_diagnostics(r)
            return False, error_msg
    except Exception as e:
        return False, str(e)


def run_ingestion(base_url, bundle_path, batch_size=25, dry_run=False,
                  reset=False, resume=True):
    """Main ingestion loop."""

    # Load the bundle
    print(f"\nLoading {bundle_path}...")
    t0 = time.time()
    with open(bundle_path) as f:
        data = json.load(f)
    entries = data.get("entry", [])
    load_time = time.time() - t0
    print(f"  Loaded {len(entries)} entries in {load_time:.1f}s")

    # Group by resource type
    by_type = defaultdict(list)
    for entry in entries:
        resource = entry.get("resource", {})
        rt = resource.get("resourceType", "Unknown")
        by_type[rt].append(resource)

    print(f"\nResource breakdown:")
    for rt in LOAD_ORDER:
        if rt in by_type:
            print(f"  {rt}: {len(by_type[rt])}")
    # Any types not in LOAD_ORDER
    for rt, resources in sorted(by_type.items()):
        if rt not in LOAD_ORDER:
            print(f"  {rt}: {len(resources)} (unordered)")

    if dry_run:
        print("\n[DRY RUN] Would ingest the above resources. Exiting.")
        return

    # Wait for server
    if not wait_for_server(base_url):
        sys.exit(1)

    session = requests.Session()

    # Reset if requested
    if reset:
        print("\nResetting server (deleting all resources)...")
        for rt in reversed(LOAD_ORDER):
            try:
                r = session.delete(f"{base_url}/{rt}?_cascade=delete",
                                    timeout=60)
                print(f"  Deleted {rt}: {r.status_code}")
            except Exception:
                pass
        clear_progress()

    # Load progress for resume
    ingested_ids = load_progress() if resume else set()
    if ingested_ids:
        print(f"\nResuming: {len(ingested_ids)} resources already ingested")

    # Ingest in dependency order
    total_success = 0
    total_errors = 0
    total_skipped = 0
    error_log = []

    ordered_types = LOAD_ORDER + [rt for rt in by_type if rt not in LOAD_ORDER]

    for rt in ordered_types:
        if rt not in by_type:
            continue

        resources = by_type[rt]
        print(f"\n{'='*60}")
        print(f"Ingesting {rt} ({len(resources)} resources)")
        print(f"{'='*60}")

        # Prepare and filter resources
        prepared = []
        for r in resources:
            cleaned = prepare_resource(r)
            if cleaned is None:
                total_skipped += 1
                continue
            rid = cleaned.get("id", "")
            resource_key = f"{rt}/{rid}"
            if resource_key in ingested_ids:
                total_skipped += 1
                continue
            prepared.append(cleaned)

        if not prepared:
            print(f"  All {len(resources)} already ingested, skipping.")
            continue

        print(f"  {len(prepared)} to ingest, {len(resources) - len(prepared)} skipped")

        # Batch and send
        batches = [prepared[i:i+batch_size] for i in range(0, len(prepared), batch_size)]

        for batch_idx, batch in enumerate(batches):
            success, count, error = ingest_batch(
                session, base_url, batch, batch_idx + 1, len(batches)
            )

            if success:
                total_success += count
                for r in batch:
                    rid = r.get("id", "")
                    ingested_ids.add(f"{rt}/{rid}")
                # Progress bar
                pct = (batch_idx + 1) / len(batches) * 100
                print(f"  [{pct:5.1f}%] Batch {batch_idx+1}/{len(batches)}: "
                      f"{count} resources OK  (total: {total_success})")
            else:
                # Retry individually
                print(f"  Batch {batch_idx+1} failed: {error}")
                print(f"  Retrying {len(batch)} resources individually...")
                for r in batch:
                    ok, err = ingest_single(session, base_url, r)
                    if ok:
                        total_success += 1
                        rid = r.get("id", "")
                        ingested_ids.add(f"{rt}/{rid}")
                    else:
                        total_errors += 1
                        error_log.append({
                            "type": rt,
                            "id": r.get("id", "unknown"),
                            "error": err
                        })

            # Save progress every 10 batches
            if (batch_idx + 1) % 10 == 0:
                save_progress(ingested_ids)

        # Save progress after each resource type
        save_progress(ingested_ids)

    # Final summary
    print(f"\n{'='*60}")
    print(f"INGESTION COMPLETE")
    print(f"{'='*60}")
    print(f"  Successful: {total_success}")
    print(f"  Skipped:    {total_skipped}")
    print(f"  Errors:     {total_errors}")
    print(f"  Total time: {time.time() - t0:.1f}s")

    if error_log:
        error_file = str(Path(__file__).parent / "ingest_errors.json")
        with open(error_file, "w") as f:
            json.dump(error_log, f, indent=2)
        print(f"\n  Error details saved to: {error_file}")
        print(f"\n  First 5 errors:")
        for err in error_log[:5]:
            print(f"    {err['type']}/{err['id']}: {err['error'][:100]}")

    # Clean up progress file on full success
    if total_errors == 0:
        clear_progress()
        print("\n  All resources ingested successfully!")

    return total_success, total_errors


# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Load FHIR resources into HAPI FHIR server"
    )
    parser.add_argument("--file", default=DEFAULT_BUNDLE,
                        help="Path to FHIR Bundle JSON file")
    parser.add_argument("--base-url", default=DEFAULT_FHIR_BASE,
                        help="HAPI FHIR base URL")
    parser.add_argument("--batch-size", type=int, default=25,
                        help="Resources per transaction bundle (default: 25)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count resources without loading")
    parser.add_argument("--reset", action="store_true",
                        help="Delete all server data before loading")
    parser.add_argument("--no-resume", action="store_true",
                        help="Don't resume from previous run")

    args = parser.parse_args()

    run_ingestion(
        base_url=args.base_url,
        bundle_path=args.file,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        reset=args.reset,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()
