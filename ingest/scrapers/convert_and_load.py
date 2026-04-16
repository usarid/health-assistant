#!/usr/bin/env python3
"""
Convert MyChart scraped data to FHIR R4 resources and load into HAPI FHIR.

Usage:
  python3 05_convert_and_load.py <scraped_json_file> [--fhir-base http://localhost:8080/fhir]

The script reads the JSON output from the browser console scrapers,
converts entries to FHIR R4 resources, and POSTs them to the HAPI FHIR server.
"""

import json
import sys
import re
import hashlib
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote


FHIR_BASE = "http://localhost:8080/fhir"
SOURCE_SYSTEM = "https://mskmychart.mskcc.org"


def fhir_post(resource_type, resource):
    """POST a FHIR resource to the server."""
    url = f"{FHIR_BASE}/{resource_type}"
    data = json.dumps(resource).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/fhir+json")
    req.add_header("Accept", "application/fhir+json")
    try:
        resp = urlopen(req)
        result = json.loads(resp.read())
        return result.get("id"), resp.status
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ERROR {e.code}: {body[:300]}")
        return None, e.code


def fhir_put(resource_type, resource_id, resource):
    """PUT a FHIR resource (upsert by id)."""
    url = f"{FHIR_BASE}/{resource_type}/{resource_id}"
    data = json.dumps(resource).encode("utf-8")
    req = Request(url, data=data, method="PUT")
    req.add_header("Content-Type", "application/fhir+json")
    req.add_header("Accept", "application/fhir+json")
    try:
        resp = urlopen(req)
        result = json.loads(resp.read())
        return result.get("id"), resp.status
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ERROR {e.code}: {body[:300]}")
        return None, e.code


def make_id(text):
    """Generate a stable ID from text."""
    return hashlib.md5(text.encode()).hexdigest()[:12]


def parse_date(text):
    """Try to parse a date from various formats."""
    if not text:
        return None
    # Try common formats
    for fmt in [
        "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%B %d, %Y",
        "%b %d, %Y", "%m-%d-%Y", "%Y/%m/%d",
        "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
    ]:
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Try regex extraction
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', text)
    if m:
        month, day, year = m.groups()
        if len(year) == 2:
            year = "20" + year if int(year) < 50 else "19" + year
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', text)
    if m:
        return m.group(0)
    return None


def convert_fhir_extract(data):
    """Convert data from the FHIR probe (01) — already in FHIR format."""
    resources = data.get("resources", {})
    results = []
    for rt, entries in resources.items():
        for entry in entries:
            entry.pop("id", None)  # Let server assign IDs
            entry.pop("meta", None)
            results.append((rt, entry))
    return results


def convert_internal_api(data):
    """Convert data from internal API scraper (02)."""
    results = []

    # Visits -> Encounter
    for v in data.get("visits", []):
        date = parse_date(v.get("date", v.get("Date", "")))
        enc = {
            "resourceType": "Encounter",
            "status": "finished",
            "class": {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "AMB",
                "display": "ambulatory"
            },
            "type": [{
                "text": v.get("type", v.get("Type", v.get("VisitType", "Visit")))
            }],
            "meta": {
                "source": SOURCE_SYSTEM
            }
        }
        if date:
            enc["period"] = {"start": date}

        provider = v.get("provider", v.get("Provider", v.get("ProviderName", "")))
        if provider:
            enc["participant"] = [{
                "individual": {"display": provider},
                "type": [{"text": "primary performer"}]
            }]

        location = v.get("location", v.get("Location", v.get("Department", "")))
        if location:
            enc["location"] = [{"location": {"display": location}}]

        reason = v.get("reason", v.get("Reason", v.get("VisitReason", "")))
        if reason:
            enc["reasonCode"] = [{"text": reason}]

        results.append(("Encounter", enc))

    # Messages -> Communication
    for m in data.get("messages", []):
        date = parse_date(m.get("date", m.get("Date", "")))
        comm = {
            "resourceType": "Communication",
            "status": "completed",
            "meta": {"source": SOURCE_SYSTEM},
        }
        if date:
            comm["sent"] = date

        subject_text = m.get("subject", m.get("Subject", ""))
        from_text = m.get("from", m.get("From", m.get("Sender", "")))

        if subject_text:
            comm["topic"] = {"text": subject_text}
        if from_text:
            comm["sender"] = {"display": from_text}

        body = m.get("body", m.get("Body", m.get("rawText", "")))
        if body:
            comm["payload"] = [{"contentString": body[:5000]}]

        results.append(("Communication", comm))

    # Test Results -> DiagnosticReport
    for t in data.get("testResults", []):
        date = parse_date(t.get("date", t.get("Date", "")))
        dr = {
            "resourceType": "DiagnosticReport",
            "status": "final",
            "meta": {"source": SOURCE_SYSTEM},
            "code": {"text": t.get("rawText", t.get("Name", "Test Result"))[:200]},
        }
        if date:
            dr["effectiveDateTime"] = date
        results.append(("DiagnosticReport", dr))

    # Medications -> MedicationRequest
    for m in data.get("medications", []):
        mr = {
            "resourceType": "MedicationRequest",
            "status": "active",
            "intent": "order",
            "meta": {"source": SOURCE_SYSTEM},
            "medicationCodeableConcept": {
                "text": m.get("rawText", m.get("Name", "Unknown Medication"))[:200]
            },
        }
        results.append(("MedicationRequest", mr))

    # Conditions -> Condition
    for c in data.get("conditions", []):
        desc = c if isinstance(c, str) else c.get("description", c.get("Name", ""))
        cond = {
            "resourceType": "Condition",
            "clinicalStatus": {
                "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]
            },
            "code": {"text": desc[:200]},
            "meta": {"source": SOURCE_SYSTEM},
        }
        results.append(("Condition", cond))

    # Allergies -> AllergyIntolerance
    for a in data.get("allergies", []):
        desc = a if isinstance(a, str) else a.get("description", a.get("Name", ""))
        ai = {
            "resourceType": "AllergyIntolerance",
            "clinicalStatus": {
                "coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]
            },
            "code": {"text": desc[:200]},
            "meta": {"source": SOURCE_SYSTEM},
        }
        results.append(("AllergyIntolerance", ai))

    # Immunizations -> Immunization
    for i in data.get("immunizations", []):
        desc = i if isinstance(i, str) else i.get("description", i.get("Name", ""))
        imm = {
            "resourceType": "Immunization",
            "status": "completed",
            "vaccineCode": {"text": desc[:200]},
            "meta": {"source": SOURCE_SYSTEM},
        }
        results.append(("Immunization", imm))

    # Procedures -> Procedure
    for p in data.get("procedures", []):
        desc = p if isinstance(p, str) else p.get("description", p.get("Name", ""))
        proc = {
            "resourceType": "Procedure",
            "status": "completed",
            "code": {"text": desc[:200]},
            "meta": {"source": SOURCE_SYSTEM},
        }
        results.append(("Procedure", proc))

    # Care Team -> Practitioner
    for ct in data.get("careTeam", []):
        raw = ct.get("rawText", "") if isinstance(ct, dict) else str(ct)
        pract = {
            "resourceType": "Practitioner",
            "meta": {"source": SOURCE_SYSTEM},
            "name": [{"text": raw[:200]}],
        }
        results.append(("Practitioner", pract))

    return results


def convert_dom_extract(data):
    """Convert data from DOM scraper (03) or page scraper (04)."""
    # DOM scraper output has the same structure as internal API
    # but may have rawText instead of structured fields
    results = convert_internal_api(data)

    # Also handle visit details
    for vd in data.get("visitDetails", []):
        text_parts = []
        if vd.get("title"):
            text_parts.append(vd["title"])
        for section_name, content in vd.get("sections", {}).items():
            text_parts.append(f"{section_name}: {content}")
        if not text_parts and vd.get("allText"):
            text_parts.append(vd["allText"][:5000])

        if text_parts:
            doc_ref = {
                "resourceType": "DocumentReference",
                "status": "current",
                "type": {"text": "Visit Detail"},
                "meta": {"source": SOURCE_SYSTEM},
                "description": vd.get("title", "Visit Detail")[:200],
                "content": [{
                    "attachment": {
                        "contentType": "text/plain",
                        "data": None,  # We'll use the description instead
                        "title": vd.get("title", "Visit")[:100],
                    }
                }],
            }
            # Store the full text as a contained note
            full_text = "\n\n".join(text_parts)[:10000]
            doc_ref["content"][0]["attachment"]["contentType"] = "text/plain"
            # Base64 encode the text
            import base64
            doc_ref["content"][0]["attachment"]["data"] = base64.b64encode(
                full_text.encode("utf-8")
            ).decode("ascii")
            results.append(("DocumentReference", doc_ref))

    # Handle message details
    for md in data.get("messageDetails", []):
        comm = {
            "resourceType": "Communication",
            "status": "completed",
            "meta": {"source": SOURCE_SYSTEM},
        }
        if md.get("date"):
            d = parse_date(md["date"])
            if d:
                comm["sent"] = d
        if md.get("subject"):
            comm["topic"] = {"text": md["subject"][:200]}
        if md.get("from"):
            comm["sender"] = {"display": md["from"]}
        if md.get("body"):
            comm["payload"] = [{"contentString": md["body"][:5000]}]
        results.append(("Communication", comm))

    return results


def convert_page_scrapes(pages):
    """Convert data from the page-by-page scraper (04)."""
    results = []
    for page in pages:
        url = page.get("url", "")
        title = page.get("title", "")

        # Store each page as a DocumentReference
        text_parts = [f"Page: {title}", f"URL: {url}"]

        for section in page.get("sections", []):
            text_parts.append(f"\n## {section['heading']}\n{section['content']}")

        for table in page.get("tables", []):
            headers = table.get("headers", [])
            text_parts.append(f"\nTable: {' | '.join(headers)}")
            for row in table.get("rows", []):
                if isinstance(row, dict):
                    text_parts.append(" | ".join(row.get("cells", [])))
                else:
                    text_parts.append(" | ".join(row))

        full_text = "\n".join(text_parts)
        if len(full_text) > 100:  # Skip near-empty pages
            import base64
            doc_ref = {
                "resourceType": "DocumentReference",
                "status": "current",
                "type": {"text": f"MyChart Page: {title[:100]}"},
                "meta": {"source": SOURCE_SYSTEM},
                "description": title[:200],
                "content": [{
                    "attachment": {
                        "contentType": "text/plain",
                        "data": base64.b64encode(full_text[:10000].encode()).decode("ascii"),
                        "title": title[:100],
                    }
                }],
            }
            results.append(("DocumentReference", doc_ref))

        # Also try to extract structured data from embedded JSON
        for ej in page.get("embeddedJson", []):
            if isinstance(ej, dict):
                # Check if it looks like FHIR data
                if ej.get("resourceType"):
                    results.append((ej["resourceType"], ej))
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 05_convert_and_load.py <scraped_json_file> [--fhir-base URL]")
        print("       python3 05_convert_and_load.py *.json  # Process multiple files")
        sys.exit(1)

    global FHIR_BASE
    files = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--fhir-base" and i + 1 < len(sys.argv):
            FHIR_BASE = sys.argv[i + 1]
            i += 2
        else:
            files.append(sys.argv[i])
            i += 1

    print(f"FHIR Base URL: {FHIR_BASE}")

    all_resources = []

    for filepath in files:
        print(f"\nProcessing: {filepath}")
        with open(filepath) as f:
            data = json.load(f)

        # Detect format and convert
        if isinstance(data, list):
            # Page scraper output (array of pages)
            resources = convert_page_scrapes(data)
            print(f"  Format: Page scrapes ({len(data)} pages)")
        elif data.get("resources"):
            # FHIR probe output
            resources = convert_fhir_extract(data)
            print(f"  Format: FHIR extract")
        elif data.get("source", "").endswith("(DOM)"):
            # DOM scraper output
            resources = convert_dom_extract(data)
            print(f"  Format: DOM extract")
        else:
            # Internal API output
            resources = convert_internal_api(data)
            print(f"  Format: Internal API extract")

        print(f"  Converted to {len(resources)} FHIR resources")
        all_resources.extend(resources)

    if not all_resources:
        print("\nNo resources to load.")
        sys.exit(0)

    # Summary by type
    type_counts = {}
    for rt, _ in all_resources:
        type_counts[rt] = type_counts.get(rt, 0) + 1
    print(f"\n=== Resources to load ===")
    for rt, count in sorted(type_counts.items()):
        print(f"  {rt}: {count}")
    print(f"  TOTAL: {len(all_resources)}")

    # Load into HAPI FHIR
    print(f"\nLoading into {FHIR_BASE}...")
    success = 0
    errors = 0
    for i, (rt, resource) in enumerate(all_resources):
        resource["resourceType"] = rt
        rid, status = fhir_post(rt, resource)
        if rid:
            success += 1
            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(all_resources)}] Loaded {success} resources...")
        else:
            errors += 1
            print(f"  [{i+1}] Failed to load {rt}")

    print(f"\n=== Loading Complete ===")
    print(f"  Success: {success}")
    print(f"  Errors: {errors}")
    print(f"  Total: {len(all_resources)}")


if __name__ == "__main__":
    main()
