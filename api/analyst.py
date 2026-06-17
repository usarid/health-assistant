"""PHV Analyst — Cloud AI actor that performs deep pattern analysis on patient data.

Uses Claude (Sonnet) to detect trends, flag anomalies, compare results over time,
and generate clinical insights. Can be triggered on-demand or on a schedule.

Provides:
  - POST /api/analyst/analyze      — run a specific analysis
  - POST /api/analyst/full-review  — comprehensive review of recent data
  - GET  /api/analyst/findings     — list past findings
"""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic
import httpx
from fastapi import APIRouter, Request

# Import grouped medication list (single source of truth for patient meds)
from meds import get_grouped_medications

router = APIRouter(prefix="/api/analyst", tags=["analyst"])

# ── Configuration ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Use the bare alias (no date suffix) — aliases auto-resolve to the latest
# snapshot in the tier and don't expire on a calendar. Dated snapshot IDs
# (claude-sonnet-4-20250514, etc.) are deprecated and retire 6-12 months
# after release, returning 404 from that day onward. Stick to the alias.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
HAPI_BASE = os.environ.get("HAPI_BASE", "http://hapi:8080/fhir")

# Attribution metadata
ANALYST_SYSTEM = "PHV-Analyst"
ANALYST_VERSION = "1.0"

# ── HTTP Client ──────────────────────────────────────────────────────────
_fhir_client: Optional[httpx.AsyncClient] = None


async def get_fhir_client() -> httpx.AsyncClient:
    global _fhir_client
    if _fhir_client is None or _fhir_client.is_closed:
        _fhir_client = httpx.AsyncClient(base_url=HAPI_BASE, timeout=30.0)
    return _fhir_client


async def _fhir_get(path: str, params: dict = None) -> dict:
    client = await get_fhir_client()
    r = await client.get(f"/{path}", params=params or {})
    r.raise_for_status()
    return r.json()


# ── Data Gathering ────────────────────────────────────────────────────────

async def gather_lab_trends(months: int = 6, max_results: int = 200) -> list[dict]:
    """Gather lab observations from the last N months with values and ranges."""
    since = (datetime.now(timezone.utc) - timedelta(days=months * 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bundle = await _fhir_get("Observation", {
        "category": "laboratory",
        "date": f"ge{since}",
        "_count": str(max_results),
        "_sort": "-date",
    })
    results = []
    for entry in bundle.get("entry", []):
        r = entry.get("resource", {})
        code = r.get("code", {})
        display = code.get("text", "")
        if not display:
            codings = code.get("coding", [])
            display = next((c.get("display", "") for c in codings if c.get("display")), "")
        loinc = ""
        for c in code.get("coding", []):
            if "loinc" in c.get("system", ""):
                loinc = c.get("code", "")
                break
        vq = r.get("valueQuantity", {})
        value = vq.get("value")
        unit = vq.get("unit", "")
        date = (r.get("effectiveDateTime") or "")[:10]
        ref_lo, ref_hi = None, None
        ranges = r.get("referenceRange", [])
        if ranges:
            ref_lo = ranges[0].get("low", {}).get("value")
            ref_hi = ranges[0].get("high", {}).get("value")

        if value is not None and display:
            results.append({
                "test": display, "loinc": loinc, "value": value, "unit": unit,
                "date": date, "ref_low": ref_lo, "ref_high": ref_hi,
                "out_of_range": (ref_lo is not None and value < ref_lo) or (ref_hi is not None and value > ref_hi),
            })
    return results


async def gather_active_conditions() -> list[dict]:
    """Get active conditions."""
    bundle = await _fhir_get("Condition", {
        "clinical-status": "active",
        "_count": "50",
    })
    results = []
    for entry in bundle.get("entry", []):
        r = entry.get("resource", {})
        code = r.get("code", {})
        display = code.get("text", "")
        if not display:
            codings = code.get("coding", [])
            display = next((c.get("display", "") for c in codings if c.get("display")), "Unknown")
        recorded = (r.get("recordedDate") or "")[:10]
        results.append({"condition": display, "recorded": recorded})
    return results



async def gather_medications() -> list[dict]:
    """Get the grouped, deduplicated medication list with user overrides applied.

    Uses the same logic as the Meds tab UI, which handles normalization,
    alias resolution, deduplication, and user overrides. This ensures the
    AI analyst sees the same clean medication list the patient sees."""
    grouped = await get_grouped_medications()

    # Simplify for the AI context — just the fields it needs
    results = []
    for med in grouped:
        dosage = med.get("user_dosage") or med.get("dosage") or ""
        frequency = med.get("user_frequency") or med.get("frequency") or ""
        entry = {
            "medication": med["display"],
            "normalized_name": med["key"],
            "status": med["effective_status"],
            "last_ordered": med.get("most_recent_order", ""),
            "prescriber": med.get("prescriber", ""),
            "record_count": med.get("record_count", 1),
        }
        if dosage:
            entry["dosage"] = dosage
        if frequency:
            entry["frequency"] = frequency
        if med.get("user_override"):
            entry["patient_confirmed_status"] = med["user_override"]
        if med.get("user_notes"):
            entry["patient_notes"] = med["user_notes"]
        results.append(entry)

    return results


# ── Analysis Types ────────────────────────────────────────────────────────

ANALYSIS_TYPES = {
    "trend_analysis": {
        "name": "Lab Trend Analysis",
        "description": "Analyze trends in lab values over time, flag concerning directions",
        "system_prompt": """You are a clinical data analyst reviewing a patient's lab trends.

Your task:
1. Identify significant trends (improving, worsening, stable) for each lab value
2. Flag any values that are out of reference range or trending toward out-of-range
3. Note any correlations between different lab values
4. Highlight clinically significant changes (not just statistical noise)

Format your response as a structured analysis with sections for:
- Key Findings (most important observations first)
- Trends (organized by body system or clinical relevance)
- Values Requiring Attention (out of range or trending concerning)

Be precise with numbers and dates. Do not add disclaimers or caveats.""",
    },
    "medication_review": {
        "name": "Medication Review",
        "description": "Review current medications in context of conditions and lab results",
        "system_prompt": """You are a clinical pharmacist reviewing a patient's medication regimen.

IMPORTANT: The medication list you receive has ALREADY been deduplicated and grouped. Multiple FHIR
records for the same medication are normal (prescription renewals, data imports) and have been consolidated.
Do NOT report duplicate records, conflicting FHIR statuses, or "medication reconciliation issues" — these
are artifacts of how EHR systems store data and have already been resolved. Focus on clinically meaningful
observations, not data quality issues.

Your task:
1. List current medications (status "taking" or "active") with their purposes relative to active conditions
2. Note any lab values that might be relevant to medication monitoring (e.g., kidney function for certain drugs)
3. Identify any potential interactions or concerns worth discussing with the physician
4. Note medications where dosage or frequency information is missing

Keep your tone helpful and informative, not alarmist. Present findings as useful context for the patient,
not as urgent warnings. Format your response as a structured review. Be specific with dates and values.
Do not add disclaimers.""",
    },
    "pre_appointment": {
        "name": "Pre-Appointment Briefing",
        "description": "Prepare a briefing for an upcoming doctor's appointment",
        "system_prompt": """You are preparing a patient briefing for an upcoming doctor's appointment.

Your task:
1. Summarize the patient's current status (conditions, medications, recent trends)
2. List the most important topics to discuss
3. Highlight any concerning trends or values that need attention
4. Suggest specific questions the patient might want to ask
5. Note any changes since the last likely appointment

Format as a concise, actionable briefing. Be specific with data points. Do not add disclaimers.""",
    },
    "comprehensive": {
        "name": "Comprehensive Health Review",
        "description": "Full review of all available data for overall health assessment",
        "system_prompt": """You are conducting a comprehensive review of a patient's health data.

IMPORTANT: The medication list has already been deduplicated and grouped. Do NOT report duplicate records
or "medication reconciliation issues" — these are normal EHR data artifacts that have been resolved.
Focus on clinically meaningful observations.

Your task:
1. Provide an overall health status assessment based on available data
2. Organize findings by body system or clinical domain
3. Identify the most clinically significant issues
4. Note any gaps in monitoring or data
5. Highlight positive trends and areas of improvement
6. Flag anything that warrants prompt medical attention

Keep your tone helpful and informative, not alarmist. Format as a thorough but readable clinical summary.
Use specific numbers and dates throughout. Do not add disclaimers.""",
    },
}


async def run_analysis(analysis_type: str, months: int = 6) -> dict:
    """Run a specific type of analysis."""
    if analysis_type not in ANALYSIS_TYPES:
        return {"error": f"Unknown analysis type: {analysis_type}. Available: {list(ANALYSIS_TYPES.keys())}"}

    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not configured"}

    config = ANALYSIS_TYPES[analysis_type]

    # Gather data
    labs, conditions, meds = await asyncio.gather(
        gather_lab_trends(months=months),
        gather_active_conditions(),
        gather_medications(),
    )

    # Build the data context
    data_context = f"""Patient Data Summary (last {months} months):

Active Conditions ({len(conditions)}):
{json.dumps(conditions, indent=2)}

Medications ({len(meds)} unique, grouped and deduplicated):
This is the patient's consolidated medication list. Each entry represents one unique medication
after grouping duplicate FHIR records. The "status" field reflects the patient's current
relationship with this medication:
- "taking" or "active" = currently taking
- "not_taking" = confirmed not taking / discontinued
- "as_needed" = taking as needed
- "stopped"/"completed"/"cancelled" = historical, no longer active
If "patient_confirmed_status" is present, the patient has explicitly confirmed this status.
Multiple FHIR records for the same medication (shown in "record_count") are NORMAL and do NOT
indicate discrepancies — they simply reflect prescription renewals or data imports over time.
Do NOT flag duplicate records or conflicting FHIR statuses as problems — this has already been
resolved by the medication grouping system.
{json.dumps(meds, indent=2)}

Lab Results ({len(labs)} values):
{json.dumps(labs, indent=2)}
"""

    # Call Claude
    start_time = time.time()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=config["system_prompt"],
        messages=[{"role": "user", "content": f"Please analyze this patient data:\n\n{data_context}"}],
    )

    analysis_text = response.content[0].text
    elapsed = round(time.time() - start_time, 2)

    # Store as FHIR DocumentReference with attribution
    now = datetime.now(timezone.utc).isoformat()
    title = f"Analyst: {config['name']}"

    doc_ref = {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "51848-0",
                "display": "Assessment note",
            }],
            "text": title,
        },
        "date": now,
        "description": title,
        "content": [{
            "attachment": {
                "contentType": "text/plain",
                "data": __import__("base64").b64encode(analysis_text.encode()).decode(),
                "title": title,
            },
        }],
        "extension": [
            {
                "url": "http://phv.local/fhir/StructureDefinition/ai-attribution",
                "extension": [
                    {"url": "system", "valueString": ANALYST_SYSTEM},
                    {"url": "version", "valueString": ANALYST_VERSION},
                    {"url": "model", "valueString": ANTHROPIC_MODEL},
                    {"url": "generated-at", "valueDateTime": now},
                    {"url": "tier", "valueString": "tier-3"},
                    {"url": "confidence", "valueString": "analysis"},
                    {"url": "analysis-type", "valueString": analysis_type},
                    {"url": "data-window-months", "valueString": str(months)},
                ],
            },
        ],
    }

    fhir_client = await get_fhir_client()
    r = await fhir_client.post("/DocumentReference", json=doc_ref)
    r.raise_for_status()
    stored = r.json()

    return {
        "analysis_type": analysis_type,
        "analysis_name": config["name"],
        "analysis": analysis_text,
        "model": ANTHROPIC_MODEL,
        "generation_time_seconds": elapsed,
        "data_summary": {
            "conditions": len(conditions),
            "medications": len(meds),
            "lab_results": len(labs),
            "months_analyzed": months,
        },
        "document_reference": {
            "id": stored.get("id"),
            "resourceType": "DocumentReference",
        },
    }


# ── Routes ────────────────────────────────────────────────────────────────

@router.post("/analyze")
async def analyze(request: Request):
    """Run a specific analysis.

    Expects JSON:
      { "type": "trend_analysis|medication_review|pre_appointment|comprehensive", "months": 6 }
    """
    body = await request.json()
    analysis_type = body.get("type", "trend_analysis")
    months = body.get("months", 6)
    return await run_analysis(analysis_type, months=months)


@router.post("/full-review")
async def full_review(request: Request):
    """Run a comprehensive review (convenience endpoint)."""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    months = body.get("months", 6)
    return await run_analysis("comprehensive", months=months)


@router.get("/types")
async def list_analysis_types():
    """List available analysis types."""
    return {
        name: {"name": config["name"], "description": config["description"]}
        for name, config in ANALYSIS_TYPES.items()
    }


@router.get("/findings")
async def list_findings(count: int = 20):
    """List past analyst findings."""
    client = await get_fhir_client()
    try:
        r = await client.get("/DocumentReference", params={
            "type": "http://loinc.org|51848-0",
            "_count": str(count),
            "_sort": "-date",
        })
        r.raise_for_status()
        bundle = r.json()
        findings = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            content = res.get("content", [{}])[0].get("attachment", {})
            text = ""
            if content.get("data"):
                import base64
                text = base64.b64decode(content["data"]).decode("utf-8", errors="replace")

            attribution = {}
            for ext in res.get("extension", []):
                if "ai-attribution" in ext.get("url", ""):
                    for sub in ext.get("extension", []):
                        attribution[sub.get("url", "")] = sub.get("valueString") or sub.get("valueDateTime", "")

            findings.append({
                "id": res.get("id"),
                "date": res.get("date", ""),
                "title": res.get("description", ""),
                "analysis": text[:500] + ("..." if len(text) > 500 else ""),
                "full_analysis": text,
                "attribution": attribution,
            })
        return {"findings": findings}
    except Exception as e:
        return {"error": str(e)}
