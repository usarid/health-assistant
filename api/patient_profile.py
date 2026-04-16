"""PHV Patient Profile — Aggregates demographics, vitals, conditions,
major health events, and trends into a single "who is this patient" view.

Provides:
  GET /api/profile           — full patient profile (accepts ?units=imperial|metric)
  PUT /api/profile/condition  — user override for a condition status
  DELETE /api/profile/condition — remove a condition override
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import aiosqlite
import httpx
from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/profile", tags=["profile"])

# ── Configuration ─────────────────────────────────────────────────────────

import os
HAPI_BASE = os.environ.get("HAPI_BASE", "http://hapi:8080/fhir")

_fhir_client = None


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


# ── SQLite overlay for condition overrides ─────────────────────────────────

DB_PATH = os.environ.get("ASSISTANT_DB", "/data/chat.db")
_db = None


async def _get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.executescript("""
            CREATE TABLE IF NOT EXISTS condition_overrides (
                condition_key TEXT PRIMARY KEY,
                user_status TEXT NOT NULL CHECK(user_status IN ('active', 'resolved', 'in_remission', 'managed', 'inactive')),
                note TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
        """)
        await _db.commit()
    return _db


async def get_condition_overrides() -> dict:
    """Return all condition overrides keyed by condition_key."""
    db = await _get_db()
    cursor = await db.execute("SELECT * FROM condition_overrides")
    rows = await cursor.fetchall()
    return {r["condition_key"]: dict(r) for r in rows}


# ── Unit Conversion ──────────────────────────────────────────────────────

CONVERSIONS = {
    # (from_unit, to_unit): lambda
    ("cm", "in"): lambda v: v / 2.54,
    ("in", "cm"): lambda v: v * 2.54,
    ("[in_i]", "cm"): lambda v: v * 2.54,
    ("cm", "[in_i]"): lambda v: v / 2.54,
    ("kg", "lbs"): lambda v: v * 2.20462,
    ("lbs", "kg"): lambda v: v / 2.20462,
    ("kg", "lb"): lambda v: v * 2.20462,
    ("lb", "kg"): lambda v: v / 2.20462,
    ("Cel", "°F"): lambda v: v * 9 / 5 + 32,
    ("°C", "°F"): lambda v: v * 9 / 5 + 32,
    ("°F", "Cel"): lambda v: (v - 32) * 5 / 9,
    ("°F", "°C"): lambda v: (v - 32) * 5 / 9,
    ("kg/m2", "kg/m²"): lambda v: v,  # display normalization
}

# Imperial target units per vital name
IMPERIAL_UNITS = {
    "Height": "in",
    "Weight": "lbs",
    "BMI": "kg/m²",
    "Temperature": "°F",
}

# Metric target units per vital name (FHIR default, usually already metric)
METRIC_UNITS = {
    "Height": "cm",
    "Weight": "kg",
    "BMI": "kg/m²",
    "Temperature": "°C",
}


def convert_value(value, from_unit, to_unit):
    """Convert a value between units. Returns (converted_value, display_unit)."""
    if not from_unit or not to_unit:
        return value, from_unit or to_unit or ""
    # Normalize units for matching
    fu = from_unit.strip()
    tu = to_unit.strip()
    if fu == tu:
        return value, tu
    key = (fu, tu)
    if key in CONVERSIONS:
        return CONVERSIONS[key](value), tu
    # Try case-insensitive
    for (a, b), fn in CONVERSIONS.items():
        if a.lower() == fu.lower() and b.lower() == tu.lower():
            return fn(value), tu
    # No conversion available
    return value, from_unit


# ── Vital Averaging Config ───────────────────────────────────────────────

# Per-vital: time window in days for averaging, and aggregation method
VITAL_CONFIG = {
    "8302-2":  {"name": "Height",           "unit": "cm",   "window_days": 365, "method": "latest"},
    "29463-7": {"name": "Weight",           "unit": "kg",   "window_days": 14,  "method": "average"},
    "39156-5": {"name": "BMI",              "unit": "kg/m²","window_days": 14,  "method": "average"},
    "8480-6":  {"name": "BP Systolic",      "unit": "mmHg", "window_days": 14,  "method": "average"},
    "8462-4":  {"name": "BP Diastolic",     "unit": "mmHg", "window_days": 14,  "method": "average"},
    "8867-4":  {"name": "Heart Rate",       "unit": "bpm",  "window_days": 7,   "method": "average"},
    "8310-5":  {"name": "Temperature",      "unit": "°F",   "window_days": 0,   "method": "latest"},
    "59408-5": {"name": "SpO2",             "unit": "%",    "window_days": 7,   "method": "average"},
    "9279-1":  {"name": "Respiratory Rate", "unit": "/min", "window_days": 7,   "method": "average"},
}


def _describe_window(window_days: int, reading_count: int, method: str) -> str:
    """Return a human-readable description of what time frame this covers."""
    if method == "latest" or reading_count <= 1:
        return "latest"
    if window_days <= 7:
        return "past week"
    if window_days <= 14:
        return "past 2 weeks"
    if window_days <= 30:
        return "past month"
    if window_days <= 90:
        return "past 3 months"
    if window_days <= 365:
        return "past year"
    return f"past {window_days} days"


# ── Patient Demographics ──────────────────────────────────────────────────

async def gather_demographics() -> dict:
    """Fetch the Patient resource and extract demographics."""
    try:
        bundle = await _fhir_get("Patient", {"_count": "1"})
        entries = bundle.get("entry", [])
        if not entries:
            return {}

        patient = entries[0].get("resource", {})
        patient_id = patient.get("id", "")

        # Name
        names = patient.get("name", [])
        display_name = ""
        for n in names:
            if n.get("use") == "official" or not display_name:
                given = " ".join(n.get("given", []))
                family = n.get("family", "")
                display_name = f"{given} {family}".strip()

        # Birth date and age
        birth_date = patient.get("birthDate", "")
        age = None
        if birth_date:
            try:
                bd = datetime.strptime(birth_date, "%Y-%m-%d")
                today = datetime.now()
                age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
            except Exception:
                pass

        # Gender
        gender = patient.get("gender", "")

        # Address
        addresses = patient.get("address", [])
        address = ""
        if addresses:
            a = addresses[0]
            parts = []
            if a.get("city"):
                parts.append(a["city"])
            if a.get("state"):
                parts.append(a["state"])
            if a.get("postalCode"):
                parts.append(a["postalCode"])
            address = ", ".join(parts)

        # Phone / email
        telecoms = patient.get("telecom", [])
        phone = ""
        email = ""
        for t in telecoms:
            if t.get("system") == "phone" and not phone:
                phone = t.get("value", "")
            if t.get("system") == "email" and not email:
                email = t.get("value", "")

        # Language
        communications = patient.get("communication", [])
        language = ""
        for c in communications:
            lang = c.get("language", {})
            language = lang.get("text", "") or (lang.get("coding", [{}])[0].get("display", ""))
            if c.get("preferred"):
                break

        # Marital status
        marital = patient.get("maritalStatus", {})
        marital_status = marital.get("text", "") or (marital.get("coding", [{}])[0].get("display", "") if marital.get("coding") else "")

        # Race/ethnicity (US Core extensions)
        race = ""
        ethnicity = ""
        for ext in patient.get("extension", []):
            url = ext.get("url", "")
            if "us-core-race" in url:
                for sub in ext.get("extension", []):
                    if sub.get("url") == "text":
                        race = sub.get("valueString", "")
            elif "us-core-ethnicity" in url:
                for sub in ext.get("extension", []):
                    if sub.get("url") == "text":
                        ethnicity = sub.get("valueString", "")

        return {
            "id": patient_id,
            "name": display_name,
            "birth_date": birth_date,
            "age": age,
            "gender": gender,
            "address": address,
            "phone": phone,
            "email": email,
            "language": language,
            "marital_status": marital_status,
            "race": race,
            "ethnicity": ethnicity,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Recent Vitals (with averaging) ──────────────────────────────────────

async def gather_recent_vitals(unit_system: str = "imperial") -> dict:
    """Fetch vital sign observations and compute averages over clinically
    appropriate time windows per vital type."""
    try:
        bundle = await _fhir_get("Observation", {
            "category": "vital-signs",
            "_count": "500",
            "_sort": "-date",
        })

        now = datetime.now()

        # Collect all readings grouped by LOINC code
        by_code = defaultdict(list)
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            codings = res.get("code", {}).get("coding", [])
            loinc = ""
            display = res.get("code", {}).get("text", "")
            for c in codings:
                if c.get("system", "").endswith("loinc.org"):
                    loinc = c.get("code", "")
                    if not display:
                        display = c.get("display", "")
                    break

            value = None
            unit = ""
            vq = res.get("valueQuantity", {})
            if vq:
                value = vq.get("value")
                unit = vq.get("unit", "")

            # Also check component (for BP readings that use components)
            components = res.get("component", [])
            for comp in components:
                comp_codings = comp.get("code", {}).get("coding", [])
                comp_loinc = ""
                for cc in comp_codings:
                    if cc.get("system", "").endswith("loinc.org"):
                        comp_loinc = cc.get("code", "")
                        break
                comp_vq = comp.get("valueQuantity", {})
                if comp_loinc and comp_vq.get("value") is not None:
                    by_code[comp_loinc].append({
                        "value": comp_vq["value"],
                        "unit": comp_vq.get("unit", ""),
                        "date": (res.get("effectiveDateTime") or "")[:10],
                    })

            date = (res.get("effectiveDateTime") or "")[:10]
            if value is not None and (loinc or display):
                key = loinc or display
                by_code[key].append({"value": value, "unit": unit, "date": date})

        # Build vitals summary with averaging
        vitals = {}
        for code, readings in by_code.items():
            readings.sort(key=lambda r: r["date"], reverse=True)
            config = VITAL_CONFIG.get(code, {})
            name = config.get("name", code)
            method = config.get("method", "average")
            window_days = config.get("window_days", 14)

            if method == "latest" or window_days == 0:
                # Just use the most recent reading
                current = readings[0]
                display_value = current["value"]
                source_unit = current["unit"] or config.get("unit", "")
                reading_count = 1
                date_display = current["date"]  # ISO date for frontend to format
                window_desc = "latest"
                # Previous for trend comparison
                previous_value = readings[1]["value"] if len(readings) > 1 else None
                previous_date = readings[1]["date"] if len(readings) > 1 else None
            else:
                # Average readings within the time window
                cutoff = now - timedelta(days=window_days)
                cutoff_str = cutoff.strftime("%Y-%m-%d")

                window_readings = [r for r in readings if r["date"] >= cutoff_str]
                if not window_readings:
                    # Fall back to most recent reading if nothing in window
                    window_readings = [readings[0]]

                values = [r["value"] for r in window_readings]
                display_value = sum(values) / len(values)
                reading_count = len(values)
                source_unit = window_readings[0]["unit"] or config.get("unit", "")
                date_display = window_readings[-1]["date"]  # oldest in window (start of range)
                window_desc = _describe_window(window_days, reading_count, method)

                # Previous period for trend: same-length window immediately before
                prev_cutoff = cutoff - timedelta(days=window_days)
                prev_cutoff_str = prev_cutoff.strftime("%Y-%m-%d")
                prev_readings = [r for r in readings if prev_cutoff_str <= r["date"] < cutoff_str]
                if prev_readings:
                    prev_values = [r["value"] for r in prev_readings]
                    previous_value = sum(prev_values) / len(prev_values)
                    previous_date = prev_readings[0]["date"]
                else:
                    previous_value = None
                    previous_date = None

            # Unit conversion
            target_units = IMPERIAL_UNITS if unit_system == "imperial" else METRIC_UNITS
            target_unit = target_units.get(name)
            if target_unit and source_unit:
                display_value, display_unit = convert_value(display_value, source_unit, target_unit)
                if previous_value is not None:
                    previous_value, _ = convert_value(previous_value, source_unit, target_unit)
            else:
                display_unit = source_unit

            vitals[name] = {
                "value": round(display_value, 1) if isinstance(display_value, float) else display_value,
                "unit": display_unit,
                "date": date_display,
                "method": method if reading_count > 1 else "latest",
                "reading_count": reading_count,
                "window": window_desc,
                "previous_value": round(previous_value, 1) if isinstance(previous_value, float) and previous_value is not None else previous_value,
                "previous_date": previous_date,
            }

        return vitals
    except Exception as e:
        return {"error": str(e)}


# ── Active Conditions ─────────────────────────────────────────────────────

import re as _re

# Conditions that are episodic/transient events, not ongoing conditions
_TRANSIENT_PATTERNS = [
    r"^admission\b",
    r"^encounter\b",
    r"^visit\b",
    r"^rash\b.*\beruption\b",
    r"^unknown$",
    r"^status post\b",
    r"^status$",
    r"^h/o:\s",           # "history of" prefix — these are historical, not active
    r"^s/p\b",            # status post
    r"\bsymptoms?\b$",    # vague "symptoms" entries like "GI symptoms"
]
_TRANSIENT_RES = [_re.compile(p, _re.IGNORECASE) for p in _TRANSIENT_PATTERNS]

# Merge rules: map variant names to a canonical form
_MERGE_MAP = {
    "abdominal pain, epigastric": "abdominal pain",
    "generalized abdominal pain": "abdominal pain",
    "pfo (patent foramen ovale)": "patent foramen ovale (PFO)",
    "s/p percutaneous patent foramen ovale closure": "patent foramen ovale (PFO) — closed",
    "peripheral neuropathy": "peripheral neuropathy",
    "axonal polyneuropathy": "peripheral neuropathy",
    "axonal sensorimotor neuropathy": "peripheral neuropathy",
    "numbness and tingling of right leg": "peripheral neuropathy",
    "paresthesia of skin": "peripheral neuropathy",
    "neuropathy": "peripheral neuropathy",
    "cva (cerebrovascular accident)": "CVA (cerebrovascular accident)",
    "right-sided lacunar infarction": "CVA (cerebrovascular accident)",
    "status post cva": None,  # filter out — already covered by CVA entry
    "tia (transient ischemic attack)": "TIA (transient ischemic attack)",
    "multiple myeloma (cms-hcc)": "multiple myeloma",
    "multiple myeloma, remission status unspecified (cms-hcc)": "multiple myeloma",
    "gammopathy monoclonal": "MGUS (monoclonal gammopathy of unknown significance)",
    "mgus (monoclonal gammopathy of unknown significance)": "MGUS (monoclonal gammopathy of unknown significance)",
    "essential hypertension": "hypertension",
    "hypertension": "hypertension",
    "elevated blood pressure reading": "hypertension",
    "exertional dyspnea": "exertional dyspnea",
    "palpitations": "palpitations",
    "supraventricular tachycardia": "supraventricular tachycardia",
    "vertigo": "vertigo",
    "dizziness": "vertigo",
    "anemia": "anemia",
    "constipation": "constipation",
    "irritable bowel syndrome with constipation": "IBS with constipation",
    "proteinuria, unspecified type": "proteinuria",
    "ckd (chronic kidney disease)": "chronic kidney disease (CKD)",
    "pelvic floor dysfunction": "pelvic floor dysfunction",
}

# ── Condition Categorization ──────────────────────────────────────────────

# Map canonical condition names (lowercase) to clinical categories
_CATEGORY_MAP = {
    # Oncology / Hematology
    "multiple myeloma": "Oncology",
    "mgus (monoclonal gammopathy of unknown significance)": "Oncology",
    "anemia": "Oncology",
    "b12 deficiency": "Oncology",

    # Cardiovascular
    "hypertension": "Cardiovascular",
    "cva (cerebrovascular accident)": "Cardiovascular",
    "tia (transient ischemic attack)": "Cardiovascular",
    "patent foramen ovale (pfo)": "Cardiovascular",
    "patent foramen ovale (pfo) — closed": "Cardiovascular",
    "arterial calcification": "Cardiovascular",
    "palpitations": "Cardiovascular",
    "supraventricular tachycardia": "Cardiovascular",

    # Neurological
    "peripheral neuropathy": "Neurological",
    "vertigo": "Neurological",
    "headache": "Neurological",
    "migraine with aura and without status migrainosus, not intractable": "Neurological",

    # Gastrointestinal
    "abdominal pain": "Gastrointestinal",
    "ibs with constipation": "Gastrointestinal",
    "constipation": "Gastrointestinal",
    "gastroesophageal reflux disease": "Gastrointestinal",
    "small intestinal bacterial overgrowth (sibo)": "Gastrointestinal",
    "helicobacter pylori gastritis": "Gastrointestinal",
    "history of colonic polyps": "Gastrointestinal",

    # Renal / Urological
    "chronic kidney disease (ckd)": "Renal",
    "proteinuria": "Renal",
    "pelvic floor dysfunction": "Renal",

    # Metabolic
    "pre-diabetes": "Metabolic",
    "hyperlipidemia": "Metabolic",
    "disorder of sulfur-bearing amino acid metabolism": "Metabolic",

    # Musculoskeletal
    "chronic right-sided thoracic back pain": "Musculoskeletal",
    "facet arthropathy, thoracic": "Musculoskeletal",
    "raynaud's syndrome": "Musculoskeletal",

    # Respiratory
    "exertional dyspnea": "Respiratory",

    # Dermatologic
    "rosacea": "Dermatologic",
}

# Preferred display order of categories
_CATEGORY_ORDER = [
    "Oncology", "Cardiovascular", "Neurological", "Gastrointestinal",
    "Renal", "Metabolic", "Musculoskeletal", "Respiratory", "Dermatologic", "Other",
]


def _categorize_condition(canonical_name: str) -> str:
    """Return the clinical category for a condition."""
    return _CATEGORY_MAP.get(canonical_name.lower(), "Other")


def _normalize_condition_name(name: str) -> str:
    """Normalize a condition name for dedup."""
    n = name.strip()
    lower = n.lower()
    return _MERGE_MAP.get(lower, n)


def _is_transient(name: str) -> bool:
    """Check if a condition name looks transient/episodic."""
    for pat in _TRANSIENT_RES:
        if pat.search(name):
            return True
    return False


async def gather_conditions_raw() -> tuple:
    """Fetch active conditions from FHIR, deduplicate, and filter transient entries.
    Returns (grouped_conditions, all_evidence_texts):
      - grouped_conditions: flat dict { canonical_key: condition_dict }
      - all_evidence_texts: list of ALL raw condition texts (including filtered/transient)
        so the inference engine can use them as evidence
    """
    bundle = await _fhir_get("Condition", {
        "clinical-status": "active",
        "_count": "200",
        "_sort": "-recorded-date",
    })

    raw = []
    for entry in bundle.get("entry", []):
        res = entry.get("resource", {})
        code = res.get("code", {})
        text = code.get("text", "")
        if not text:
            codings = code.get("coding", [])
            text = next((c.get("display", "") for c in codings if c.get("display")), "")
        if not text:
            continue

        recorded = (res.get("recordedDate") or "")[:10]
        onset = ""
        if res.get("onsetDateTime"):
            onset = res["onsetDateTime"][:10]
        elif res.get("onsetPeriod", {}).get("start"):
            onset = res["onsetPeriod"]["start"][:10]

        raw.append({
            "text": text,
            "onset": onset,
            "recorded": recorded,
        })

    # Capture ALL raw texts as evidence (before any filtering)
    all_evidence_texts = [r["text"].lower() for r in raw]

    # Normalize, filter, deduplicate
    grouped = {}
    for r in raw:
        if _is_transient(r["text"]):
            continue

        canonical = _normalize_condition_name(r["text"])
        if canonical is None:
            continue

        key = canonical.lower()
        date = r["onset"] or r["recorded"] or ""

        if key not in grouped:
            grouped[key] = {
                "condition": canonical,
                "onset": date,
                "recorded": r["recorded"],
                "original_texts": [r["text"]],
            }
        else:
            existing = grouped[key]
            existing["original_texts"].append(r["text"])
            if date and (not existing["onset"] or date < existing["onset"]):
                existing["onset"] = date
            if r["recorded"] and (not existing["recorded"] or r["recorded"] < existing["recorded"]):
                existing["recorded"] = r["recorded"]

    return grouped, all_evidence_texts


# ── Condition Status Inference ────────────────────────────────────────────

# Procedure keywords that resolve specific conditions
_PROCEDURE_RESOLVES = {
    # procedure keyword → (condition_key, inferred_status, auto_note)
    "patent foramen ovale closure": ("patent foramen ovale (pfo)", "resolved", "PFO closure procedure on record"),
    "pfo closure": ("patent foramen ovale (pfo)", "resolved", "PFO closure procedure on record"),
    "foramen ovale closure": ("patent foramen ovale (pfo)", "resolved", "PFO closure procedure on record"),
    "h. pylori": ("helicobacter pylori gastritis", "resolved", "Treatment on record"),
    "helicobacter": ("helicobacter pylori gastritis", "resolved", "Treatment on record"),
}

# Condition-text evidence: if ANY condition record (including filtered/transient
# ones like "S/P ..." entries) contains these phrases, infer the status.
# This catches cases where a procedure was only recorded as a condition, not as
# a Procedure resource.
_CONDITION_TEXT_EVIDENCE = [
    # (keyword_in_any_condition_text, target_condition_key, status, note)
    ("percutaneous patent foramen ovale closure", "patent foramen ovale (pfo)", "resolved", "Closure noted in medical record"),
    ("pfo closure", "patent foramen ovale (pfo)", "resolved", "Closure noted in medical record"),
    ("foramen ovale closure", "patent foramen ovale (pfo)", "resolved", "Closure noted in medical record"),
    ("foramen ovale repair", "patent foramen ovale (pfo)", "resolved", "Repair noted in medical record"),
]

# Lab LOINC codes / names that, when normal, resolve conditions
_LAB_RESOLVES = {
    # lab_name_keyword → (condition_key, loinc_codes[], what_normal_means)
    "protein": {
        "condition": "proteinuria",
        "loincs": ["2888-6", "5804-0", "21482-5"],  # urine protein variants
        "keywords": ["urine protein", "protein urine", "proteinuria"],
        "normal_resolves": True,
        "note": "Recent urine protein within normal range",
    },
}

# NOTE: We intentionally do NOT infer status from condition name text
# (e.g. "remission" in the name). Condition names like "remission status
# unspecified" are ambiguous or explicitly uncertain. Reliable inference
# comes only from concrete evidence: procedures performed and lab results.


async def _gather_all_procedures() -> list:
    """Fetch ALL procedures for inference (not just significant ones)."""
    try:
        bundle = await _fhir_get("Procedure", {
            "_count": "200",
            "_sort": "-date",
        })
        procs = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            code = res.get("code", {})
            text = code.get("text", "")
            if not text:
                codings = code.get("coding", [])
                text = next((c.get("display", "") for c in codings if c.get("display")), "")
            date = (res.get("performedDateTime") or res.get("performedPeriod", {}).get("start", "") or "")[:10]
            if text:
                procs.append({"text": text.lower(), "date": date})
        return procs
    except Exception:
        return []


def infer_condition_statuses(
    conditions: dict,
    procedures: list,
    labs: list,
    all_evidence_texts: list = None,
) -> dict:
    """Cross-reference conditions against procedures, labs, and condition-text
    evidence to infer statuses.
    Returns {condition_key: {"status": ..., "note": ..., "source": "inferred"}}.

    Does NOT override user overrides — caller applies those first.

    Args:
        conditions: deduped conditions dict from gather_conditions_raw()
        procedures: all Procedure resources from _gather_all_procedures()
        labs: lab results from gather_key_labs()
        all_evidence_texts: ALL raw condition texts (including filtered/transient
            ones like "S/P percutaneous patent foramen ovale closure") — these
            serve as evidence even though they're not displayed as conditions
    """
    inferred = {}
    evidence_texts = all_evidence_texts or []

    # 1. Procedure-based inference
    for proc in procedures:
        ptext = proc["text"]
        for kw, (cond_key, status, note) in _PROCEDURE_RESOLVES.items():
            if kw in ptext and cond_key in conditions:
                date_str = f" ({proc['date']})" if proc.get("date") else ""
                inferred[cond_key] = {
                    "status": status,
                    "note": note + date_str,
                    "source": "inferred",
                }

    # 1b. Condition-text evidence: scan ALL condition texts (including
    #     filtered/transient like "S/P ..." entries) for evidence of resolved conditions
    for ev_text in evidence_texts:
        for kw, cond_key, status, note in _CONDITION_TEXT_EVIDENCE:
            if kw in ev_text and cond_key in conditions and cond_key not in inferred:
                inferred[cond_key] = {
                    "status": status,
                    "note": note,
                    "source": "inferred",
                }

    # 2. Lab-based inference: if most recent lab is normal, resolve the condition
    for _rule_key, rule in _LAB_RESOLVES.items():
        cond_key = rule["condition"]
        if cond_key not in conditions:
            continue
        # Search labs for matching tests
        for lab in labs:
            if lab.get("error"):
                continue
            lab_name_lower = lab.get("name", "").lower()
            lab_loinc = lab.get("loinc", "")
            matched = False
            if lab_loinc in rule["loincs"]:
                matched = True
            else:
                for kw in rule["keywords"]:
                    if kw in lab_name_lower:
                        matched = True
                        break
            if matched and not lab.get("out_of_range", True) and rule["normal_resolves"]:
                date_str = f" ({lab.get('date', '')})" if lab.get("date") else ""
                inferred[cond_key] = {
                    "status": "resolved",
                    "note": rule["note"] + date_str,
                    "source": "inferred",
                }
                break  # found a normal result, done

    # 3. (Removed) Text hints from condition names — too unreliable.
    #    "remission status unspecified" is explicitly uncertain, not affirmative.
    #    We only infer from concrete evidence (procedures, labs).

    # 4. (Removed) CVA time-based inference — too speculative.
    #    A stroke may or may not have lasting effects; let the user decide.

    return inferred


def process_conditions(
    raw_conditions: dict,
    overrides: dict,
    inferred_statuses: dict,
) -> dict:
    """Categorize conditions and apply overrides + inferred statuses.
    Priority: user override > inferred status > active (default).

    Returns { "active": {cat: [...]}, "resolved": [...] }
    """
    active_by_category = defaultdict(list)
    resolved = []

    for key, c in raw_conditions.items():
        # Remove internal field from API output
        c.pop("original_texts", None)

        # 1. User override takes precedence
        override = overrides.get(key)
        if override:
            user_status = override["user_status"]
            c["user_status"] = user_status
            c["user_note"] = override.get("note", "")
            c["status_source"] = "user"
            if user_status != "active":
                resolved.append(c)
                continue
        # 2. Inferred status (auto)
        elif key in inferred_statuses:
            inf = inferred_statuses[key]
            c["user_status"] = inf["status"]
            c["user_note"] = inf["note"]
            c["status_source"] = "inferred"
            if inf["status"] != "active":
                resolved.append(c)
                continue

        # 3. Default: active
        category = _categorize_condition(c["condition"])
        c["category"] = category
        active_by_category[category].append(c)

    # Sort within each category
    for cat in active_by_category:
        active_by_category[cat].sort(
            key=lambda c: c.get("onset") or c.get("recorded") or "",
            reverse=True,
        )

    # Build ordered categories
    active_ordered = {}
    for cat in _CATEGORY_ORDER:
        if cat in active_by_category:
            active_ordered[cat] = active_by_category[cat]

    resolved.sort(key=lambda c: c.get("onset") or "", reverse=True)

    return {
        "active": active_ordered,
        "resolved": resolved,
    }


# ── Major Health Events ───────────────────────────────────────────────────

async def gather_health_events(limit: int = 20) -> list:
    """Fetch significant health events: procedures, hospitalizations, key encounters."""
    events = []

    # Procedures
    try:
        bundle = await _fhir_get("Procedure", {
            "_count": str(limit),
            "_sort": "-date",
        })
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            code = res.get("code", {})
            text = code.get("text", "")
            if not text:
                codings = code.get("coding", [])
                text = next((c.get("display", "") for c in codings if c.get("display")), "")
            date = (res.get("performedDateTime") or res.get("performedPeriod", {}).get("start", "") or "")[:10]
            status = res.get("status", "")
            if text:
                events.append({
                    "type": "procedure",
                    "description": text,
                    "date": date,
                    "status": status,
                })
    except Exception:
        pass

    # Significant encounters (inpatient, emergency)
    try:
        bundle = await _fhir_get("Encounter", {
            "_count": str(limit),
            "_sort": "-date",
        })
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            enc_class = res.get("class", {})
            class_code = enc_class.get("code", "")
            # Only include inpatient, emergency, or observation
            if class_code not in ("IMP", "EMER", "OBSENC", "inpatient", "emergency"):
                # Also check type text for keywords
                type_text = ""
                for t in res.get("type", []):
                    type_text += t.get("text", "") + " "
                    for c in t.get("coding", []):
                        type_text += c.get("display", "") + " "
                type_lower = type_text.lower()
                if not any(kw in type_lower for kw in ("hospital", "emergency", "inpatient", "surgery", "transplant", "infusion", "chemotherapy")):
                    continue

            # Get encounter description
            types = res.get("type", [])
            description = ""
            for t in types:
                description = t.get("text", "")
                if not description:
                    codings = t.get("coding", [])
                    description = next((c.get("display", "") for c in codings if c.get("display")), "")
                if description:
                    break
            if not description:
                description = f"Encounter ({class_code})"

            period = res.get("period", {})
            start = (period.get("start") or "")[:10]
            end = (period.get("end") or "")[:10]

            reason = ""
            for rc in res.get("reasonCode", []):
                reason = rc.get("text", "")
                if reason:
                    break

            if description or reason:
                events.append({
                    "type": "encounter",
                    "description": reason or description,
                    "date": start,
                    "end_date": end,
                    "status": res.get("status", ""),
                })
    except Exception:
        pass

    # Sort all events by date, most recent first
    events.sort(key=lambda e: e.get("date", ""), reverse=True)
    return events[:limit]


# ── Key Lab Trends (most recent abnormal or notable) ──────────────────────

async def gather_key_labs() -> list:
    """Fetch recent lab results, highlighting abnormal values."""
    try:
        bundle = await _fhir_get("Observation", {
            "category": "laboratory",
            "_count": "100",
            "_sort": "-date",
        })

        # Group by test name, keep most recent per test
        by_test = {}
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            code = res.get("code", {})
            display = code.get("text", "")
            loinc = ""
            for c in code.get("coding", []):
                if c.get("system", "").endswith("loinc.org"):
                    loinc = c.get("code", "")
                    if not display:
                        display = c.get("display", "")
                    break
            if not display:
                continue

            key = loinc or display.lower()
            if key in by_test:
                continue  # already have most recent

            vq = res.get("valueQuantity", {})
            value = vq.get("value")
            unit = vq.get("unit", "")
            if value is None:
                continue

            date = (res.get("effectiveDateTime") or "")[:10]
            ref_lo, ref_hi = None, None
            ranges = res.get("referenceRange", [])
            if ranges:
                ref_lo = ranges[0].get("low", {}).get("value")
                ref_hi = ranges[0].get("high", {}).get("value")

            out_of_range = False
            if ref_lo is not None and value < ref_lo:
                out_of_range = True
            if ref_hi is not None and value > ref_hi:
                out_of_range = True

            by_test[key] = {
                "name": display,
                "loinc": loinc,
                "value": value,
                "unit": unit,
                "date": date,
                "ref_low": ref_lo,
                "ref_high": ref_hi,
                "out_of_range": out_of_range,
            }

        # Return all, sorted: abnormal first, then by date
        abnormal = [l for l in by_test.values() if l["out_of_range"]]
        normal = [l for l in by_test.values() if not l["out_of_range"]]
        abnormal.sort(key=lambda l: l["date"], reverse=True)
        normal.sort(key=lambda l: l["date"], reverse=True)
        return abnormal + normal[:10]  # All abnormal + top 10 normal

    except Exception as e:
        return [{"error": str(e)}]


# ── Condition Override Endpoints ───────────────────────────────────────────

USER_STATUSES = ("active", "resolved", "in_remission", "managed", "inactive")
STATUS_LABELS = {
    "active": "Active",
    "resolved": "Resolved",
    "in_remission": "In remission",
    "managed": "Managed by medication",
    "inactive": "Inactive",
}


@router.put("/condition")
async def set_condition_override(request: Request):
    """Set user override for a condition's status.
    Expects JSON: { condition_key: str, user_status: str, note?: str }
    """
    body = await request.json()
    key = body.get("condition_key", "").strip().lower()
    status = body.get("user_status", "").strip().lower()
    note = body.get("note", "").strip()

    if not key:
        return {"error": "condition_key is required"}
    if status not in USER_STATUSES:
        return {"error": f"user_status must be one of: {', '.join(USER_STATUSES)}"}

    db = await _get_db()
    now = datetime.now(timezone.utc).isoformat()

    if status == "active":
        # Remove override — revert to FHIR's active status
        await db.execute("DELETE FROM condition_overrides WHERE condition_key = ?", (key,))
    else:
        await db.execute(
            """INSERT INTO condition_overrides (condition_key, user_status, note, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(condition_key) DO UPDATE SET user_status=excluded.user_status, note=excluded.note, updated_at=excluded.updated_at""",
            (key, status, note, now),
        )
    await db.commit()
    return {"ok": True, "condition_key": key, "user_status": status}


@router.delete("/condition")
async def remove_condition_override(request: Request):
    """Remove a condition override (revert to FHIR active status).
    Expects JSON: { condition_key: str }
    """
    body = await request.json()
    key = body.get("condition_key", "").strip().lower()
    if not key:
        return {"error": "condition_key is required"}

    db = await _get_db()
    await db.execute("DELETE FROM condition_overrides WHERE condition_key = ?", (key,))
    await db.commit()
    return {"ok": True, "condition_key": key}


# ── Main Profile Endpoint ─────────────────────────────────────────────────

@router.get("")
async def get_profile(units: str = Query("imperial", regex="^(imperial|metric)$")):
    """Return complete patient profile: demographics, vitals, conditions, events, labs."""
    errors = []

    async def safe(name, coro):
        try:
            return await coro
        except Exception as e:
            errors.append(f"{name}: {str(e)}")
            print(f"[profile] Error in {name}: {e}", flush=True)
            return {} if name in ("demographics", "vitals", "conditions_raw") else []

    # Gather all data in parallel — including all procedures for inference
    (demographics, vitals, conditions_result, events, labs,
     all_procedures, overrides) = await asyncio.gather(
        safe("demographics", gather_demographics()),
        safe("vitals", gather_recent_vitals(unit_system=units)),
        safe("conditions_raw", gather_conditions_raw()),
        safe("events", gather_health_events()),
        safe("labs", gather_key_labs()),
        safe("all_procedures", _gather_all_procedures()),
        safe("overrides", get_condition_overrides()),
    )

    # Unpack conditions — gather_conditions_raw returns (grouped_dict, evidence_texts)
    if isinstance(conditions_result, tuple):
        conditions_raw, all_evidence_texts = conditions_result
    else:
        conditions_raw = conditions_result if isinstance(conditions_result, dict) else {}
        all_evidence_texts = []

    # Infer condition statuses from procedures, labs, and condition-text evidence
    try:
        inferred = infer_condition_statuses(
            conditions_raw,
            all_procedures if isinstance(all_procedures, list) else [],
            labs if isinstance(labs, list) else [],
            all_evidence_texts,
        )
        if inferred:
            print(f"[profile] Auto-inferred statuses: {list(inferred.keys())}", flush=True)
    except Exception as e:
        inferred = {}
        errors.append(f"inference: {str(e)}")
        print(f"[profile] Inference error: {e}", flush=True)

    # Process: categorize, apply overrides, apply inference
    try:
        conditions = process_conditions(
            conditions_raw if isinstance(conditions_raw, dict) else {},
            overrides if isinstance(overrides, dict) else {},
            inferred,
        )
    except Exception as e:
        conditions = {"active": {}, "resolved": []}
        errors.append(f"process_conditions: {str(e)}")
        print(f"[profile] process_conditions error: {e}", flush=True)

    cond_summary = f"active_cats={len(conditions.get('active', {}))}, resolved={len(conditions.get('resolved', []))}, inferred={len(inferred)}"

    print(f"[profile] Loaded: demographics={bool(demographics)}, vitals={len(vitals) if isinstance(vitals, dict) else 0}, "
          f"{cond_summary}, events={len(events) if isinstance(events, list) else 0}, "
          f"labs={len(labs) if isinstance(labs, list) else 0}, errors={errors}", flush=True)

    return {
        "demographics": demographics,
        "vitals": vitals,
        "conditions": conditions,
        "health_events": events,
        "key_labs": labs,
        "unit_system": units,
        "status_labels": STATUS_LABELS,
        **({"errors": errors} if errors else {}),
    }
