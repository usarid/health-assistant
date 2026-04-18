"""PHV AI Assistant — Chat endpoint with streaming, context assembly, and chat history.

Provides:
  - POST /api/assistant/chat   — stream a response (SSE)
  - GET  /api/assistant/threads — list conversation threads
  - GET  /api/assistant/thread/{id} — get messages for a thread
  - DELETE /api/assistant/thread/{id} — delete a thread
"""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import aiosqlite
import anthropic
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

# Analyst integration — import the run_analysis function for tool use
from analyst import run_analysis as analyst_run_analysis, ANALYSIS_TYPES
# Meds integration — import grouped medication list for context assembly
from meds import get_grouped_medications

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

# ── Configuration ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
HAPI_BASE = os.environ.get("HAPI_BASE", "http://hapi:8080/fhir")
DB_PATH = os.environ.get("ASSISTANT_DB", "/data/chat.db")

# ── Database ───────────────────────────────────────────────────────────────
_db: Optional[aiosqlite.Connection] = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.executescript("""
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New conversation',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                model TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        await _db.commit()
    return _db


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


# ── FHIR Context Assembly ─────────────────────────────────────────────────
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


async def _fhir_post(path: str, body: dict) -> dict:
    """POST a resource to FHIR."""
    client = await get_fhir_client()
    r = await client.post(f"/{path}", json=body)
    r.raise_for_status()
    return r.json()


async def _get_patient_reference() -> dict:
    """Get the patient subject reference from existing FHIR data."""
    try:
        bundle = await _fhir_get("MedicationRequest", {"_count": "1"})
        entries = bundle.get("entry", [])
        if entries:
            ref = entries[0].get("resource", {}).get("subject")
            if ref:
                return ref
    except Exception:
        pass
    return {"reference": "Patient/1"}  # fallback


# ── Patient-entered FHIR tagging ─────────────────────────────────────────

PHV_TAG_SYSTEM = "https://phv.local/tags"
PHV_TAG_PATIENT_ENTERED = {
    "system": PHV_TAG_SYSTEM,
    "code": "patient-entered",
    "display": "Patient-entered data",
}


def _tag_as_patient_entered(resource: dict) -> dict:
    """Add patient-entered meta tag to a FHIR resource."""
    if "meta" not in resource:
        resource["meta"] = {}
    if "tag" not in resource["meta"]:
        resource["meta"]["tag"] = []
    resource["meta"]["tag"].append(PHV_TAG_PATIENT_ENTERED)
    return resource


# ── Action execution — write to FHIR after user confirmation ─────────────

async def execute_proposed_action(action: dict) -> dict:
    """Execute a user-confirmed proposed action by writing to FHIR.

    Supported action types:
      - add_observation: Add a patient-reported observation (vitals, measurements)
      - update_medication_status: Change a medication's status
      - add_medication: Add a new medication the patient is taking
      - add_note: Add a free-text clinical note / annotation
    """
    action_type = action.get("action_type", "")
    params = action.get("params", {})
    patient_ref = await _get_patient_reference()
    now = datetime.now(timezone.utc).isoformat()

    try:
        if action_type == "add_observation":
            # Home vitals, measurements, patient-reported values
            resource = _tag_as_patient_entered({
                "resourceType": "Observation",
                "status": "final",
                "category": [{
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": params.get("category", "vital-signs"),
                        "display": params.get("category_display", "Vital Signs"),
                    }]
                }],
                "code": {
                    "text": params.get("name", ""),
                    **({"coding": [params["coding"]]} if params.get("coding") else {}),
                },
                "subject": patient_ref,
                "performer": [patient_ref],  # patient is the performer
                "effectiveDateTime": params.get("date", now[:10]),
                "issued": now,
            })
            # Value: could be quantity, string, or component (e.g., BP has systolic+diastolic)
            if params.get("components"):
                resource["component"] = params["components"]
            elif params.get("value") is not None and params.get("unit"):
                resource["valueQuantity"] = {
                    "value": float(params["value"]),
                    "unit": params["unit"],
                    "system": "http://unitsofmeasure.org",
                }
            elif params.get("value_text"):
                resource["valueString"] = params["value_text"]

            if params.get("note"):
                resource["note"] = [{"text": params["note"]}]

            result = await _fhir_post("Observation", resource)
            return {"ok": True, "resource_type": "Observation", "id": result.get("id", ""), "action_type": action_type}

        elif action_type == "update_medication_status":
            # Use the existing meds.py override system
            from meds import get_db as get_meds_db, normalize_med_name, get_aliases, resolve_key
            med_key = params.get("med_key", "")
            new_status = params.get("status", "")
            display_name = params.get("display_name", "")
            if not med_key or not new_status:
                return {"error": "med_key and status are required"}
            # Normalize the key so it matches the meds grouping logic
            aliases = await get_aliases()
            med_key = resolve_key(normalize_med_name(med_key), aliases)
            db = await get_meds_db()
            await db.execute(
                """INSERT INTO med_overrides (med_key, display_name, status, notes, user_dosage, user_frequency, updated_at)
                   VALUES (?, ?, ?, ?, '', '', ?)
                   ON CONFLICT(med_key) DO UPDATE SET
                     status = excluded.status,
                     notes = excluded.notes,
                     updated_at = excluded.updated_at""",
                (med_key, display_name, new_status, params.get("notes", ""), now),
            )
            await db.commit()
            return {"ok": True, "resource_type": "MedicationOverride", "med_key": med_key, "status": new_status, "action_type": action_type}

        elif action_type == "add_medication":
            # Create a MedicationRequest with patient-entered tag
            med_name = params.get("name", "")
            if not med_name:
                return {"error": "Medication name is required"}
            resource = _tag_as_patient_entered({
                "resourceType": "MedicationRequest",
                "status": "active",
                "intent": "order",
                "reportedBoolean": True,  # FHIR flag: patient-reported
                "medicationCodeableConcept": {"text": med_name},
                "subject": patient_ref,
                "authoredOn": params.get("date", now[:10]),
            })
            if params.get("dosage"):
                resource["dosageInstruction"] = [{"text": params["dosage"]}]
            if params.get("note"):
                resource["note"] = [{"text": params["note"]}]
            result = await _fhir_post("MedicationRequest", resource)
            return {"ok": True, "resource_type": "MedicationRequest", "id": result.get("id", ""), "action_type": action_type}

        elif action_type == "add_note":
            # Store as a DocumentReference or Basic annotation
            resource = _tag_as_patient_entered({
                "resourceType": "DocumentReference",
                "status": "current",
                "type": {"text": params.get("type", "Patient Note")},
                "subject": patient_ref,
                "date": now,
                "description": params.get("title", "Patient note"),
                "content": [{
                    "attachment": {
                        "contentType": "text/plain",
                        "data": __import__("base64").b64encode(params.get("text", "").encode()).decode(),
                    }
                }],
            })
            result = await _fhir_post("DocumentReference", resource)
            return {"ok": True, "resource_type": "DocumentReference", "id": result.get("id", ""), "action_type": action_type}

        elif action_type == "create_reminder":
            # Create a reminder via the reminders API (same DB)
            import aiosqlite, json as _json
            reminder_db_path = os.environ.get("ASSISTANT_DB", "/data/chat.db")
            db = await aiosqlite.connect(reminder_db_path)
            db.row_factory = aiosqlite.Row
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    context TEXT DEFAULT '',
                    due_at TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    priority TEXT DEFAULT 'normal',
                    linked_med TEXT DEFAULT '',
                    linked_condition TEXT DEFAULT '',
                    linked_resource_id TEXT DEFAULT '',
                    actions TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT '',
                    source TEXT DEFAULT 'assistant'
                );
            """)
            actions_json = _json.dumps(params.get("actions", []))
            cursor = await db.execute(
                """INSERT INTO reminders (message, context, due_at, priority,
                       linked_med, linked_condition, linked_resource_id, actions,
                       created_at, updated_at, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'assistant')""",
                (
                    params.get("message", ""),
                    params.get("context", ""),
                    params.get("due_at", ""),
                    params.get("priority", "normal"),
                    params.get("linked_med", ""),
                    params.get("linked_condition", ""),
                    params.get("linked_resource_id", ""),
                    actions_json,
                    now, now,
                ),
            )
            await db.commit()
            rid = cursor.lastrowid
            await db.close()
            return {"ok": True, "reminder_id": rid, "action_type": action_type}

        else:
            return {"error": f"Unknown action type: {action_type}"}

    except Exception as e:
        return {"error": str(e)}


async def _count_resource(rtype: str) -> int:
    try:
        bundle = await _fhir_get(rtype, {"_summary": "count"})
        return bundle.get("total", 0)
    except Exception:
        return 0


async def gather_patient_context() -> str:
    """Assemble a concise patient context summary from FHIR data for the system prompt."""
    sections = []

    # 1. Resource counts
    rtypes = ["Observation", "Condition", "MedicationRequest", "Procedure",
              "DiagnosticReport", "Encounter", "Immunization", "AllergyIntolerance"]
    counts = await asyncio.gather(*[_count_resource(rt) for rt in rtypes])
    counts_str = ", ".join(f"{rt}: {c}" for rt, c in zip(rtypes, counts) if c > 0)
    sections.append(f"Available FHIR data: {counts_str}")

    # 2. Active conditions
    try:
        bundle = await _fhir_get("Condition", {
            "clinical-status": "active",
            "_count": "50",
            "_sort": "-recorded-date"
        })
        conditions = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            code = res.get("code", {})
            text = code.get("text", "")
            if not text:
                codings = code.get("coding", [])
                text = codings[0].get("display", "") if codings else "Unknown"
            recorded = res.get("recordedDate", "")[:10]
            conditions.append(f"- {text}" + (f" (recorded {recorded})" if recorded else ""))
        if conditions:
            sections.append("Active conditions:\n" + "\n".join(conditions))
    except Exception:
        pass

    # 3. Medications — use the grouped, deduplicated list from meds.py
    try:
        grouped_meds = await get_grouped_medications()
        meds = []
        for med in grouped_meds:
            status_label = {
                "taking": "TAKING (patient confirmed)",
                "not_taking": "NOT TAKING (patient confirmed)",
                "as_needed": "AS NEEDED (patient confirmed)",
            }
            parts = [f"- {med['display']} [key: {med['key']}]"]
            if med.get("user_override"):
                parts.append(f"[{status_label.get(med['user_override'], med['user_override'])}]")
            elif med.get("effective_status"):
                parts.append(f"[{med['effective_status']}]")
            if med.get("most_recent_order"):
                parts.append(f"(ordered {med['most_recent_order']})")
            dosage = med.get("user_dosage") or med.get("dosage") or ""
            frequency = med.get("user_frequency") or med.get("frequency") or ""
            if dosage:
                parts.append(f"— {dosage}")
            if frequency:
                parts.append(f"({frequency})")
            meds.append(" ".join(parts))

        if meds:
            sections.append(f"Medications ({len(meds)} unique, deduplicated):\n" + "\n".join(meds))
    except Exception as e:
        sections.append(f"Medications: error loading — {str(e)}")

    # 4. Recent lab results (last 20 observations with numeric values)
    try:
        bundle = await _fhir_get("Observation", {
            "category": "laboratory",
            "_count": "20",
            "_sort": "-date"
        })
        labs = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            code = res.get("code", {})
            display = code.get("text", "")
            if not display:
                codings = code.get("coding", [])
                display = codings[0].get("display", "") if codings else ""
            vq = res.get("valueQuantity", {})
            value = vq.get("value")
            unit = vq.get("unit", "")
            date = (res.get("effectiveDateTime") or "")[:10]
            ref_range = ""
            ranges = res.get("referenceRange", [])
            if ranges:
                lo = ranges[0].get("low", {}).get("value", "")
                hi = ranges[0].get("high", {}).get("value", "")
                if lo or hi:
                    ref_range = f" [ref: {lo}-{hi}]"
            if value is not None and display:
                labs.append(f"- {display}: {value} {unit}{ref_range} ({date})")
        if labs:
            sections.append("Recent lab results:\n" + "\n".join(labs))
    except Exception:
        pass

    # 5. Allergies
    try:
        bundle = await _fhir_get("AllergyIntolerance", {"_count": "20"})
        allergies = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            code = res.get("code", {})
            text = code.get("text", "")
            if not text:
                codings = code.get("coding", [])
                text = codings[0].get("display", "") if codings else ""
            if text:
                allergies.append(f"- {text}")
        if allergies:
            sections.append("Known allergies:\n" + "\n".join(allergies))
    except Exception:
        pass

    return "\n\n".join(sections)


async def get_preferences() -> dict:
    """Load all preferences as a dict."""
    db = await get_db()
    cursor = await db.execute("SELECT key, value FROM preferences")
    rows = await cursor.fetchall()
    return {r["key"]: r["value"] for r in rows}


def build_system_prompt(patient_context: str, prefs: dict = None) -> str:
    prefs = prefs or {}
    # Build the user-customizable response style section
    style_lines = []
    if prefs.get("response_length"):
        style_lines.append(f"Response length preference: {prefs['response_length']}")
    if prefs.get("tone"):
        style_lines.append(f"Tone: {prefs['tone']}")
    if prefs.get("detail_level"):
        style_lines.append(f"Technical detail level: {prefs['detail_level']}")
    if prefs.get("custom_instructions"):
        style_lines.append(f"Additional instructions from the patient: {prefs['custom_instructions']}")

    style_section = ""
    if style_lines:
        style_section = "\n\nPATIENT COMMUNICATION PREFERENCES:\n" + "\n".join(f"- {l}" for l in style_lines)

    assistant_name = prefs.get("assistant_name", "Assistant")
    name_intro = f'Your name is "{assistant_name}". ' if assistant_name and assistant_name != "Assistant" else ""

    today_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

    return f"""{name_intro}You are a knowledgeable, thoughtful health assistant for a Personal Health Vault. You have access to the patient's medical data summary below. Your role is to help the patient understand their health data, spot patterns, prepare for doctor appointments, and make informed decisions about their care.

TODAY'S DATE: {today_str}
Use this when calculating dates for reminders, follow-ups, or any time-sensitive recommendations.

IMPORTANT GUIDELINES:
- Be specific and cite data when discussing trends or findings (e.g., "Your creatinine was 1.2 mg/dL on March 15, up from 1.0 in January").
- When you notice genuinely concerning patterns, suggest the patient discuss them with their physician — but only when truly warranted, not as a routine disclaimer.
- If you don't have enough data to answer a question, say so clearly rather than speculating.
- When discussing lab values, note whether they are within reference ranges when that information is available.
- Do NOT add medical disclaimers, "consult your doctor" reminders, or "this is not medical advice" caveats to your responses. The UI already displays a permanent disclaimer. Adding one to every response is redundant and patronizing to this technically sophisticated patient.
- The medication list in the patient data summary has already been deduplicated and grouped. Multiple FHIR records for the same medication are normal (prescription renewals, data imports) and have been consolidated. Do NOT flag duplicate records, conflicting FHIR statuses, or "medication reconciliation issues" — these are data artifacts, not clinical problems. Focus on clinically meaningful observations.
- Keep your tone helpful and informative, not alarmist. Present findings as useful context, not urgent warnings.

DEEP ANALYSIS CAPABILITIES:
You have access to a run_analysis tool that performs in-depth cross-referencing of the patient's conditions, medications, and lab results. The available analysis types are:
- trend_analysis: Analyze lab value trends over time, flag concerning directions
- medication_review: Review medications in context of conditions and lab results, check for interactions
- pre_appointment: Prepare a briefing for an upcoming doctor's appointment with key discussion points
- comprehensive: Full review of all available data for overall health assessment

CRITICAL: Only use run_analysis for the INITIAL deep-dive when a broad question genuinely requires cross-referencing data you don't already have. For follow-up questions in the same conversation, ALWAYS answer from the context you already have (the patient data summary above and any previous analysis results in the conversation history). Do NOT re-run analysis for follow-ups — it's slow, expensive, and the data hasn't changed. If the patient shares new information or context (like their treatment plan), incorporate it into your existing understanding and respond conversationally.

UPDATING HEALTH RECORDS:
You can propose updates to the patient's health record using the user_propose_update tool. This includes:
- Adding home measurements (blood pressure, weight, glucose, temperature, etc.)
- Updating medication status (marking as stopped, started, changed dose)
- Adding a new medication the patient reports taking
- Adding patient notes or annotations
- Creating reminders for follow-up actions
When you use user_propose_update, the patient will see a confirmation card in the chat showing EXACTLY what will be written to their record. The update only happens after they explicitly approve it. All proposed updates are tagged as patient-entered data in FHIR, so they're always distinguishable from clinician-entered records.
Use this proactively when the patient tells you something that implies a record update — e.g., "I stopped taking X" or "My blood pressure this morning was 128/82". Propose the update and explain what you're doing. If in doubt, ask the patient if they'd like you to record it.

IMPORTANT — MEDICATION STATUS CHANGES REQUIRE TWO ACTIONS:
When a patient says they are pausing, stopping, or changing a medication, you MUST propose the update_medication_status action FIRST to actually change the medication's status in their record. Only AFTER that has been approved should you propose a create_reminder for follow-up. Never create only a reminder without first changing the medication status — the reminder alone does NOT update the medication record. For example, if a patient says "I'm going to pause doxycycline", you should:
1. First call user_propose_update with action_type "update_medication_status" (status: "not_taking", with a note explaining the pause reason)
2. After the patient approves that, THEN call user_propose_update with action_type "create_reminder" to check back in 1-2 weeks

REMINDERS:
You can create reminders using user_propose_update with action_type "create_reminder". Use this whenever a conversation implies a follow-up action or check-in is needed. Examples:
- Patient decides to pause a medication → FIRST update medication status, THEN create a reminder to check how they're doing in 1-2 weeks
- Patient reports new symptoms → remind to follow up if symptoms persist
- Discussion about scheduling an appointment → remind to book it
- Lab results are borderline → remind to recheck in the appropriate timeframe
Each reminder can include offered actions — structured buttons the patient can click to resolve the reminder. For example, a medication pause reminder might offer: "Rashes improved — keep paused", "Scalp issues returned — restart medication", or "No change — discuss with doctor".
Be proactive about suggesting reminders. When a conversation naturally implies something the patient should follow up on, propose a reminder with appropriate timing and actions. The reminder params are:
- message: what to remind about (required)
- due_at: ISO datetime for when the reminder should fire (required)
- context: brief context from the conversation
- priority: low/normal/high
- linked_med: medication key if relevant
- linked_condition: condition name if relevant
- actions: list of {{label, action_type, params}} for resolution buttons
{style_section}

PATIENT DATA SUMMARY:
{patient_context}
"""


# ── FHIR Tools for Claude ─────────────────────────────────────────────────

FHIR_TOOLS = [
    {
        "name": "search_medications",
        "description": "Search the patient's medication records. Returns medication name, status, date ordered, dosage instructions, and prescriber. Use this when the patient asks about specific medications, treatment timelines, or drug history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Medication name to search for (partial match). Leave empty to list all.",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status: active, completed, stopped, cancelled. Leave empty for all.",
                    "enum": ["active", "completed", "stopped", "cancelled", ""],
                },
                "count": {
                    "type": "integer",
                    "description": "Max results to return (default 20).",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_lab_results",
        "description": "Search the patient's laboratory test results. Returns test name, value, unit, reference range, and date. Use this to look up specific lab values, track trends over time, or find abnormal results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term for lab test name (e.g., 'creatinine', 'hemoglobin', 'WBC'). Searches display name.",
                },
                "loinc_code": {
                    "type": "string",
                    "description": "Specific LOINC code to search for (e.g., '2160-0' for creatinine). More precise than text search.",
                },
                "count": {
                    "type": "integer",
                    "description": "Max results to return (default 20).",
                    "default": 20,
                },
                "sort": {
                    "type": "string",
                    "description": "Sort order: '-date' (newest first, default) or 'date' (oldest first).",
                    "default": "-date",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_conditions",
        "description": "Search the patient's diagnosed conditions. Returns condition name, clinical status, date recorded, and category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term for condition name (e.g., 'myeloma', 'diabetes').",
                },
                "clinical_status": {
                    "type": "string",
                    "description": "Filter by status: active, resolved, inactive. Leave empty for all.",
                    "enum": ["active", "resolved", "inactive", ""],
                },
                "count": {
                    "type": "integer",
                    "description": "Max results (default 30).",
                    "default": 30,
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_encounters",
        "description": "Search the patient's encounters (visits, admissions, appointments). Returns encounter type, date, reason, location, and providers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term for encounter reason or type.",
                },
                "count": {
                    "type": "integer",
                    "description": "Max results (default 20).",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_documents",
        "description": "Search clinical documents and notes (DiagnosticReport, DocumentReference). Returns document type, date, content/summary, and author.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term for document content or title.",
                },
                "resource_type": {
                    "type": "string",
                    "description": "Restrict to DiagnosticReport or DocumentReference.",
                    "enum": ["DiagnosticReport", "DocumentReference", ""],
                },
                "count": {
                    "type": "integer",
                    "description": "Max results (default 10).",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "name": "run_analysis",
        "description": (
            "Run a deep clinical analysis on the patient's data. This gathers labs, conditions, and medications "
            "and uses AI to produce structured findings. Use this when the patient asks for a medication review, "
            "lab trend analysis, pre-appointment briefing, comprehensive health review, or any request that requires "
            "cross-referencing multiple data types to find patterns or concerns. "
            "Available analysis types: trend_analysis (lab trends over time), medication_review (meds vs conditions vs labs), "
            "pre_appointment (appointment preparation briefing), comprehensive (full health review)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "analysis_type": {
                    "type": "string",
                    "description": "The type of analysis to run.",
                    "enum": ["trend_analysis", "medication_review", "pre_appointment", "comprehensive"],
                },
                "months": {
                    "type": "integer",
                    "description": "How many months of data to analyze (default 6).",
                    "default": 6,
                },
            },
            "required": ["analysis_type"],
        },
    },
    {
        "name": "user_propose_update",
        "description": (
            "Propose a write/update to the patient's health record. The patient will see a confirmation "
            "card and must explicitly approve before any change is made. All changes are tagged as "
            "patient-entered data. Use this when the patient tells you something that implies a record "
            "update — e.g., 'I stopped taking teclistamab', 'My BP this morning was 128/82', "
            "'I started taking vitamin D 2000 IU daily', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "description": "The type of update to propose.",
                    "enum": ["add_observation", "update_medication_status", "add_medication", "add_note", "create_reminder"],
                },
                "summary": {
                    "type": "string",
                    "description": "A clear, patient-friendly summary of what will be recorded (e.g., 'Record blood pressure 128/82 mmHg from today').",
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Parameters for the action. Varies by action_type:\n"
                        "- add_observation: {name, value, unit, date?, category?, note?, components? (for BP: [{code:{text:'Systolic'},valueQuantity:{value,unit}},...]}"
                        "\n- update_medication_status: {med_key (MUST use the exact [key: ...] value from the medication list), display_name, status ('taking'|'not_taking'|'as_needed'), notes?}"
                        "\n- add_medication: {name, dosage?, date?, note?}"
                        "\n- add_note: {title, text, type?}"
                        "\n- create_reminder: {message, due_at (ISO datetime), context?, priority?, linked_med?, linked_condition?, actions? (list of {label, action_type, params})}"
                    ),
                },
            },
            "required": ["action_type", "summary", "params"],
        },
    },
]


async def _execute_fhir_tool(name: str, args: dict) -> str:
    """Execute a FHIR tool call and return the result as a string."""
    try:
        if name == "search_medications":
            grouped = await get_grouped_medications()
            # Apply filters
            name_filter = (args.get("name") or "").lower()
            status_filter = args.get("status") or ""
            count = args.get("count", 20)
            # Map status filter: FHIR "active" also matches our "taking"
            status_map = {"active": {"active", "taking", "as_needed"}, "stopped": {"stopped", "not_taking"}, "completed": {"completed"}, "cancelled": {"cancelled"}}
            allowed_statuses = status_map.get(status_filter, set()) if status_filter else None
            results = []
            for med in grouped:
                if name_filter and name_filter not in med["display"].lower() and name_filter not in med["key"]:
                    continue
                if allowed_statuses and med["effective_status"] not in allowed_statuses:
                    continue
                dosage = med.get("user_dosage") or med.get("dosage") or ""
                frequency = med.get("user_frequency") or med.get("frequency") or ""
                entry = {
                    "medication": med["display"],
                    "status": med["effective_status"],
                    "ordered": med.get("most_recent_order", ""),
                    "dosage": dosage,
                    "frequency": frequency,
                    "prescriber": med.get("prescriber", ""),
                }
                if med.get("user_override"):
                    entry["patient_confirmed"] = med["user_override"]
                results.append(entry)
                if len(results) >= count:
                    break
            return json.dumps({"total": len(results), "results": results}, indent=2)

        elif name == "search_lab_results":
            params = {
                "category": "laboratory",
                "_count": str(args.get("count", 20)),
                "_sort": args.get("sort", "-date"),
            }
            if args.get("loinc_code"):
                params["code"] = f"http://loinc.org|{args['loinc_code']}"
            elif args.get("query"):
                params["_content"] = args["query"]
            bundle = await _fhir_get("Observation", params)
            results = []
            for entry in bundle.get("entry", []):
                res = entry.get("resource", {})
                code = res.get("code", {})
                display = code.get("text", "")
                if not display:
                    codings = code.get("coding", [])
                    display = next((c.get("display", "") for c in codings if c.get("display")), "")
                loinc = ""
                for c in code.get("coding", []):
                    if "loinc" in c.get("system", ""):
                        loinc = c.get("code", "")
                        break
                vq = res.get("valueQuantity", {})
                value = vq.get("value")
                unit = vq.get("unit", "")
                date = (res.get("effectiveDateTime") or "")[:10]
                ref_lo, ref_hi = "", ""
                ranges = res.get("referenceRange", [])
                if ranges:
                    ref_lo = ranges[0].get("low", {}).get("value", "")
                    ref_hi = ranges[0].get("high", {}).get("value", "")
                results.append({
                    "test": display, "loinc": loinc, "value": value, "unit": unit,
                    "date": date, "ref_low": ref_lo, "ref_high": ref_hi,
                })
            return json.dumps({"total": bundle.get("total", len(results)), "results": results}, indent=2)

        elif name == "search_conditions":
            params = {"_count": str(args.get("count", 30)), "_sort": "-recorded-date"}
            if args.get("clinical_status"):
                params["clinical-status"] = args["clinical_status"]
            if args.get("query"):
                params["_content"] = args["query"]
            bundle = await _fhir_get("Condition", params)
            results = []
            for entry in bundle.get("entry", []):
                res = entry.get("resource", {})
                code = res.get("code", {})
                text = code.get("text", "")
                if not text:
                    codings = code.get("coding", [])
                    text = next((c.get("display", "") for c in codings if c.get("display")), "Unknown")
                status = res.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "")
                recorded = (res.get("recordedDate") or "")[:10]
                results.append({"condition": text, "status": status, "recorded": recorded})
            return json.dumps({"total": bundle.get("total", len(results)), "results": results}, indent=2)

        elif name == "search_encounters":
            params = {"_count": str(args.get("count", 20)), "_sort": "-date"}
            if args.get("query"):
                params["_content"] = args["query"]
            bundle = await _fhir_get("Encounter", params)
            results = []
            for entry in bundle.get("entry", []):
                res = entry.get("resource", {})
                etype = ""
                for t in res.get("type", []):
                    etype = t.get("text", "")
                    if not etype:
                        codings = t.get("coding", [])
                        etype = next((c.get("display", "") for c in codings if c.get("display")), "")
                    break
                period = res.get("period", {})
                start = (period.get("start") or "")[:10]
                reason = ""
                for r_list in res.get("reasonCode", []):
                    reason = r_list.get("text", "")
                    if reason:
                        break
                status = res.get("status", "")
                results.append({
                    "type": etype, "status": status, "date": start, "reason": reason,
                })
            return json.dumps({"total": bundle.get("total", len(results)), "results": results}, indent=2)

        elif name == "search_documents":
            rtype = args.get("resource_type", "") or "DiagnosticReport"
            params = {"_count": str(args.get("count", 10)), "_sort": "-date"}
            if args.get("query"):
                params["_content"] = args["query"]
            bundle = await _fhir_get(rtype, params)
            results = []
            for entry in bundle.get("entry", []):
                res = entry.get("resource", {})
                code = res.get("code", {}) if rtype == "DiagnosticReport" else res.get("type", {})
                text = code.get("text", "")
                if not text:
                    codings = code.get("coding", [])
                    text = next((c.get("display", "") for c in codings if c.get("display")), "")
                date = (res.get("effectiveDateTime") or res.get("date") or "")[:10]
                conclusion = res.get("conclusion", "")
                results.append({"title": text, "date": date, "summary": conclusion[:500] if conclusion else ""})
            return json.dumps({"total": bundle.get("total", len(results)), "results": results}, indent=2)

        elif name == "run_analysis":
            analysis_type = args.get("analysis_type", "trend_analysis")
            months = args.get("months", 6)
            result = await analyst_run_analysis(analysis_type, months=months)
            # Return the analysis text plus metadata (not the full stored doc)
            if "error" in result:
                return json.dumps({"error": result["error"]})
            return json.dumps({
                "analysis_type": result.get("analysis_type"),
                "analysis_name": result.get("analysis_name"),
                "analysis": result.get("analysis", ""),
                "data_summary": result.get("data_summary"),
                "generation_time_seconds": result.get("generation_time_seconds"),
            }, indent=2)

        elif name == "user_propose_update":
            # Don't execute — return the proposal as-is so the SSE stream
            # can deliver it as a confirmation card. The tool "result" tells Claude
            # the proposal was shown to the patient.
            return json.dumps({
                "proposed": True,
                "action_type": args.get("action_type"),
                "summary": args.get("summary"),
                "params": args.get("params"),
                "message": "The proposed update has been shown to the patient for confirmation. They will approve or reject it. Do not propose the same update again — wait for their response.",
            })

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Streaming Chat with Tool Use ──────────────────────────────────────────

async def stream_chat(
    thread_id: str,
    user_message: str,
    history: list[dict],
    system_prompt: str,
    use_tools: bool = True,
    max_tool_rounds: int = 5,
) -> AsyncIterator[str]:
    """Stream a chat response from Claude with FHIR tool use, yielding SSE-formatted chunks."""

    if not ANTHROPIC_API_KEY:
        yield f"data: {json.dumps({'type': 'error', 'error': 'ANTHROPIC_API_KEY not configured. Set it in the api service environment.'})}\n\n"
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Build message list from history
    messages = []
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    full_response = []
    MAX_TOOL_ROUNDS = 5  # default, overridable via preferences

    try:
        effective_rounds = max_tool_rounds if use_tools else 0

        for _round in range(effective_rounds + 1):
            is_last_round = (_round == effective_rounds)

            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                # Don't offer tools on last round — force a text response
                **({"tools": FHIR_TOOLS} if not is_last_round else {}),
            )

            # Separate text and tool_use blocks
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            # If no tool calls (or last round), stream the text to the user
            if not tool_uses or response.stop_reason != "tool_use" or is_last_round:
                for block in text_blocks:
                    full_response.append(block.text)
                    yield f"data: {json.dumps({'type': 'text', 'text': block.text})}\n\n"
                break

            # Tool calls needed — show indicators but discard intermediate text
            for tool_use in tool_uses:
                yield f"data: {json.dumps({'type': 'tool_call', 'tool': tool_use.name, 'args': tool_use.input})}\n\n"

            # Execute tool calls and feed results back
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tool_use in tool_uses:
                result = await _execute_fhir_tool(tool_use.name, tool_use.input)
                # Check if the tool returned an error or a proposed action
                is_error = False
                is_proposal = False
                try:
                    parsed = json.loads(result)
                    is_error = "error" in parsed
                    is_proposal = parsed.get("proposed") is True
                except Exception:
                    pass
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result,
                    **({"is_error": True} if is_error else {}),
                })
                if is_proposal:
                    # Emit a special SSE event for the confirmation card
                    yield f"data: {json.dumps({'type': 'proposed_action', 'action_type': parsed.get('action_type'), 'summary': parsed.get('summary'), 'params': parsed.get('params'), 'thread_id': thread_id})}\n\n"
                else:
                    result_summary = result[:2000] if len(result) > 2000 else result
                    yield f"data: {json.dumps({'type': 'tool_result', 'tool': tool_use.name, 'result': result_summary, 'is_error': is_error})}\n\n"
            messages.append({"role": "user", "content": tool_results})

            # Notify frontend of round completion
            yield f"data: {json.dumps({'type': 'round', 'round': _round + 1, 'max_rounds': effective_rounds})}\n\n"

        # Send completion event
        complete_text = "".join(full_response)
        if not complete_text:
            complete_text = "I gathered the data but wasn't able to formulate a complete response. Please try rephrasing your question."
            yield f"data: {json.dumps({'type': 'text', 'text': complete_text})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'text': complete_text, 'model': ANTHROPIC_MODEL})}\n\n"

    except asyncio.TimeoutError:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Request timed out. The question may have required too many lookups. Try a more specific question.'})}\n\n"
    except anthropic.APIConnectionError as e:
        yield f"data: {json.dumps({'type': 'error', 'error': f'Could not connect to Anthropic API: {str(e)}'})}\n\n"
    except anthropic.AuthenticationError:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Invalid Anthropic API key. Check ANTHROPIC_API_KEY.'})}\n\n"
    except anthropic.RateLimitError:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Rate limited by Anthropic. Please wait a moment and try again.'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'error': f'Unexpected error: {str(e)}'})}\n\n"


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/execute-action")
async def execute_action(request: Request):
    """Execute a user-approved proposed action.

    Expects JSON: { action_type, summary, params }
    Only called after the user explicitly clicks Approve in the confirmation card.
    """
    body = await request.json()
    action_type = body.get("action_type", "")
    summary = body.get("summary", "")
    params = body.get("params", {})

    if not action_type:
        return {"error": "action_type is required"}

    result = await execute_proposed_action({
        "action_type": action_type,
        "params": params,
    })

    if result.get("ok"):
        print(f"[assistant] Executed user-approved action: {action_type} — {summary}", flush=True)
    else:
        print(f"[assistant] Failed to execute action: {action_type} — {result.get('error', '')}", flush=True)

    return result


@router.post("/chat")
async def chat(request: Request):
    """Stream a chat response. Expects JSON: {thread_id?, message}"""
    body = await request.json()
    user_message = body.get("message", "").strip()
    thread_id = body.get("thread_id")
    use_tools = body.get("use_tools", True)

    if not user_message:
        return {"error": "Empty message"}

    db = await get_db()

    # Create or fetch thread
    if not thread_id:
        thread_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        # Use first ~50 chars of message as title
        title = user_message[:50] + ("..." if len(user_message) > 50 else "")
        await db.execute(
            "INSERT INTO threads (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (thread_id, title, now, now),
        )
        await db.commit()

    # Save user message
    msg_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    await db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (msg_id, thread_id, "user", user_message, now),
    )
    await db.commit()

    # Load conversation history for this thread (excluding current message)
    cursor = await db.execute(
        "SELECT role, content FROM messages WHERE thread_id = ? ORDER BY created_at",
        (thread_id,),
    )
    rows = await cursor.fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in rows[:-1]]  # exclude the one we just inserted

    # Assemble patient context and preferences
    patient_context, prefs = await asyncio.gather(
        gather_patient_context(), get_preferences()
    )
    system_prompt = build_system_prompt(patient_context, prefs)

    async def event_stream():
        full_text = ""
        model_used = ANTHROPIC_MODEL

        max_rounds = int(prefs.get("max_tool_rounds", "5"))
        async for chunk in stream_chat(thread_id, user_message, history, system_prompt,
                                       use_tools=use_tools, max_tool_rounds=max_rounds):
            yield chunk
            # Parse to capture the done event for DB storage
            try:
                line = chunk.strip()
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if data.get("type") == "done":
                        full_text = data.get("text", "")
                        model_used = data.get("model", ANTHROPIC_MODEL)
            except Exception:
                pass

        # Save assistant response to DB
        if full_text:
            db = await get_db()
            resp_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            await db.execute(
                "INSERT INTO messages (id, thread_id, role, content, model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (resp_id, thread_id, "assistant", full_text, model_used, now),
            )
            await db.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id)
            )
            await db.commit()

        # Send thread_id so the frontend knows which thread this belongs to
        yield f"data: {json.dumps({'type': 'meta', 'thread_id': thread_id})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer
        },
    )


@router.get("/threads")
async def list_threads():
    """List all conversation threads, most recent first."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, title, created_at, updated_at FROM threads ORDER BY updated_at DESC"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.get("/thread/{thread_id}")
async def get_thread(thread_id: str):
    """Get all messages in a thread."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, role, content, model, created_at FROM messages WHERE thread_id = ? ORDER BY created_at",
        (thread_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.delete("/thread/{thread_id}")
async def delete_thread(thread_id: str):
    """Delete a thread and all its messages."""
    db = await get_db()
    await db.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
    await db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    await db.commit()
    return {"ok": True}


@router.get("/preferences")
async def get_prefs():
    """Get all user preferences."""
    return await get_preferences()


@router.put("/preferences")
async def set_prefs(request: Request):
    """Set user preferences. Expects JSON: {key: value, ...}"""
    body = await request.json()
    db = await get_db()
    for key, value in body.items():
        if value is None or value == "":
            await db.execute("DELETE FROM preferences WHERE key = ?", (key,))
        else:
            await db.execute(
                "INSERT INTO preferences (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
    await db.commit()
    return await get_preferences()
