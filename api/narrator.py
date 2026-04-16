"""PHV Narrator — Local AI actor that generates plain-language summaries of FHIR resources.

Runs against Ollama (local LLM on the host) to produce human-readable narratives
for new or existing health data. Summaries are stored back as FHIR DocumentReference
resources with full content attribution.

Provides:
  - POST /api/narrator/narrate     — narrate a single resource or batch
  - POST /api/narrator/narrate-new — narrate recently ingested resources
  - GET  /api/narrator/summaries   — list generated summaries
  - GET  /api/narrator/status      — check Ollama connectivity and model availability
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Request

logger = logging.getLogger("narrator")

router = APIRouter(prefix="/api/narrator", tags=["narrator"])

# ── Configuration ──────────────────────────────────────────────────────────
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral")
HAPI_BASE = os.environ.get("HAPI_BASE", "http://hapi:8080/fhir")

# Attribution metadata
NARRATOR_SYSTEM = "PHV-Narrator"
NARRATOR_VERSION = "1.0"

# ── HTTP Clients ──────────────────────────────────────────────────────────
_ollama_client: Optional[httpx.AsyncClient] = None
_fhir_client: Optional[httpx.AsyncClient] = None


async def get_ollama_client() -> httpx.AsyncClient:
    global _ollama_client
    if _ollama_client is None or _ollama_client.is_closed:
        _ollama_client = httpx.AsyncClient(base_url=OLLAMA_BASE, timeout=120.0)
    return _ollama_client


async def get_fhir_client() -> httpx.AsyncClient:
    global _fhir_client
    if _fhir_client is None or _fhir_client.is_closed:
        _fhir_client = httpx.AsyncClient(base_url=HAPI_BASE, timeout=30.0)
    return _fhir_client


# ── Ollama Integration ────────────────────────────────────────────────────

async def ollama_generate(prompt: str, system: str = "", temperature: float = 0.3) -> str:
    """Generate text from local Ollama model."""
    client = await get_ollama_client()
    response = await client.post("/api/generate", json={
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": 500,  # keep summaries concise
        },
    })
    response.raise_for_status()
    return response.json().get("response", "").strip()


async def check_ollama() -> dict:
    """Check Ollama connectivity and model availability."""
    try:
        client = await get_ollama_client()
        # Check if Ollama is reachable
        r = await client.get("/api/tags")
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        has_model = any(OLLAMA_MODEL in m for m in models)
        return {
            "connected": True,
            "models": models,
            "active_model": OLLAMA_MODEL,
            "model_available": has_model,
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
            "active_model": OLLAMA_MODEL,
            "model_available": False,
        }


# ── FHIR Resource Extraction ─────────────────────────────────────────────

def extract_resource_text(resource: dict) -> str:
    """Extract the most useful textual content from a FHIR resource for narration."""
    rtype = resource.get("resourceType", "")
    parts = [f"Resource type: {rtype}"]

    if rtype == "Observation":
        code = resource.get("code", {})
        display = code.get("text", "")
        if not display:
            codings = code.get("coding", [])
            display = next((c.get("display", "") for c in codings if c.get("display")), "")
        parts.append(f"Test: {display}")

        vq = resource.get("valueQuantity", {})
        if vq.get("value") is not None:
            parts.append(f"Value: {vq['value']} {vq.get('unit', '')}")

        vs = resource.get("valueString", "")
        if vs:
            parts.append(f"Value: {vs}")

        date = (resource.get("effectiveDateTime") or "")[:10]
        if date:
            parts.append(f"Date: {date}")

        ranges = resource.get("referenceRange", [])
        if ranges:
            lo = ranges[0].get("low", {}).get("value", "")
            hi = ranges[0].get("high", {}).get("value", "")
            unit = ranges[0].get("low", {}).get("unit", "")
            if lo or hi:
                parts.append(f"Reference range: {lo}-{hi} {unit}")

        interp = resource.get("interpretation", [])
        if interp:
            for i in interp:
                parts.append(f"Interpretation: {i.get('text', '')}")

    elif rtype == "DiagnosticReport":
        code = resource.get("code", {})
        display = code.get("text", "")
        if not display:
            codings = code.get("coding", [])
            display = next((c.get("display", "") for c in codings if c.get("display")), "")
        parts.append(f"Report: {display}")
        date = (resource.get("effectiveDateTime") or "")[:10]
        if date:
            parts.append(f"Date: {date}")
        conclusion = resource.get("conclusion", "")
        if conclusion:
            parts.append(f"Conclusion: {conclusion}")

    elif rtype == "Condition":
        code = resource.get("code", {})
        display = code.get("text", "")
        if not display:
            codings = code.get("coding", [])
            display = next((c.get("display", "") for c in codings if c.get("display")), "Unknown")
        parts.append(f"Condition: {display}")
        status = resource.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "")
        if status:
            parts.append(f"Status: {status}")
        recorded = (resource.get("recordedDate") or "")[:10]
        if recorded:
            parts.append(f"Recorded: {recorded}")

    elif rtype == "MedicationRequest":
        med = resource.get("medicationReference", {}).get("display", "")
        if not med:
            med = resource.get("medicationCodeableConcept", {}).get("text", "")
        if not med:
            for c in resource.get("contained", []):
                if c.get("resourceType") == "Medication":
                    med = c.get("code", {}).get("text", "")
                    break
        parts.append(f"Medication: {med}")
        status = resource.get("status", "")
        if status:
            parts.append(f"Status: {status}")
        authored = (resource.get("authoredOn") or "")[:10]
        if authored:
            parts.append(f"Ordered: {authored}")
        dosages = resource.get("dosageInstruction", [])
        if dosages:
            parts.append(f"Dosage: {dosages[0].get('text', '')}")
        requester = resource.get("requester", {}).get("display", "")
        if requester:
            parts.append(f"Prescriber: {requester}")

    elif rtype == "Encounter":
        etype = ""
        for t in resource.get("type", []):
            etype = t.get("text", "")
            break
        if etype:
            parts.append(f"Type: {etype}")
        period = resource.get("period", {})
        start = (period.get("start") or "")[:10]
        if start:
            parts.append(f"Date: {start}")
        for r in resource.get("reasonCode", []):
            reason = r.get("text", "")
            if reason:
                parts.append(f"Reason: {reason}")
                break

    elif rtype == "AllergyIntolerance":
        code = resource.get("code", {})
        display = code.get("text", "")
        if not display:
            codings = code.get("coding", [])
            display = next((c.get("display", "") for c in codings if c.get("display")), "")
        parts.append(f"Allergy: {display}")

    elif rtype == "Procedure":
        code = resource.get("code", {})
        display = code.get("text", "")
        if not display:
            codings = code.get("coding", [])
            display = next((c.get("display", "") for c in codings if c.get("display")), "")
        parts.append(f"Procedure: {display}")
        date = (resource.get("performedDateTime") or resource.get("performedPeriod", {}).get("start", "") or "")[:10]
        if date:
            parts.append(f"Date: {date}")

    return "\n".join(parts)


# ── Narration ─────────────────────────────────────────────────────────────

NARRATOR_SYSTEM_PROMPT = """You are a medical record narrator. Your job is to convert structured health data into a clear, concise, plain-language summary that a patient can easily understand.

Rules:
- Write 1-3 sentences, no more.
- Use plain language but keep medical terms where useful (with brief explanations).
- Include the key facts: what was measured/done, the result, the date, and whether it's normal.
- If a value is outside the reference range, note that clearly.
- Do not add medical advice or recommendations.
- Do not add disclaimers.
- Write in second person ("Your...").
"""


async def narrate_resource(resource: dict) -> str:
    """Generate a plain-language summary of a single FHIR resource."""
    resource_text = extract_resource_text(resource)
    prompt = f"Summarize this health record entry for the patient:\n\n{resource_text}"
    return await ollama_generate(prompt, system=NARRATOR_SYSTEM_PROMPT)


async def narrate_batch(resources: list[dict]) -> str:
    """Generate a combined summary for a batch of related resources."""
    all_text = []
    for r in resources[:20]:  # cap at 20 to avoid prompt overflow
        all_text.append(extract_resource_text(r))

    combined = "\n\n---\n\n".join(all_text)
    prompt = f"Summarize these {len(resources)} health record entries together in a cohesive paragraph for the patient:\n\n{combined}"
    return await ollama_generate(prompt, system=NARRATOR_SYSTEM_PROMPT, temperature=0.3)


# ── Store Summary as FHIR DocumentReference ───────────────────────────────

async def store_summary(
    summary: str,
    source_references: list[str],
    title: str = "AI Narrative Summary",
) -> dict:
    """Store a narrator summary as a FHIR DocumentReference with attribution."""
    now = datetime.now(timezone.utc).isoformat()

    doc_ref = {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "51855-5",
                "display": "Patient Note",
            }],
            "text": title,
        },
        "date": now,
        "description": title,
        "content": [{
            "attachment": {
                "contentType": "text/plain",
                "data": __import__("base64").b64encode(summary.encode()).decode(),
                "title": title,
            },
        }],
        # Attribution metadata
        "extension": [
            {
                "url": "http://phv.local/fhir/StructureDefinition/ai-attribution",
                "extension": [
                    {"url": "system", "valueString": NARRATOR_SYSTEM},
                    {"url": "version", "valueString": NARRATOR_VERSION},
                    {"url": "model", "valueString": OLLAMA_MODEL},
                    {"url": "generated-at", "valueDateTime": now},
                    {"url": "tier", "valueString": "tier-3"},
                    {"url": "confidence", "valueString": "generated"},
                ],
            },
        ],
        # Link back to source resources
        "context": {
            "related": [{"reference": ref} for ref in source_references],
        },
    }

    client = await get_fhir_client()
    r = await client.post("/DocumentReference", json=doc_ref)
    r.raise_for_status()
    return r.json()


# ── Routes ────────────────────────────────────────────────────────────────

@router.get("/status")
async def status():
    """Check Ollama connectivity and model status."""
    return await check_ollama()


@router.post("/narrate")
async def narrate(request: Request):
    """Narrate one or more FHIR resources.

    Expects JSON:
      { "resource_ids": ["Observation/123", ...] }
    or:
      { "resources": [ {full FHIR resource}, ... ] }
    or:
      { "resource_id": "Observation/123" }

    Returns the generated summary and the stored DocumentReference.
    """
    body = await request.json()

    # Check Ollama first
    ollama_status = await check_ollama()
    if not ollama_status["connected"] or not ollama_status["model_available"]:
        return {"error": "Ollama not available", "details": ollama_status}

    resources = body.get("resources", [])
    resource_ids = body.get("resource_ids", [])
    single_id = body.get("resource_id", "")
    if single_id:
        resource_ids = [single_id]

    # Fetch resources by ID if needed
    if resource_ids and not resources:
        client = await get_fhir_client()
        for rid in resource_ids:
            try:
                r = await client.get(f"/{rid}")
                r.raise_for_status()
                resources.append(r.json())
            except Exception as e:
                return {"error": f"Failed to fetch {rid}: {str(e)}"}

    if not resources:
        return {"error": "No resources provided"}

    # Generate summary
    start_time = time.time()
    if len(resources) == 1:
        summary = await narrate_resource(resources[0])
        title = f"Narrative: {resources[0].get('resourceType', 'Resource')}"
    else:
        summary = await narrate_batch(resources)
        title = f"Narrative: {len(resources)} resources"

    elapsed = round(time.time() - start_time, 2)

    # Build source references
    source_refs = []
    for r in resources:
        rtype = r.get("resourceType", "")
        rid = r.get("id", "")
        if rtype and rid:
            source_refs.append(f"{rtype}/{rid}")

    # Store as FHIR DocumentReference
    stored = await store_summary(summary, source_refs, title)

    return {
        "summary": summary,
        "model": OLLAMA_MODEL,
        "generation_time_seconds": elapsed,
        "source_resources": source_refs,
        "document_reference": {
            "id": stored.get("id"),
            "resourceType": "DocumentReference",
        },
    }


@router.post("/narrate-new")
async def narrate_new(request: Request):
    """Narrate recently ingested resources.

    Expects JSON:
      { "since": "2026-04-06", "resource_types": ["Observation", "MedicationRequest"] }

    Defaults to last 24 hours if 'since' not provided.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    since = body.get("since", "")
    rtypes = body.get("resource_types", ["Observation", "DiagnosticReport", "MedicationRequest", "Condition"])

    if not since:
        # Default: last 24 hours
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(f"narrate-new called: since={since}, types={rtypes}")

    client = await get_fhir_client()
    all_resources = []

    for rtype in rtypes:
        try:
            r = await client.get(f"/{rtype}", params={
                "_lastUpdated": f"ge{since}",
                "_count": "5",
                "_sort": "-_lastUpdated",
            })
            r.raise_for_status()
            bundle = r.json()
            entries = bundle.get("entry", [])
            logger.info(f"  {rtype}: found {len(entries)} resources")
            for entry in entries:
                all_resources.append(entry.get("resource", {}))
        except Exception as e:
            logger.error(f"  {rtype}: FHIR fetch error: {e}")

    if not all_resources:
        logger.info("No resources found to narrate")
        return {"message": "No new resources found since " + since, "count": 0}

    logger.info(f"Total resources to narrate: {len(all_resources)}")

    # Narrate in batches grouped by resource type
    results = []
    by_type = {}
    for r in all_resources:
        rt = r.get("resourceType", "Other")
        by_type.setdefault(rt, []).append(r)

    for rt, group in by_type.items():
        try:
            logger.info(f"  Narrating {len(group)} {rt} resources via Ollama...")
            start_time = time.time()
            if len(group) == 1:
                summary = await narrate_resource(group[0])
            else:
                summary = await narrate_batch(group)
            elapsed = round(time.time() - start_time, 2)
            logger.info(f"  {rt} narration done in {elapsed}s")

            source_refs = [f"{r.get('resourceType')}/{r.get('id')}" for r in group]
            stored = await store_summary(summary, source_refs, f"Narrative: {len(group)} new {rt} records")

            results.append({
                "resource_type": rt,
                "count": len(group),
                "summary": summary,
                "generation_time_seconds": elapsed,
                "document_reference_id": stored.get("id"),
            })
        except Exception as e:
            logger.error(f"  {rt} narration failed: {e}")
            results.append({
                "resource_type": rt,
                "count": len(group),
                "error": str(e),
            })

    return {"results": results, "total_resources": len(all_resources)}


@router.get("/summaries")
async def list_summaries(count: int = 20):
    """List recent narrator-generated summaries."""
    client = await get_fhir_client()
    try:
        r = await client.get("/DocumentReference", params={
            "type": "http://loinc.org|51855-5",
            "_count": str(count),
            "_sort": "-date",
        })
        r.raise_for_status()
        bundle = r.json()
        summaries = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            # Decode the summary text
            content = res.get("content", [{}])[0].get("attachment", {})
            text = ""
            if content.get("data"):
                import base64
                text = base64.b64decode(content["data"]).decode("utf-8", errors="replace")

            # Extract attribution
            attribution = {}
            for ext in res.get("extension", []):
                if "ai-attribution" in ext.get("url", ""):
                    for sub in ext.get("extension", []):
                        attribution[sub.get("url", "")] = sub.get("valueString") or sub.get("valueDateTime", "")

            summaries.append({
                "id": res.get("id"),
                "date": res.get("date", ""),
                "title": res.get("description", ""),
                "summary": text,
                "attribution": attribution,
                "source_resources": [r.get("reference", "") for r in res.get("context", {}).get("related", [])],
            })
        return {"summaries": summaries}
    except Exception as e:
        return {"error": str(e)}
