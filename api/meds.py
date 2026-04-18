"""PHV Meds — Medication management with reconciliation and user overrides.

Provides:
  - GET  /api/meds              — list all medications (grouped, with overrides applied)
  - PUT  /api/meds/override     — set user override for a medication's status
  - DELETE /api/meds/override   — remove a user override
  - GET  /api/meds/overrides    — list all user overrides
  - POST /api/meds/add          — add a patient-reported medication
  - GET  /api/meds/search       — search RxNorm for medication names
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from fastapi import APIRouter, Request

logger = logging.getLogger("meds")
router = APIRouter(prefix="/api/meds", tags=["meds"])

# ── Configuration ──────────────────────────────────────────────────────────
HAPI_BASE = os.environ.get("HAPI_BASE", "http://hapi:8080/fhir")
DB_PATH = os.environ.get("ASSISTANT_DB", "/data/chat.db")
RXNORM_BASE = "https://rxnav.nlm.nih.gov/REST"

# ── HTTP Clients ──────────────────────────────────────────────────────────
_fhir_client: Optional[httpx.AsyncClient] = None
_rxnorm_client: Optional[httpx.AsyncClient] = None


async def get_fhir_client() -> httpx.AsyncClient:
    global _fhir_client
    if _fhir_client is None or _fhir_client.is_closed:
        _fhir_client = httpx.AsyncClient(base_url=HAPI_BASE, timeout=30.0)
    return _fhir_client


async def get_rxnorm_client() -> httpx.AsyncClient:
    global _rxnorm_client
    if _rxnorm_client is None or _rxnorm_client.is_closed:
        _rxnorm_client = httpx.AsyncClient(base_url=RXNORM_BASE, timeout=15.0)
    return _rxnorm_client


# ── Database (user overrides) ────────────────────────────────────────────
_db: Optional[aiosqlite.Connection] = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.executescript("""
            CREATE TABLE IF NOT EXISTS med_overrides (
                med_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('taking', 'not_taking', 'as_needed')),
                notes TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS med_synonyms (
                med_key TEXT PRIMARY KEY,
                rxcui TEXT DEFAULT '',
                generic_name TEXT DEFAULT '',
                brand_names TEXT DEFAULT '',
                synonyms TEXT DEFAULT '',
                fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS med_aliases (
                alias_key TEXT PRIMARY KEY,
                canonical_key TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        # Migration: add dosage/frequency columns if missing
        try:
            await _db.execute("SELECT user_dosage FROM med_overrides LIMIT 0")
        except Exception:
            await _db.execute("ALTER TABLE med_overrides ADD COLUMN user_dosage TEXT DEFAULT ''")
            await _db.execute("ALTER TABLE med_overrides ADD COLUMN user_frequency TEXT DEFAULT ''")
            await _db.commit()
        await _db.commit()
    return _db


# ── RxNorm Synonym Lookup ────────────────────────────────────────────────

async def lookup_rxnorm_synonyms(name: str, rxcui: str = "") -> dict:
    """Look up brand/generic names from RxNorm. Returns cached result if available."""
    norm = normalize_med_name(name)
    db = await get_db()

    # Check cache first
    cursor = await db.execute("SELECT * FROM med_synonyms WHERE med_key = ?", (norm,))
    cached = await cursor.fetchone()
    if cached and (cached["brand_names"] or cached["generic_name"] or cached["rxcui"]):
        return {
            "generic_name": cached["generic_name"],
            "brand_names": cached["brand_names"],
            "synonyms": cached["synonyms"],
            "rxcui": cached["rxcui"],
        }
    # If cached but all empty, treat as uncached (retry the lookup)
    if cached:
        await db.execute("DELETE FROM med_synonyms WHERE med_key = ?", (norm,))
        await db.commit()

    # Not cached — look up from RxNorm
    result = {"generic_name": "", "brand_names": "", "synonyms": "", "rxcui": rxcui}

    try:
        client = await get_rxnorm_client()

        # Step 1: Get RXCUI if we don't have one
        if not rxcui:
            r = await client.get("/rxcui.json", params={"name": norm, "search": "2"})
            if r.status_code == 200:
                data = r.json()
                ids = data.get("idGroup", {}).get("rxnormId", [])
                if ids:
                    rxcui = ids[0]
                    result["rxcui"] = rxcui

        # If still no RXCUI, try approximate match
        if not rxcui:
            r = await client.get("/approximateTerm.json", params={"term": name, "maxEntries": "1"})
            if r.status_code == 200:
                candidates = r.json().get("approximateGroup", {}).get("candidate", [])
                if candidates:
                    rxcui = candidates[0].get("rxcui", "")
                    result["rxcui"] = rxcui

        if not rxcui:
            # Cache empty result to avoid re-lookups
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT OR REPLACE INTO med_synonyms (med_key, rxcui, generic_name, brand_names, synonyms, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                (norm, "", "", "", "", now),
            )
            await db.commit()
            return result

        # Step 2: Get ALL related concepts, then filter to BN (brand) and IN (ingredient)
        # Using allrelated instead of related because the RXCUI may be at any level
        # (ingredient, clinical drug, etc.) and allrelated traverses all relationship types
        r = await client.get(f"/rxcui/{rxcui}/allrelated.json")
        if r.status_code == 200:
            data = r.json()
            brand_names = set()
            generic_names = set()

            for group in data.get("allRelatedGroup", {}).get("conceptGroup", []):
                tty = group.get("tty", "")
                for prop in group.get("conceptProperties", []):
                    concept_name = prop.get("name", "")
                    if not concept_name:
                        continue
                    if tty == "BN":
                        brand_names.add(concept_name)
                    elif tty == "IN":
                        generic_names.add(concept_name)

            # Limit to top 3 each to keep it clean
            result["brand_names"] = ", ".join(sorted(brand_names)[:3]) if brand_names else ""
            result["generic_name"] = ", ".join(sorted(generic_names)[:3]) if generic_names else ""

    except Exception as e:
        logger.warning(f"RxNorm lookup failed for {name}: {e}")


    # Cache the result (only cache if we got something useful, or if no RXCUI was found)
    if not result["rxcui"] and not result["brand_names"] and not result["generic_name"]:
        # Don't cache complete failures — might be a transient network issue
        return result
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO med_synonyms (med_key, rxcui, generic_name, brand_names, synonyms, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (norm, result["rxcui"], result["generic_name"], result["brand_names"], result.get("synonyms", ""), now),
    )
    await db.commit()

    return result


async def enrich_medications_with_synonyms(medications: list) -> list:
    """Enrich a list of medication dicts with synonym info. Runs lookups in parallel."""
    async def enrich_one(med):
        try:
            synonyms = await lookup_rxnorm_synonyms(med["display"], med.get("rxnorm_code", ""))
            med["generic_name"] = synonyms.get("generic_name", "")
            med["brand_names"] = synonyms.get("brand_names", "")
        except Exception:
            pass
        return med

    # Run up to 5 concurrent lookups to avoid hammering the API
    import asyncio
    semaphore = asyncio.Semaphore(5)

    async def limited_enrich(med):
        async with semaphore:
            return await enrich_one(med)

    await asyncio.gather(*[limited_enrich(m) for m in medications])
    return medications


# ── Helpers ──────────────────────────────────────────────────────────────

def normalize_med_name(name: str) -> str:
    """Normalize medication name for grouping/matching.

    Goals: group the same drug across different salt forms, dosage forms,
    and pharmacy naming conventions. E.g. "doxycycline monohydrate 50 mg tablet"
    and "doxycycline 50 mg" → "doxycycline".
    """
    n = name.lower().strip()
    # Remove parenthetical/bracket content like "(50 mg/5 gram)" or "[Nizoral]"
    n = re.sub(r'\s*[\(\[].*?[\)\]]', '', n).strip()
    # Strip at comma — in pharmacy data, commas separate drug name from
    # compounding/misc info: "TESTOSTERONE CYP, MICRO (BULK) MISC" → "testosterone cyp"
    if ',' in n:
        n = n.split(',')[0].strip()
    # Remove percentage prefixes like "0.9%"
    n = re.sub(r'^[\d.]+%\s*', '', n).strip()
    # Remove inline percentages like "testosterone 1 %" → "testosterone"
    n = re.sub(r'\s+\d+\s*%', '', n).strip()
    # Remove dose/form info: split at first digit sequence
    n = re.split(r'\s+\d', n)[0].strip()
    # Remove trailing form words and salt forms (longest first, loop until stable)
    suffixes = sorted([
        # Dosage forms
        ' tablet', ' capsule', ' tab', ' cap', ' oral',
        ' er', ' xl', ' sr', ' dr', ' cr',
        ' injection', ' solution', ' suspension', ' cream',
        ' gel', ' ointment', ' patch', ' spray', ' inhaler',
        ' flush', ' packet', ' powder', ' shampoo',
        ' medicated shampoo', ' topical', ' medicated',
        ' ophthalmic', ' nasal', ' rectal', ' vaginal',
        ' lotion', ' foam', ' drops', ' suppository',
        # Pharmaceutical salt forms (full names)
        ' hcl', ' hydrochloride', ' monohydrate', ' dihydrate',
        ' sulfate', ' sulphate', ' sodium', ' potassium', ' calcium',
        ' mesylate', ' besylate', ' tartrate', ' fumarate',
        ' maleate', ' succinate', ' acetate', ' phosphate',
        ' citrate', ' nitrate', ' bromide', ' chloride',
        ' disodium', ' magnesium', ' lactate', ' gluconate',
        # Pharmaceutical salt abbreviations (common in Epic/pharmacy data)
        ' cyp',  # cypionate
        ' prop', # propionate
        ' enan', # enanthate
        ' sod',  # sodium
        ' pot',  # potassium
        ' phos', # phosphate
        # Pharmacy jargon
        ' misc', ' bulk', ' micro', ' compounding', ' compound',
        ' in packet', ' in water', ' in sterile water',
        ' bolus', ' infusion', ' iv', ' subcut',
    ], key=len, reverse=True)
    # Words that are chemical/element names — if stripping a salt suffix would
    # leave only one of these, don't strip (the "salt" IS the drug).
    # E.g. "sodium phosphate" should stay, not become "sodium".
    chemical_bases = {
        'sodium', 'potassium', 'calcium', 'magnesium', 'lithium',
        'iron', 'zinc', 'copper', 'selenium', 'chromium', 'manganese',
        'phosphate', 'chloride', 'fluoride', 'bromide', 'sulfate',
    }
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if n.endswith(suffix):
                candidate = n[:-len(suffix)].strip()
                # Don't strip if the remainder is just a chemical element/ion
                if candidate in chemical_bases:
                    continue
                n = candidate
                changed = True
                break
    return n


def _clean_dosage_text(text: str) -> str:
    """Clean up Epic/FHIR dosage instruction text.

    Removes trailing noise like ', Historical Med', ', Patient Reported',
    and extraneous metadata that isn't useful to the patient.
    """
    # Remove common Epic trailing tags
    noise_suffixes = [
        ', Historical Med', ', Historical Medication',
        ', Patient Reported', ', Patient reported',
        ', Outpatient', ', Inpatient',
        '. Historical Med', '. Historical Medication',
    ]
    for suffix in noise_suffixes:
        if text.endswith(suffix):
            text = text[:-len(suffix)].rstrip('.,')
    # Also strip if these appear anywhere as standalone segments
    for tag in ['Historical Med', 'Historical Medication', 'Patient Reported']:
        text = re.sub(r',?\s*' + re.escape(tag) + r'\s*$', '', text)
        text = re.sub(r',?\s*' + re.escape(tag) + r'\s*,', ',', text)
    # Remove "DAW" (Dispense As Written) — pharmacy noise
    text = re.sub(r'\s*[·•]\s*DAW\b', '', text)
    # Clean up trailing punctuation
    text = text.rstrip('., ')
    return text.strip()


def extract_med_info(resource: dict) -> Optional[dict]:
    """Extract medication info from a FHIR MedicationRequest resource."""
    display = resource.get("medicationReference", {}).get("display", "")
    if not display:
        cc = resource.get("medicationCodeableConcept", {})
        display = cc.get("text", "")
        if not display:
            codings = cc.get("coding", [])
            display = next((c.get("display", "") for c in codings if c.get("display")), "")
    if not display:
        for c in resource.get("contained", []):
            if c.get("resourceType") == "Medication":
                display = c.get("code", {}).get("text", "")
                break
    if not display:
        return None

    # Extract RxNorm code if available
    rxnorm_code = ""
    cc = resource.get("medicationCodeableConcept", {})
    for coding in cc.get("coding", []):
        if "rxnorm" in coding.get("system", "").lower():
            rxnorm_code = coding.get("code", "")
            break
    # Also check medicationReference contained resources
    if not rxnorm_code:
        for c in resource.get("contained", []):
            if c.get("resourceType") == "Medication":
                for coding in c.get("code", {}).get("coding", []):
                    if "rxnorm" in coding.get("system", "").lower():
                        rxnorm_code = coding.get("code", "")
                        break

    status = resource.get("status", "")
    authored = resource.get("authoredOn") or ""
    dosage = ""
    frequency = ""
    if resource.get("dosageInstruction"):
        di = resource["dosageInstruction"][0]
        dosage = di.get("text", "")
        # Clean up Epic noise from dosage text
        if dosage:
            dosage = _clean_dosage_text(dosage)
        # Try to extract structured frequency
        timing = di.get("timing", {})
        repeat = timing.get("repeat", {})
        if repeat.get("frequency") and repeat.get("period"):
            freq = repeat["frequency"]
            period = repeat["period"]
            period_unit = repeat.get("periodUnit", "")
            unit_map = {"d": "day", "wk": "week", "mo": "month"}
            frequency = f"{freq}x per {unit_map.get(period_unit, period_unit)}"

    requester = resource.get("requester", {}).get("display", "")

    # Check if patient-reported
    is_patient_reported = False
    for tag in resource.get("meta", {}).get("tag", []):
        if tag.get("code") == "patient-reported":
            is_patient_reported = True
            break

    return {
        "id": resource.get("id", ""),
        "display": display,
        "normalized": normalize_med_name(display),
        "status": status,
        "authored": authored,
        "ordered": authored[:10],
        "dosage": dosage,
        "frequency": frequency,
        "prescriber": requester,
        "rxnorm_code": rxnorm_code,
        "patient_reported": is_patient_reported,
    }


async def get_overrides() -> dict:
    """Load all user overrides keyed by med_key."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM med_overrides")
    rows = await cursor.fetchall()
    return {r["med_key"]: dict(r) for r in rows}


async def get_aliases() -> dict:
    """Load all user merge aliases: alias_key → canonical_key."""
    db = await get_db()
    cursor = await db.execute("SELECT alias_key, canonical_key FROM med_aliases")
    rows = await cursor.fetchall()
    return {r["alias_key"]: r["canonical_key"] for r in rows}


def resolve_key(norm_key: str, aliases: dict) -> str:
    """Resolve a normalized key through the alias chain (max 3 hops)."""
    for _ in range(3):
        if norm_key in aliases:
            norm_key = aliases[norm_key]
        else:
            break
    return norm_key


# ── Routes ───────────────────────────────────────────────────────────────

async def get_grouped_medications() -> list[dict]:
    """Core medication grouping logic — reusable by analyst/assistant.

    Returns the same grouped, deduplicated, override-applied medication list
    that the Meds tab UI shows. This is the single source of truth for
    'what medications does this patient have'.
    """
    return (await _build_medication_list())["medications"]


async def _build_medication_list() -> dict:
    """Internal: build the full grouped medication response."""
    client = await get_fhir_client()

    # Fetch ALL MedicationRequests, paginating through all pages
    all_records = []
    seen_ids = set()
    patient_reported_count = 0
    page_count = 0

    next_url = "/MedicationRequest"
    next_params = {"_count": "200", "_sort": "-date"}

    try:
        while next_url:
            page_count += 1
            if page_count == 1:
                r = await client.get(next_url, params=next_params)
            else:
                # Subsequent pages: HAPI returns full URLs in bundle links
                # We need to make the request relative to the FHIR base
                r = await client.get(next_url)
            r.raise_for_status()
            bundle = r.json()

            for entry in bundle.get("entry", []):
                res = entry.get("resource", {})
                rid = res.get("id", "")
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                info = extract_med_info(res)
                if info:
                    all_records.append(info)
                    if info["patient_reported"]:
                        patient_reported_count += 1
                        print(f"[meds] Found patient-reported med: {info['display']} (id={info['id']}, norm={info['normalized']})", flush=True)

            # Check for next page link
            next_url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    full_url = link.get("url", "")
                    # Convert absolute URL to relative path for our client
                    if full_url:
                        # HAPI returns URLs like http://hapi:8080/fhir/MedicationRequest?...
                        from urllib.parse import urlparse
                        parsed = urlparse(full_url)
                        next_url = parsed.path.replace("/fhir", "", 1) + "?" + parsed.query if parsed.query else parsed.path.replace("/fhir", "", 1)
                    break

            # Safety: don't paginate forever
            if page_count >= 10:
                print(f"[meds] WARNING: Hit pagination limit (10 pages)", flush=True)
                break

        print(f"[meds] List: {len(all_records)} records from {page_count} page(s), {patient_reported_count} patient-reported", flush=True)
    except Exception as e:
        logger.error(f"Failed to fetch medications: {e}")
        return {"error": str(e), "medications": []}

    # Load user aliases and overrides
    aliases = await get_aliases()
    overrides = await get_overrides()

    # Group by normalized name (applying user merge aliases)
    by_name = defaultdict(list)
    for rec in all_records:
        key = resolve_key(rec["normalized"], aliases)
        rec["_group_key"] = key  # track which group this ended up in
        by_name[key].append(rec)

    # Build grouped medication list
    medications = []
    for norm_name, records in sorted(by_name.items()):
        records.sort(key=lambda r: r["authored"], reverse=True)

        # Detect status conflicts
        statuses = set(r["status"] for r in records if r["status"])
        has_conflict = (
            len(statuses) > 1
            and "active" in statuses
            and statuses & {"stopped", "completed", "cancelled"}
        )

        # Best display name (most detailed, from most recent record)
        best_display = records[0]["display"]

        # Most recent dosage info
        dosage = ""
        frequency = ""
        for rec in records:
            if rec["dosage"]:
                dosage = rec["dosage"]
                break
        for rec in records:
            if rec["frequency"]:
                frequency = rec["frequency"]
                break

        # Check for user override
        override = overrides.get(norm_name)
        user_status = override["status"] if override else None
        user_notes = override["notes"] if override else ""
        user_dosage = override["user_dosage"] if override and override.get("user_dosage") else ""
        user_frequency = override["user_frequency"] if override and override.get("user_frequency") else ""

        # Determine effective display status
        if user_status:
            effective_status = user_status
        elif has_conflict:
            effective_status = "conflicting"
        else:
            # Use FHIR status from most recent record
            effective_status = records[0]["status"]

        # Any RxNorm code from any record
        rxnorm = ""
        for rec in records:
            if rec["rxnorm_code"]:
                rxnorm = rec["rxnorm_code"]
                break

        # Is patient-reported?
        patient_reported = any(rec["patient_reported"] for rec in records)

        medications.append({
            "key": norm_name,
            "display": best_display,
            "effective_status": effective_status,
            "has_conflict": has_conflict,
            "user_override": user_status,
            "user_notes": user_notes,
            "dosage": dosage,
            "frequency": frequency,
            "user_dosage": user_dosage,
            "user_frequency": user_frequency,
            "rxnorm_code": rxnorm,
            "patient_reported": patient_reported,
            "record_count": len(records),
            "most_recent_order": records[0]["ordered"],
            "prescriber": records[0]["prescriber"],
            "history": [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "ordered": r["ordered"],
                    "dosage": r["dosage"],
                    "prescriber": r["prescriber"],
                }
                for r in records
            ],
        })

    # Sort: user-confirmed taking first, then active, then conflicting, then stopped
    # Sort alphabetically by normalized name
    medications.sort(key=lambda m: m["key"])

    # Enrich with RxNorm synonyms (uses cache, background-friendly)
    await enrich_medications_with_synonyms(medications)

    # Detect near-duplicates: medications where one key is an exact prefix of
    # the other followed by a space (i.e. the longer key adds qualifier words).
    # E.g. "ondansetron" / "ondansetron odt" → likely same drug.
    # But NOT "sodium phosphate" / "sodium chloride" — different second word.
    all_keys = [m["key"] for m in medications]
    for med in medications:
        dupes = []
        k = med["key"]
        for other in all_keys:
            if other == k:
                continue
            shorter, longer = (k, other) if len(k) <= len(other) else (other, k)
            # The shorter key must be the full base name, and the longer key
            # must start with it followed by a space (or be identical length).
            if len(shorter) >= 4 and (
                longer == shorter or
                longer.startswith(shorter + " ") or
                longer.startswith(shorter + "-")
            ):
                dupes.append(other)
        med["possible_duplicates"] = dupes[:3] if dupes else []

    return {
        "medications": medications,
        "total": len(medications),
        "overrides_count": len(overrides),
    }


@router.get("")
async def list_medications():
    """List all medications, grouped by drug with user overrides applied."""
    return await _build_medication_list()


@router.put("/override")
async def set_override(request: Request):
    """Set a user override for a medication's status and/or dosage.

    Expects JSON:
      { "med_key": "losartan", "display_name": "Losartan 25 mg",
        "status": "taking|not_taking|as_needed", "notes": "",
        "user_dosage": "25 mg", "user_frequency": "once daily" }
    """
    body = await request.json()
    raw_key = body.get("med_key", "").strip()
    display_name = body.get("display_name", "").strip()
    status = body.get("status", "").strip()
    notes = body.get("notes", "").strip()
    user_dosage = body.get("user_dosage", "").strip()
    user_frequency = body.get("user_frequency", "").strip()

    if not raw_key:
        return {"error": "med_key is required"}

    # Normalize so assistant-provided keys match the grouping logic
    aliases = await get_aliases()
    med_key = resolve_key(normalize_med_name(raw_key), aliases)

    # If only dosage/frequency are being updated (no status change), keep existing status
    if not status or status not in ("taking", "not_taking", "as_needed"):
        db_tmp = await get_db()
        cursor = await db_tmp.execute(
            "SELECT status FROM med_overrides WHERE med_key = ?", (med_key,)
        )
        row = await cursor.fetchone()
        if row and row["status"]:
            status = row["status"]
        elif not status or status not in ("taking", "not_taking", "as_needed"):
            # Default: if FHIR says active, treat as taking
            status = "taking"

    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO med_overrides (med_key, display_name, status, notes, user_dosage, user_frequency, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(med_key) DO UPDATE SET
             display_name = excluded.display_name,
             status = excluded.status,
             notes = excluded.notes,
             user_dosage = excluded.user_dosage,
             user_frequency = excluded.user_frequency,
             updated_at = excluded.updated_at""",
        (med_key, display_name, status, notes, user_dosage, user_frequency, now),
    )
    await db.commit()
    return {"ok": True, "med_key": med_key, "status": status}


@router.delete("/override")
async def delete_override(request: Request):
    """Remove a user override.

    Expects JSON: { "med_key": "losartan" }
    """
    body = await request.json()
    med_key = body.get("med_key", "").strip()
    if not med_key:
        return {"error": "med_key is required"}

    db = await get_db()
    await db.execute("DELETE FROM med_overrides WHERE med_key = ?", (med_key,))
    await db.commit()
    return {"ok": True}


@router.get("/overrides")
async def list_overrides():
    """List all user overrides."""
    overrides = await get_overrides()
    return {"overrides": list(overrides.values())}


@router.get("/search")
async def search_medications(q: str = "", limit: int = 15):
    """Search for medications using RxNorm API.

    Uses a multi-strategy approach:
    1. RxNorm spelling suggestions (good for partial words like "ketocon")
    2. RxNorm approximate term match
    3. For ingredient-level hits, also fetches common drug forms and brand names
    4. Existing FHIR medications
    """
    if not q or len(q) < 2:
        return {"results": []}

    results = []
    seen_normalized = set()  # dedup by normalized name

    def _add_result(name: str, rxcui: str, source: str):
        """Add a result, deduplicating by normalized name."""
        norm = normalize_med_name(name)
        if norm in seen_normalized:
            return False
        seen_normalized.add(norm)
        results.append({"name": name, "rxcui": rxcui, "source": source})
        return True

    try:
        client = await get_rxnorm_client()

        # ── Strategy 1: Spelling suggestions (handles partial words well) ──
        try:
            r = await client.get("/spellingsuggestions.json", params={"name": q})
            if r.status_code == 200:
                suggestions = r.json().get("suggestionGroup", {}).get("suggestionList", {}).get("suggestion", [])
                for suggestion in suggestions[:5]:
                    # Get the RXCUI for this suggestion
                    r2 = await client.get("/rxcui.json", params={"name": suggestion, "search": "2"})
                    if r2.status_code == 200:
                        ids = r2.json().get("idGroup", {}).get("rxnormId", [])
                        if ids:
                            _add_result(suggestion, ids[0], "rxnorm")
        except Exception:
            pass

        # ── Strategy 2: Approximate term match ──
        try:
            r = await client.get("/approximateTerm.json", params={"term": q, "maxEntries": "5"})
            if r.status_code == 200:
                candidates = r.json().get("approximateGroup", {}).get("candidate", [])
                for cand in candidates:
                    rxcui = cand.get("rxcui", "")
                    if not rxcui:
                        continue
                    r2 = await client.get(f"/rxcui/{rxcui}/properties.json")
                    if r2.status_code == 200:
                        props = r2.json().get("properties", {})
                        name = props.get("name", "")
                        tty = props.get("tty", "")
                        if name:
                            _add_result(name, rxcui, "rxnorm")
        except Exception:
            pass

        # ── Strategy 3: Expand ingredient/brand hits to show drug forms ──
        # If we have results, look up the first one's related concepts to show
        # common forms (cream, shampoo, tablet) and brand names
        if results and len(results) < limit:
            first_rxcui = results[0].get("rxcui", "")
            if first_rxcui:
                try:
                    r = await client.get(f"/rxcui/{first_rxcui}/related.json",
                                         params={"tty": "BN+SCDF+SBDF"})
                    if r.status_code == 200:
                        for group in r.json().get("relatedGroup", {}).get("conceptGroup", []):
                            tty = group.get("tty", "")
                            for prop in group.get("conceptProperties", []):
                                name = prop.get("name", "")
                                rxcui = prop.get("rxcui", "")
                                if name and rxcui:
                                    # Brand names are high-value, add them
                                    if tty == "BN":
                                        _add_result(name, rxcui, "rxnorm")
                                    # Drug forms (SCDF) are useful — show without dose
                                    elif tty == "SCDF":
                                        _add_result(name, rxcui, "rxnorm")
                                    # Branded drug forms (SBDF) only if we need more
                                    elif tty == "SBDF" and len(results) < limit:
                                        _add_result(name, rxcui, "rxnorm")
                                if len(results) >= limit:
                                    break
                            if len(results) >= limit:
                                break
                except Exception:
                    pass

    except Exception as e:
        logger.warning(f"RxNorm search failed: {e}")

    # ── Strategy 4: Search existing FHIR medications ──
    try:
        fhir = await get_fhir_client()
        r = await fhir.get("/MedicationRequest", params={"_content": q, "_count": "10"})
        r.raise_for_status()
        bundle = r.json()
        for entry in bundle.get("entry", []):
            info = extract_med_info(entry.get("resource", {}))
            if info:
                _add_result(info["display"], info["rxnorm_code"], "existing")
    except Exception as e:
        logger.warning(f"FHIR medication search failed: {e}")

    # Sort: exact/prefix matches first, then existing records, then others
    q_lower = q.lower()
    def sort_key(r):
        name_lower = r["name"].lower()
        if name_lower == q_lower:
            return (0, name_lower)
        elif name_lower.startswith(q_lower):
            return (1, name_lower)
        elif r["source"] == "existing":
            return (2, name_lower)
        else:
            return (3, name_lower)

    results.sort(key=sort_key)
    return {"results": results[:limit], "query": q}


@router.delete("/synonym-cache")
async def clear_synonym_cache():
    """Clear the synonym cache to force re-fetch from RxNorm."""
    db = await get_db()
    await db.execute("DELETE FROM med_synonyms")
    await db.commit()
    return {"ok": True}


@router.post("/merge")
async def merge_medications(request: Request):
    """Merge two medication cards by creating an alias.

    Expects JSON:
      { "source_key": "doxycycline monohydrate", "target_key": "doxycycline" }

    After merging, records under source_key will appear under target_key.
    """
    body = await request.json()
    source = body.get("source_key", "").strip()
    target = body.get("target_key", "").strip()
    if not source or not target or source == target:
        return {"error": "source_key and target_key must be different non-empty strings"}

    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO med_aliases (alias_key, canonical_key, created_at) VALUES (?, ?, ?)",
        (source, target, now),
    )
    # Also migrate any override from source to target if target doesn't have one
    cursor = await db.execute("SELECT * FROM med_overrides WHERE med_key = ?", (source,))
    source_override = await cursor.fetchone()
    if source_override:
        cursor2 = await db.execute("SELECT * FROM med_overrides WHERE med_key = ?", (target,))
        target_override = await cursor2.fetchone()
        if not target_override:
            await db.execute(
                "UPDATE med_overrides SET med_key = ? WHERE med_key = ?",
                (target, source),
            )
    await db.commit()
    print(f"[meds] Merged '{source}' → '{target}'", flush=True)
    return {"ok": True, "source_key": source, "target_key": target}


@router.delete("/merge")
async def unmerge_medications(request: Request):
    """Remove a merge alias, restoring the medication as its own card.

    Expects JSON: { "alias_key": "doxycycline monohydrate" }
    """
    body = await request.json()
    alias_key = body.get("alias_key", "").strip()
    if not alias_key:
        return {"error": "alias_key is required"}

    db = await get_db()
    await db.execute("DELETE FROM med_aliases WHERE alias_key = ?", (alias_key,))
    await db.commit()
    return {"ok": True}


@router.post("/add")
async def add_medication(request: Request):
    """Add a patient-reported medication.

    Expects JSON:
      {
        "name": "Vitamin D 5000 IU",
        "rxcui": "optional-rxnorm-code",
        "dosage": "5000 IU",
        "frequency": "once daily",
        "notes": "optional notes",
        "status": "taking|as_needed"
      }
    """
    body = await request.json()
    name = body.get("name", "").strip()
    rxcui = body.get("rxcui", "").strip()
    dosage = body.get("dosage", "").strip()
    frequency = body.get("frequency", "").strip()
    notes = body.get("notes", "").strip()
    status = body.get("status", "taking").strip()

    if not name:
        return {"error": "name is required"}

    now = datetime.now(timezone.utc).isoformat()

    # Build FHIR MedicationRequest
    med_request = {
        "resourceType": "MedicationRequest",
        "status": "active",
        "intent": "plan",
        "medicationCodeableConcept": {
            "text": name,
        },
        "authoredOn": now,
        "meta": {
            "tag": [
                {
                    "system": "http://phv.local/tags",
                    "code": "patient-reported",
                    "display": "Patient-reported medication",
                }
            ],
        },
        "note": [],
    }

    # Add RxNorm coding if provided
    if rxcui:
        med_request["medicationCodeableConcept"]["coding"] = [{
            "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
            "code": rxcui,
            "display": name,
        }]

    # Add dosage instruction
    if dosage or frequency:
        dosage_text = dosage
        if frequency:
            dosage_text = f"{dosage}, {frequency}" if dosage else frequency
        med_request["dosageInstruction"] = [{
            "text": dosage_text,
        }]

    # Add notes
    if notes:
        med_request["note"].append({"text": notes})

    # Look up patient reference from an existing MedicationRequest so the new
    # record is consistent with the rest of the FHIR data
    try:
        fhir = await get_fhir_client()
        sample = await fhir.get("/MedicationRequest", params={"_count": "1"})
        if sample.status_code == 200:
            entries = sample.json().get("entry", [])
            if entries:
                subject_ref = entries[0].get("resource", {}).get("subject")
                if subject_ref:
                    med_request["subject"] = subject_ref
    except Exception:
        pass  # non-critical — store without subject

    # Store in FHIR
    try:
        r = await fhir.post("/MedicationRequest", json=med_request)
        r.raise_for_status()
        stored = r.json()
        stored_id = stored.get("id", "")
        print(f"[meds] Created MedicationRequest/{stored_id} for '{name}'", flush=True)

        # Verify the resource is readable
        verify = await fhir.get(f"/MedicationRequest/{stored_id}")
        if verify.status_code != 200:
            print(f"[meds] WARNING: Created resource {stored_id} but verify read failed: {verify.status_code}", flush=True)
        else:
            print(f"[meds] Verified MedicationRequest/{stored_id} is readable", flush=True)

        # Also set user override to "taking" or "as_needed"
        norm = normalize_med_name(name)
        db = await get_db()
        await db.execute(
            """INSERT INTO med_overrides (med_key, display_name, status, notes, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(med_key) DO UPDATE SET
                 display_name = excluded.display_name,
                 status = excluded.status,
                 notes = excluded.notes,
                 updated_at = excluded.updated_at""",
            (norm, name, status, notes, now),
        )
        await db.commit()

        return {
            "ok": True,
            "id": stored_id,
            "normalized_key": norm,
            "name": name,
            "status": status,
        }
    except Exception as e:
        import traceback
        print(f"[meds] Failed to add medication: {traceback.format_exc()}", flush=True)
        return {"error": str(e)}
