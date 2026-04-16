#!/usr/bin/env python3
"""
phv_search.py — Personal Health Vault search utility.

Query your FHIR health records from the command line.

Usage:
    # Full-text search
    python3 phv_search.py "bone lesion"
    python3 phv_search.py "teclistamab"
    python3 phv_search.py "enterography"

    # Search specific resource types
    python3 phv_search.py --type Observation "creatinine"
    python3 phv_search.py --type DiagnosticReport "pathology"
    python3 phv_search.py --type Condition

    # Search by LOINC code
    python3 phv_search.py --loinc 2093-3          # Total cholesterol
    python3 phv_search.py --loinc 718-7            # Hemoglobin

    # Search by date range
    python3 phv_search.py --date 2025              # All of 2025
    python3 phv_search.py --date 2025-06           # June 2025
    python3 phv_search.py --after 2025-01-01 --before 2025-12-31

    # Search by source institution
    python3 phv_search.py --source MSKCC
    python3 phv_search.py --source UCSF --type Observation

    # Combine filters
    python3 phv_search.py --type Observation --date 2025 --source MSKCC "hemoglobin"

    # Output formats
    python3 phv_search.py --format table "creatinine"    # Default: summary table
    python3 phv_search.py --format json "creatinine"     # Full FHIR JSON
    python3 phv_search.py --format timeline "hemoglobin"  # Date-sorted timeline

    # Limit results
    python3 phv_search.py --count 10 "observation"

    # List all resource types and counts
    python3 phv_search.py --stats

Requires: requests (pip install requests)
"""

import argparse
import json
import sys
import textwrap
from datetime import datetime
from urllib.parse import urlencode, quote

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests",
                           "--break-system-packages", "-q"])
    import requests


FHIR_BASE = "http://localhost:8080/fhir"


def _apply_base_url(url):
    global FHIR_BASE
    FHIR_BASE = url

RESOURCE_TYPES = [
    "Patient", "Observation", "DiagnosticReport", "MedicationRequest",
    "DocumentReference", "Condition", "Procedure", "Encounter",
    "Immunization", "AllergyIntolerance", "Binary", "CarePlan",
    "MedicationStatement", "ImagingStudy", "Practitioner",
    "Organization", "Location", "Medication", "Device"
]

# Common LOINC codes for quick reference
COMMON_LOINC = {
    "hemoglobin": "718-7",
    "hgb": "718-7",
    "wbc": "6690-2",
    "platelets": "777-3",
    "plt": "777-3",
    "creatinine": "2160-0",
    "egfr": "33914-3",
    "glucose": "2345-7",
    "a1c": "4548-4",
    "hba1c": "4548-4",
    "cholesterol": "2093-3",
    "ldl": "2089-1",
    "hdl": "2085-9",
    "triglycerides": "2571-8",
    "alt": "1742-6",
    "ast": "1920-8",
    "albumin": "1751-7",
    "bilirubin": "1975-2",
    "sodium": "2951-2",
    "potassium": "2823-3",
    "calcium": "17861-6",
    "tsh": "3016-3",
    "bp-systolic": "8480-6",
    "bp-diastolic": "8462-4",
    "heart-rate": "8867-4",
    "hr": "8867-4",
    "spo2": "2708-6",
    "bmi": "39156-5",
    "weight": "29463-7",
    "iga": "2458-8",
    "igg": "2462-0",
    "igm": "2472-9",
    "crp": "1988-5",
    "esr": "4537-7",
    "ferritin": "2276-4",
    "iron": "2498-4",
    "b12": "2132-9",
    "folate": "2284-8",
    "psa": "2857-1",
    "kappa-free": "11050-2",
    "lambda-free": "11051-0",
    "m-spike": "51435-6",
    "beta2-microglobulin": "1952-1",
    "ldh": "2532-0",
    "uric-acid": "3084-1",
    "phosphorus": "2777-1",
    "magnesium": "2601-3",
    "protein-total": "2885-2",
}


# ─── FHIR Queries ──────────────────────────────────────────────────────────

def fhir_get(path, params=None, count=100):
    """Make a FHIR GET request and return all matching resources."""
    if params is None:
        params = {}
    params["_count"] = str(count)

    url = f"{FHIR_BASE}/{path}"
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        bundle = r.json()
        entries = bundle.get("entry", [])
        total = bundle.get("total", len(entries))
        resources = [e.get("resource", {}) for e in entries]
        return resources, total
    except requests.exceptions.ConnectionError:
        print("Error: Cannot connect to HAPI FHIR at", FHIR_BASE)
        print("Is the server running? Start it with: docker compose up -d")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        return [], 0


def search_fulltext(query, resource_type=None, count=100):
    """Full-text search using HAPI's _content parameter."""
    rt = resource_type or "Resource"
    params = {"_content": query}
    return fhir_get(rt, params, count)


def search_by_loinc(loinc_code, date_from=None, date_to=None, count=100):
    """Search observations by LOINC code."""
    params = {"code": f"http://loinc.org|{loinc_code}"}
    if date_from:
        params["date"] = [f"ge{date_from}"]
    if date_to:
        params.setdefault("date", [])
        if isinstance(params["date"], list):
            params["date"].append(f"le{date_to}")
        else:
            params["date"] = [params["date"], f"le{date_to}"]
    return fhir_get("Observation", params, count)


def search_by_date(resource_type, date_from=None, date_to=None, count=100):
    """Search resources by date range."""
    params = {}
    if date_from:
        params["date"] = f"ge{date_from}"
    if date_to:
        if "date" in params:
            params["date"] = [params["date"], f"le{date_to}"]
        else:
            params["date"] = f"le{date_to}"
    return fhir_get(resource_type, params, count)


def get_stats():
    """Get resource counts for all types."""
    stats = {}
    for rt in RESOURCE_TYPES:
        try:
            r = requests.get(f"{FHIR_BASE}/{rt}",
                             params={"_summary": "count"}, timeout=10)
            if r.status_code == 200:
                total = r.json().get("total", 0)
                if total > 0:
                    stats[rt] = total
        except Exception:
            pass
    return stats


# ─── Formatting ─────────────────────────────────────────────────────────────

def extract_display_info(resource):
    """Extract human-readable info from a FHIR resource."""
    rt = resource.get("resourceType", "Unknown")
    info = {"type": rt, "id": resource.get("id", "")[:20]}

    # Date
    for date_field in ["effectiveDateTime", "issued", "recordedDate",
                       "authoredOn", "date", "performedDateTime", "occurrenceDateTime"]:
        if date_field in resource:
            val = resource[date_field]
            info["date"] = val[:10] if isinstance(val, str) else str(val)
            break
    if "effectivePeriod" in resource:
        period = resource["effectivePeriod"]
        info["date"] = period.get("start", "")[:10]

    # Display text
    if rt == "Observation":
        code_text = resource.get("code", {}).get("text", "")
        codings = resource.get("code", {}).get("coding", [])
        if not code_text and codings:
            code_text = codings[0].get("display", codings[0].get("code", ""))
        info["name"] = code_text

        # Value
        if "valueQuantity" in resource:
            vq = resource["valueQuantity"]
            info["value"] = f"{vq.get('value', '')} {vq.get('unit', '')}"
        elif "valueString" in resource:
            vs = resource["valueString"]
            info["value"] = vs[:150] + "..." if len(vs) > 150 else vs
        elif "valueCodeableConcept" in resource:
            info["value"] = resource["valueCodeableConcept"].get("text", "")

    elif rt == "DiagnosticReport":
        info["name"] = resource.get("code", {}).get("text", "Diagnostic Report")
        info["value"] = resource.get("conclusion", "")[:150]

    elif rt == "Condition":
        info["name"] = resource.get("code", {}).get("text", "Condition")
        info["value"] = resource.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "")

    elif rt == "MedicationRequest":
        med = resource.get("medicationCodeableConcept", {})
        info["name"] = med.get("text", "Medication")
        info["value"] = resource.get("status", "")

    elif rt == "Procedure":
        info["name"] = resource.get("code", {}).get("text", "Procedure")
        info["value"] = resource.get("status", "")

    elif rt == "DocumentReference":
        info["name"] = resource.get("type", {}).get("text",
                        resource.get("description", "Document"))
        info["value"] = resource.get("status", "")

    elif rt == "Immunization":
        info["name"] = resource.get("vaccineCode", {}).get("text", "Immunization")
        info["value"] = resource.get("status", "")

    else:
        info["name"] = resource.get("code", {}).get("text",
                        resource.get("name", str(rt)))
        info["value"] = ""

    # Source institution
    for ext in resource.get("extension", []):
        if "source-institution" in ext.get("url", ""):
            info["source"] = ext.get("valueString", "")
            break

    return info


def format_table(resources, total):
    """Format resources as a summary table."""
    if not resources:
        print("No results found.")
        return

    print(f"\n{'─'*100}")
    print(f"  Found {total} results (showing {len(resources)})")
    print(f"{'─'*100}")

    # Header
    print(f"  {'Date':<12} {'Type':<20} {'Name':<30} {'Value':<30} {'Source':<10}")
    print(f"  {'─'*10}   {'─'*18}   {'─'*28}   {'─'*28}   {'─'*8}")

    for r in resources:
        info = extract_display_info(r)
        date = info.get("date", "")[:10]
        rtype = info.get("type", "")[:18]
        name = info.get("name", "")[:28]
        value = info.get("value", "")[:28]
        source = info.get("source", "")[:10]
        print(f"  {date:<12} {rtype:<20} {name:<30} {value:<30} {source:<10}")

    print(f"{'─'*100}\n")


def format_timeline(resources, total):
    """Format resources as a chronological timeline."""
    if not resources:
        print("No results found.")
        return

    # Sort by date
    dated = []
    for r in resources:
        info = extract_display_info(r)
        date = info.get("date", "9999-99-99")
        dated.append((date, info, r))
    dated.sort(key=lambda x: x[0])

    print(f"\n{'━'*80}")
    print(f"  Timeline: {total} results (showing {len(resources)})")
    print(f"{'━'*80}")

    current_year = ""
    current_month = ""
    for date, info, r in dated:
        year = date[:4] if len(date) >= 4 else "????"
        month = date[:7] if len(date) >= 7 else "????"

        if year != current_year:
            current_year = year
            print(f"\n  ┏━━ {year} {'━'*70}")

        if month != current_month:
            current_month = month
            month_name = ""
            try:
                month_name = datetime.strptime(month, "%Y-%m").strftime("%B")
            except Exception:
                month_name = month
            print(f"  ┃")
            print(f"  ┣━ {month_name}")

        name = info.get("name", "")[:40]
        value = info.get("value", "")[:35]
        rtype = info.get("type", "")
        source = info.get("source", "")

        line = f"  ┃   {date:<12} [{rtype}] {name}"
        if value:
            line += f" = {value}"
        if source:
            line += f"  ({source})"
        print(line)

    print(f"  ┗{'━'*79}\n")


def format_json(resources, total):
    """Output full FHIR JSON."""
    output = {
        "total": total,
        "count": len(resources),
        "resources": resources
    }
    print(json.dumps(output, indent=2))


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Search your Personal Health Vault",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              phv_search.py "bone lesion"
              phv_search.py --type Observation "hemoglobin"
              phv_search.py --loinc creatinine --format timeline
              phv_search.py --date 2025 --source MSKCC
              phv_search.py --stats
        """)
    )

    parser.add_argument("query", nargs="?", default=None,
                        help="Full-text search query")
    parser.add_argument("--type", "-t", dest="resource_type",
                        help="FHIR resource type (e.g., Observation, Condition)")
    parser.add_argument("--loinc", "-l",
                        help="LOINC code or common name (e.g., 718-7, hemoglobin)")
    parser.add_argument("--date", "-d",
                        help="Date or year (e.g., 2025, 2025-06)")
    parser.add_argument("--after",
                        help="Results after this date (YYYY-MM-DD)")
    parser.add_argument("--before",
                        help="Results before this date (YYYY-MM-DD)")
    parser.add_argument("--source", "-s",
                        help="Source institution (e.g., MSKCC, UCSF)")
    parser.add_argument("--format", "-f", dest="output_format",
                        choices=["table", "json", "timeline"],
                        default="table",
                        help="Output format (default: table)")
    parser.add_argument("--count", "-n", type=int, default=100,
                        help="Max results to return (default: 100)")
    parser.add_argument("--stats", action="store_true",
                        help="Show resource counts and exit")
    parser.add_argument("--base-url", default=FHIR_BASE,
                        help=f"FHIR server URL (default: {FHIR_BASE})")

    args = parser.parse_args()

    _apply_base_url(args.base_url)

    # Stats mode
    if args.stats:
        print("\nPersonal Health Vault — Resource Summary")
        print("─" * 45)
        stats = get_stats()
        total = 0
        for rt, count in sorted(stats.items(), key=lambda x: -x[1]):
            print(f"  {rt:<25} {count:>6}")
            total += count
        print("─" * 45)
        print(f"  {'TOTAL':<25} {total:>6}")
        return

    # Resolve LOINC shorthand
    if args.loinc:
        loinc_code = COMMON_LOINC.get(args.loinc.lower(), args.loinc)

    # Parse date shortcuts
    date_from = args.after
    date_to = args.before
    if args.date:
        if len(args.date) == 4:  # Year only
            date_from = f"{args.date}-01-01"
            date_to = f"{args.date}-12-31"
        elif len(args.date) == 7:  # Year-month
            date_from = f"{args.date}-01"
            # Approximate end of month
            date_to = f"{args.date}-31"
        else:
            date_from = args.date
            date_to = args.date

    # Determine search strategy
    resources = []
    total = 0

    if args.loinc:
        resources, total = search_by_loinc(loinc_code, date_from, date_to,
                                            args.count)
    elif args.query:
        resources, total = search_fulltext(
            args.query,
            resource_type=args.resource_type,
            count=args.count
        )
    elif args.resource_type:
        params = {}
        if date_from:
            params["date"] = f"ge{date_from}"
        if date_to:
            d = params.get("date")
            if d:
                params["date"] = [d, f"le{date_to}"]
            else:
                params["date"] = f"le{date_to}"
        resources, total = fhir_get(args.resource_type, params, args.count)
    elif date_from or date_to:
        # Search observations by date if no type specified
        resources, total = search_by_date(
            "Observation", date_from, date_to, args.count
        )
    else:
        parser.print_help()
        return

    # Filter by source if specified (client-side since HAPI doesn't index extensions)
    if args.source and resources:
        filtered = []
        for r in resources:
            for ext in r.get("extension", []):
                if ("source-institution" in ext.get("url", "") and
                    args.source.lower() in ext.get("valueString", "").lower()):
                    filtered.append(r)
                    break
        resources = filtered
        total = len(resources)

    # Format output
    formatters = {
        "table": format_table,
        "json": format_json,
        "timeline": format_timeline,
    }
    formatters[args.output_format](resources, total)


if __name__ == "__main__":
    main()
