# Scraper Audit & Mobile-App Sequencing Plan

**Date:** 2026-05-29
**Scope:** Inventory of existing MyChart scrapers + HAPI FHIR baseline + prioritized gap list + recommended sequence for the in-app WebView scraping work.
**Outcome we're optimizing for:** know exactly what extra processing each new scraper will need before we wrap it in a mobile WebView, so iteration is grounded rather than speculative.

> **Status: point-in-time snapshot, partially superseded.**
> This audit's "UCSF: 0 messages" framing was wrong — UCSF messages are present in HAPI, surfaced via MSKCC's linked-accounts ("Happy Together") view and tagged with the source-of-scrape portal rather than the institution of origin. See `CONCLUSIONS_LOG.md` entries **C-001** (Happy Together aggregation), **C-002** (organizationId not preserved), **C-003/C-004** (MSKCC strong aggregator, Stanford weak), and hypothesis **H-001** (WP-24 token portability) for the corrected picture. The recommended sequence in §5 has been revised: see CONCLUSIONS_LOG and subsequent thread for the current Phase A' (attribution + dedup) and Phase A'' (empirical aggregator-overlap test).

---

## TL;DR

- The console-paste scrapers under `ingest/scrapers/` are sophisticated and three-tiered (FHIR probe → Epic internal API → DOM fallback). They are not the limiting factor.
- Structured-data coverage (labs, vitals, meds, conditions, encounters) is already strong — 36,436 Observations, 2,103 DiagnosticReports, 692 Communications.
- The depth-data target list still has clear holes: **zero UCSF messages**, zero `Appointment`, zero `ServiceRequest`, zero `CareTeam`, zero `Goal`, near-zero `MedicationStatement` and `AllergyIntolerance`.
- The most expensive cleanup is NOT scraping — it's downstream: **7 duplicate Patient resources for one human**, weak code mapping (most resources use only `text`, not coded values), and missing `Encounter` ↔ `DocumentReference` linkage. Mobile-app scraping should be designed to fix these at source, not to inherit them.

---

## 1. Existing scraper inventory

| File | Tier | Target | Mechanism |
|---|---|---|---|
| `mychart_fhir_probe.js` | 1 (FHIR) | Any Epic | Probes 6 known FHIR R4 paths; if found, pulls 15 resource types with pagination |
| `mychart_internal_api.js` | 2 (Internal API) | Any Epic | Probes ~24 internal AJAX paths (`/api/Visits`, `/api/Messages`, etc.) with anti-forgery token |
| `mychart_dom_scraper.js` | 3 (DOM) | Any Epic | Loads known section URLs (`/Visits`, `/Messaging`, `/TestResults`, …), parses tables/headers |
| `scrape_current_page.js` | Manual | Any MyChart | Per-page scraper for ad-hoc capture |
| `scrape_messages.js` | 2 | Any Epic | Calls `/MyChart/api/conversations/GetConversationDetails` with token + PageNonce + org-id (cross-institution!) |
| `scrape_visits.js` | 2/3 | Any Epic | Fetches each visit detail HTML page, parses sections+tables; filters same-origin to avoid Care Everywhere redirects |
| `network_interceptor.js` | Helper | Any | fetch() + XHR hook; silently records every JSON response as user navigates — best discovery tool |
| `scrape_ucsf_visits.js` | 2 | UCSF only | Reads `Epic.PatientAccess.Components.__Instances[7].RenderedData` to get real CSNs, calls `/UCSFMyChart/api/visits/past-details/GetVisitDetailsPast`, filters `IsLocal === true` |
| `scrape_ucsf_notes.js` | 2 | UCSF only | Depends on `__ucsfVisits`; calls `/UCSFMyChart/api/report-content/LoadReportContent` per shareable note |
| `ucsf_diag.js` | Helper | UCSF only | Diagnostic: find CSN format and visit detail URL patterns |
| `convert_and_load.py` | Loader | All | Auto-detects scraper output format, maps to FHIR R4, POSTs to HAPI |
| `process_messages.py` | Loader | All | Builds FHIR `Communication` bundle with institution classification |

**Things worth noting about the existing implementation:**

- The "generic Epic" scripts were last tuned at MSKCC, but the auth pattern (anti-forgery token + PageNonce + session cookie) is genuinely Epic-wide. Path prefixes (`/MyChart/` vs `/UCSFMyChart/` vs `/MyHealth/`) differ per customer instance.
- Stanford has no JS scraper checked in — but `converters/stanford_visits.py` and `stanford_results.py` expect JSON output in the exact Epic LoadPast shape, so the user has been running something Stanford-specific that produced `stanford_visits_raw.json` and `stanford_test_results_raw.json` and then converting offline. That gap should be closed before mobile.
- Cross-institution awareness is real and well-handled in the messages scraper (org-id captured per conversation) and in the visit scrapers (Stanford skips non-local, UCSF filters `IsLocal`). This logic must be preserved in the mobile rewrite.

---

## 2. Data classes captured (per scraper)

| Scraper | Visits | Notes | Messages | Labs | Meds | Conditions | Imaging | Appts | AVS | Care team |
|---|---|---|---|---|---|---|---|---|---|---|
| `mychart_fhir_probe.js` | ✅ Encounter | ✅ DocRef | ❌ | ✅ Obs | ✅ MedReq | ✅ Condition | ✅ DiagRep | ❌ | ❌ | ⚠️ CareTeam if exposed |
| `mychart_internal_api.js` | ✅ rawText | ⚠️ partial via visit detail | ✅ subj/from/date | ✅ rawText | ✅ rawText | ✅ list | ❌ | ❌ | ❌ | ✅ name only |
| `mychart_dom_scraper.js` | ✅ rawText | ⚠️ section text | ✅ rawText | ✅ rawText | ✅ rawText | ✅ list | ❌ | ❌ | ❌ | ✅ name only |
| `scrape_messages.js` | — | — | ✅ full thread, HTML body, participants | — | — | — | — | — | — | — |
| `scrape_visits.js` | ✅ link + section HTML | ✅ full HTML text | — | ⚠️ inline | ⚠️ inline | ⚠️ inline | ⚠️ inline | ❌ | ❌ | — |
| `scrape_ucsf_visits.js` | ✅ structured (CSN, type, provider, dept) | metadata only | — | — | — | — | — | ❌ | ❌ | ⚠️ provider name |
| `scrape_ucsf_notes.js` | — | ✅ full HTML note body via LoadReportContent | — | — | — | — | — | — | — | — |

**Categorically uncaptured by every scraper above:** future Appointments, AVS (After-Visit Summary) as a discrete document, ServiceRequest (referrals/pending orders), CarePlan, Goal, Questionnaire/QuestionnaireResponse, Device, RelatedPerson.

---

## 3. HAPI FHIR baseline (as of audit)

Live counts from `http://localhost:8080/fhir`:

| Resource | Count | Source breakdown |
|---|---|---|
| Patient | **7** | duplicates (see §5) |
| Encounter | 396 | UCSF 241, Stanford 139, other untagged |
| Condition | 65 | |
| MedicationRequest | 739 | |
| MedicationStatement | 1 | effectively empty |
| AllergyIntolerance | 3 | effectively empty |
| Observation | **36,436** | Stanford 3,489 lab; UCSF 1,356 lab; rest from MSKCC + Apple Health + vitals |
| Immunization | 13 | sparse |
| Procedure | 59 | |
| DiagnosticReport | 2,103 | |
| DocumentReference | 672 | UCSF 108 (clinical notes); Stanford & MSKCC produce DocRefs too but under different tag systems |
| CarePlan | 1 | empty |
| CareTeam | **0** | empty |
| Goal | **0** | empty |
| Communication | 692 | MSKCC 376, Stanford 316, **UCSF 0** |
| ServiceRequest | **0** | empty |
| Appointment | **0** | empty |
| Device | 1 | |
| DeviceUseStatement | 0 | empty |
| Questionnaire / QuestionnaireResponse | 0 / 0 | empty |
| Practitioner | 15 | |
| Organization | 14 | |
| RelatedPerson | 0 | |
| Location | 10 | |
| Coverage | 0 | |

**DocumentReference type distribution (top 10):** Progress Notes 207, Clinical Note 108, Telephone Encounter 66, Patient Instructions 66, Diagnostic imaging study 45, Care Plan Note 16, Discharge Instructions 15, Consults 14, H&P 10, untyped 18.

That distribution is encouraging — Progress Notes dominating means free-text clinical narrative *is* landing, just not always with clean source attribution.

---

## 4. Gap analysis vs the depth-data target list

### Stanford

| Target class | Today | Gap | Effort estimate |
|---|---|---|---|
| Labs (Obs + DR) | 3,489 obs / portion of 2,103 DR | ✅ covered | — |
| Encounters | 139 | ✅ covered | — |
| Clinical notes (DocRef bodies) | Partial — no Stanford tag distinguishes them | ⚠️ need parity with UCSF LoadReportContent equivalent | M — replicate UCSF pattern against Stanford's API |
| Messages (Communication) | 316 | ✅ covered | — |
| AVS as discrete artifact | None | ❌ missing | S — likely a separate endpoint near visit details |
| Appointments (future) | 0 | ❌ missing | S — Epic exposes `/api/Visits/Upcoming` or similar |
| ServiceRequest (referrals, pending orders) | 0 | ❌ missing | M — new scraper |
| Imaging reports | folded into 2,103 DR | ✅ likely covered, verify | — |
| CareTeam | 0 | ❌ missing | S — Epic `/api/CareTeam` exists |
| Allergies | 3 total across all sources | ❌ under-captured | S |
| Immunizations | 13 total | ❌ under-captured | S |

### UCSF

| Target class | Today | Gap | Effort estimate |
|---|---|---|---|
| Labs (Obs + DR) | 1,356 obs / portion of 2,103 DR | ✅ covered | — |
| Encounters | 241 | ✅ covered | — |
| Clinical notes (DocRef bodies) | 108 with full body | ✅ best-in-class via LoadReportContent | — |
| **Messages (Communication)** | **0** | ❌ **biggest single UCSF gap** | M — apply existing `scrape_messages.js` pattern against `/UCSFMyChart/api/conversations/GetConversationDetails` |
| AVS as discrete artifact | None | ❌ missing | S |
| Appointments (future) | 0 | ❌ missing | S |
| ServiceRequest | 0 | ❌ missing | M |
| Imaging reports | partial | ⚠️ verify Binary content resolves | S |
| CareTeam | 0 | ❌ missing | S |
| Allergies / Immunizations | conflated with Stanford counts | ❌ under-captured | S |

### Cross-cutting (affects both)

1. **Patient deduplication.** Seven `Patient` resources for one human — Epic FHIR ID (Stanford+UCSF) + MSKCC MRN + several UUIDs from separate scrape runs. None tagged. Mobile-app scraping should write to a canonical local patient ID and capture institutional identifiers as `Patient.identifier` entries, not separate resources.
2. **DocumentReference ↔ Encounter linkage.** UCSF notes use CSN-keyed `context.encounter` references; that pattern works. Stanford visit details land in DocRefs without consistent encounter linkage. Mobile app should make this required, not best-effort.
3. **Code mapping.** Most non-lab resources use only `.text`. The downstream AI assistant works around this via `loinc_synonyms.py` and `loinc_mapper.py`, but the cost of unmapped data compounds. RxNorm for meds and ICD-10/SNOMED for conditions should be normalized at ingest time, not at query time.
4. **MedicationStatement vs MedicationRequest.** Currently 739 vs 1. The reconciliation model in `meds.py` would benefit from MedicationStatement being populated, since "patient is taking" is different from "provider ordered."
5. **No `Appointment`, `ServiceRequest`, `CareTeam`.** These three together are the highest-value structured gap — they support future-facing features (reminders, care coordination) that the existing reminder system already needs.

---

## 5. Recommended sequence for the mobile-app project

Given the user picked: **cross-platform mobile**, **separate repo**, **audit-first**.

### Phase A — close the UCSF messages gap inside the existing console-paste workflow (1–2 days)
Cheapest, highest-impact extension. Adapt `scrape_messages.js` for `/UCSFMyChart/api/conversations/GetConversationDetails`. Stand up `process_messages.py` to handle the UCSF source tag. **Acceptance:** Communication count for UCSF goes from 0 to >0; round-trip into HAPI.

### Phase B — Stanford-specific scrapers committed to the repo (2–3 days)
Stand up `scrape_stanford_visits.js`, `scrape_stanford_notes.js`, `scrape_stanford_results.js` mirroring the UCSF pattern, against Stanford's path prefix (`/MyHealth/...`). Cleanly check in the JSON shapes the converters already expect. **Acceptance:** running these three scripts produces the same JSON files (`stanford_*_raw.json`) the converters consume today, without manual editing.

### Phase C — extend per-institution scrapers to the structured gaps (3–5 days)
Add scraping for: `Appointment` (future visits), `ServiceRequest` (referrals + pending orders), `CareTeam`, AVS-as-DocumentReference. Do Stanford and UCSF in parallel — same fields, different paths. **Acceptance:** at least one resource of each new type lands in HAPI per institution.

### Phase D — comparison pipeline (2 days, can run in parallel with C)
Build the diff tool that compares scraper JSON output against existing HAPI contents. Reports: new resources, changed resources, dropped resources, and unmapped fields. Becomes the regression harness for every subsequent scraper change. **Acceptance:** running it after a re-scrape produces a meaningful change report.

### Phase E — patient + code normalization pass (3–4 days)
Before any of this lands in a mobile app, fix the patient duplication and add minimum-viable code mapping (RxNorm for meds via existing `loinc_synonyms`-style tables; ICD-10 stays text for now). **Acceptance:** one canonical Patient resource per human; >80% of MedicationRequest entries have an RxNorm code.

### Phase F — mobile app scaffold (1 week)
**New repo.** Cross-platform — React Native or Capacitor. Embeds WKWebView/Android WebView. Implements the auth+scrape flow: user logs into MyChart inside WebView, app injects scraper JS from a remote config endpoint hosted by BinaHealth API, ships parsed JSON back. Reuses Phase A–C scrapers as the remotely-loaded config payload. **Acceptance:** can connect Stanford and UCSF accounts on iOS and Android, and the same data shows up in HAPI as the console-paste workflow produces today.

### Phase G — only after F is working
Add the depth-data extensions (per-message scraping efficiency, AVS export, incremental sync since last successful run). Resist scope creep until the F loop is closed end-to-end.

---

## 6. Open questions worth answering before Phase F

1. **Mobile stack: React Native vs Capacitor vs Flutter.** RN has the deepest WebView ergonomics on both platforms; Capacitor is simpler if you don't need a native UI shell; Flutter's WebView story is the weakest. No urgency now — Phase A–E happen in this repo.
2. **Remote scrape-config protocol.** JSON+injectable JS strings is the simplest; a typed DSL is more durable but is meaningfully more work. Punt until Phase F.
3. **Token / session handling.** The mobile WebView keeps the session in the user's cookie jar on their device — clean for IP-mismatch reasons (see prior discussion). No backend token vault required for v1.
4. **Where to put the comparison pipeline output.** Probably a `tools/scrape-diff/` directory in this repo, with reports emitted to `/tmp/scrape-diffs/` or similar. Not a product feature; an engineering tool.

---

## Status

This audit completes the planning phase. Next concrete action depends on you:

- **Default suggestion:** start Phase A (UCSF messages) — smallest scope, biggest user-visible win, exercises the existing pipeline end-to-end.
- **Alternative:** start Phase D (comparison pipeline) — builds the regression harness first, so every subsequent scraper change is measurable.
