# Coverage Matrix — what data we capture, from where, to what depth

**Purpose:** Single source of truth for "do we have it yet?" across every FHIR resource type × source portal × capture layer. Lets us answer "are we forgetting anything?" without re-deriving it from scrapers + HAPI counts + ingest scripts each time.

**Maintenance discipline:**
- Whenever a scrape job ships, a new portal is added, or a resource type changes status: update this file in the same commit. The matrix is the deliverable, not a side note.
- Counts are point-in-time. Re-run the counts block (see Appendix) and bump "Last counts taken" whenever you update the matrix. Don't chase small drift.
- Status changes get a one-line entry in the changelog at the bottom — keeps the history without bloating the matrix.

**Last counts taken:** 2026-08-10 post-orion-triad ingest (HAPI on localhost:8090).
**Coverage matrix version:** 5.

---

## 1. Sources / portals

| Source | Status | How we authenticate | Notes |
|---|---|---|---|
| **Stanford MyHealth** | Active | iOS Keychain creds → WebView session (mobile app) | Primary native scrape. Wraps Epic MyChart at `mychart.stanfordhealthcare.org`. Strong on visits + messages; deep cross-institution (MSKCC, UCSF, Mayo) via Happy Together. See `CONCLUSIONS_LOG` C-001, C-018. |
| **MSKCC MyChart** | Active (via Stanford Happy Together) | Inherited from Stanford session | Strong aggregator — ~57% cross-institution content surfaces here. See C-003. No direct mobile scrape yet; messages came from prior console-paste runs. |
| **UCSF MyChart** | Partial | Console-paste scrape (legacy); no mobile native yet | Some visits + notes ingested via `scrape_ucsf_visits.js` + `scrape_ucsf_notes.js`. Cross-institution Stanford visibility is weak — only ~9%. See C-004. |
| **Mayo Clinic** | Lapsed | Was linked; connection silently dropped | Historical data preserved. Re-linking is a P-001 onboarding moment. See C-010. |
| **Hoag** | Never connected | — | Referenced in records, no portal connection. P-001 "states a patient may have forgotten" candidate. |
| **Apple Health Records** | Active | Passive via Apple device → HAPI | Coverage manifest (C-018): tells us *what exists* across institutions; carries metadata not bodies. Most useful for spotting missing institutions, less useful for content. |
| **Epic on FHIR (OAuth)** | Configured, not actively pulling | OAuth env vars set, no scheduled sync | `EPIC_CLIENT_ID` / `EPIC_CLIENT_SECRET` available. Not running. Future: scheduled refresh if it adds anything the mobile scrape misses. |

---

## 2. Coverage matrix

Columns:
- **List**: do we know the full set of items exist? (= we can enumerate them)
- **Body**: do we have the actual content / value for each item?
- **HAPI**: count of resources currently in v2 HAPI
- **UI**: surfaced to the user in the web app?
- **AI**: accessible to the chat assistant (i.e., wired into its retrieval / shown in context)?

Status emoji: ✅ done · 🟡 partial · ⚠️ in flight · ❌ missing · — not applicable

| Resource type | Source | List | Body | HAPI | UI | AI | Notes / next |
|---|---|---|---|---|---|---|---|
| Patient | mixed | ✅ | n/a | 16 ✅ | ✅ Profile | ✅ | **Reorganized 2026-06-23**: `Patient/bina-user-urisarid` is the canonical Bina anchor with `link[seealso]` to 15 sub-identity Patients (one per data source). Sub-identities: 4 institutional (stanford/ucsf/mayo/mskcc), 1 affiliate (sutter), 4 vendors (labcorp/doctorsdata/sibocenter/genova), 1 EHR-unknown (cerner), 3 Apple (wearable/health-records), 2 misc (historical-import/unknown-oid/untagged). |
| Practitioner | — | ❌ | ❌ | 0 | ❌ | ❌ | Senders/recipients carried as `.display` only; no first-class Practitioner records. Low priority. |
| Encounter (past visits) | Stanford, MSKCC, UCSF | ✅ | ✅ via mobile per-visit notes | 543 | ✅ Home / appointment details | ✅ | Stable. |
| Appointment (future visits) | Stanford native | ✅ | ✅ | **4** | ✅ Home tab + FHIR | ✅ | **Phase 6a Stanford appointment scrape shipped (2026-08-10)** via `GET /orion/public/ajax/v1/appointments/futureappointments`. 4 upcoming appointments with provider (NPI), department (address), patientInstructions. First Appointment resources in HAPI. |
| Condition (problem list) | mixed + Stanford native | ✅ | ✅ | 79 | ✅ Profile | ✅ | **Phase 5 Stanford native scrape shipped (2026-06-25)** via `POST /myhealth_sso/Clinical/HealthIssues/LoadListData`. +14 from Stanford; pre-existing 65 across other sources. |
| Procedure | mixed + Stanford native | ✅ | ✅ Stanford (surgery cases) | **62** | ✅ list + Stanford surgery detail | ✅ | **Phase 6a Stanford surgery scrape shipped (2026-08-10)** via `POST /orion/public/ajax/v1/surgery/allSurgeries` (body `{"numOfDays":730}`). 1 surgery case → 3 Procedure resources (each billable code becomes its own Procedure with providerNPI, location, performedDateTime). Includes the Jan 2026 colonoscopy that motivated Phase 4. |
| Observation | mixed | ✅ | ✅ | ~40,000 | ✅ Search, Profile vitals | ✅ | Strong. Quality patches in `DATA_QUALITY_CHECKLIST.md`. **Phase 4-3 added 3,555 structured component-result Observations from 479 Stanford labs** (`urn:stanford:myhealth:component-result`). |
| DiagnosticReport (lab/imaging/path) | Stanford, MSKCC, LabCorp | ✅ 489 from Stanford as `urn:stanford:myhealth:order` | ✅ Stanford (479/489 = 98%) | 2,103 | ✅ Search | ✅ for the 479 Stanford labs (component-level Observations) | **Phase 4 complete for Stanford labs.** Two-step API (GetDetails + LoadReportContent) reverse-engineered via portal-scout; ~70s for the full batch. 10 outliers (imaging-only/comments-only/metadata-only) skipped. |
| DocumentReference (clinical notes) | Stanford (via mobile), MSKCC, UCSF | ✅ | ✅ | 821 | ✅ inline on encounter | ✅ via `loadBinaryNoteInto` | Strong since Phase 2 (per-visit-note scrape). |
| Communication (messages) | Stanford (mobile, 752), MSKCC (legacy, 376) | ✅ | ✅ | 1,128 | ✅ Messages tab | ⚠️ no direct assistant wiring yet | Phase 3 just shipped. Thread-key heuristic improvement queued (`task_e734b339`). |
| MedicationRequest | mixed | ✅ | ✅ | 739 | ✅ Meds tab | ✅ | Strong. |
| MedicationStatement | — | ❌ | ❌ | **1** ❌ | ❌ | ❌ | Effectively empty. Patient-reported meds + adherence data not captured. |
| Medication | — | ❌ | ❌ | 0 | ❌ | ❌ | Inline-referenced via MedicationRequest only. |
| AllergyIntolerance | Stanford native + historical-import + apple-health-records | ✅ Stanford | ✅ Stanford | **4** | ✅ Profile | ✅ | **Phase 5 Stanford native scrape shipped (2026-06-25)** via `POST /myhealth_sso/Clinical/Allergies/LoadListData`. +1 from Stanford; combined with prior historical + apple-health-records sources = 4 total. |
| Immunization | Stanford native + historical-import | ✅ Stanford | ✅ Stanford | **33** | ✅ Profile | ✅ | **Phase 5 Stanford native scrape shipped (2026-06-25)** via `POST /myhealth_sso/Clinical/Immunizations/LoadImmunizationsList`. 13 Stanford rows fanned out to 20 dose events (boosters expand 1:N); combined with 13 historical = 33 total. |
| CarePlan | — | ❌ | ❌ | **1** ❌ | ❌ | ❌ | Empty. |
| CareTeam | — | ❌ | ❌ | **0** ❌ | ❌ | ❌ | Empty. Stanford UI shows care team prominently — scraper gap. Useful for "who do I message about X." |
| Goal | — | ❌ | ❌ | 0 | ❌ | ❌ | Empty. |
| ServiceRequest (orders/referrals) | — | ❌ | ❌ | **0** ❌ | ❌ | ❌ | Empty. Pending/historical orders + referrals are clinically important; not captured. |
| Coverage (insurance) | — | ❌ | ❌ | 0 | ❌ | ❌ | Empty. |
| RelatedPerson (family) | — | ❌ | ❌ | 0 | ❌ | ❌ | Empty. |
| Device | — | ❌ | ❌ | 0 | ❌ | ❌ | Empty. Implants, pumps, hearing aids, etc. — would show in EHRs. |
| Questionnaire / QuestionnaireResponse | — | ❌ | ❌ | 0 | ❌ | ❌ | Empty. Pre-appointment intake forms not captured. |
| Organization, Location, Practitioner | — | ❌ | ❌ | 0 | ❌ | ❌ | All zero — we reference these via `.display` strings throughout, never as first-class resources. |
| **Stanford "Letters" folder** | Stanford | ❌ | ❌ | n/a | ❌ | ❌ | Visible in the messages sidebar (Inbox / Sent / Letters); we don't scrape it. Formal letters (medical clearances, work notes, referral letters) could have real value. |
| **Imaging studies (DICOM/PACS)** | — | ❌ | ❌ | n/a | ❌ | ❌ | Image studies behind MyChart's Imaging viewer — no FHIR `ImagingStudy` resources, no PACS access. Reports captured via DR (with body fetch underway); pixel data unreachable through patient portals. |
| **Billing / financial records** | — | ❌ | ❌ | n/a | ❌ | ❌ | Not in scope today. Bills, EOBs, payment history. |

---

## 3. Capture mechanisms

| Mechanism | Where | Status | What's captured |
|---|---|---|---|
| **Mobile-app native** (Flutter + WebView, this repo's `mobile/`) | Stanford only | Active — primary | Per-visit notes (97 + multi-note hospital stays), messages (752), labs in progress (489) |
| **Console-paste scrapers** (`ingest/scrapers/*.js`) | Any Epic | Legacy, no longer running | Original baseline. Replaced by mobile for Stanford; UCSF still depends on these. |
| **Stanford v3 ingest** (`tools/v3/`) | Stanford | Historical | Pre-mobile baseline. Produced the eorderids in HAPI we're now backfilling bodies for. |
| **Apple Health Records sync** | All sources | Passive, ongoing | DocRef + Observation metadata; mostly coverage signal not content |
| **Epic on FHIR (OAuth)** | Future | Not active | OAuth flow configured (`EPIC_REDIRECT_URI`), no scheduled sync |

---

## 4. Cross-institution attribution

A subtlety that affects everything: when Stanford's "Happy Together" surfaces data from MSKCC/UCSF/etc., **the source portal we tag with isn't the institution of origin.** A UCSF lab can land in HAPI tagged `stanford.mychart` because we scraped it via Stanford. See `CONCLUSIONS_LOG`:

- **C-001** Happy Together aggregation exists and varies by portal
- **C-002** `organizationId` not preserved through our ingestion (data quality issue)
- **C-003** MSKCC ≈ 57% cross-institution content (strong aggregator)
- **C-004** Stanford ≈ 9% cross-institution content (weak aggregator)
- **C-018** Apple Health Records as a coverage manifest

Implication: row counts above are "what we have in HAPI under that resource type" — *not* "what institution X ever provided." Don't compute coverage as a count delta without thinking about who-scraped-whom.

---

## 5. In-flight + next-up

| Status | Item | Branch / commit | Tracking |
|---|---|---|---|
| ⚠️ in flight | Phase 4-3: HTML→FHIR converter for lab DR bodies | uncommitted | TaskList #7 |
| 🟡 queued | Stanford message thread-key heuristic improvement | — | `task_e734b339` (spawn) |
| 🟡 queued | Search result expansion UI overflow | — | `task_250e0486` (spawn) |
| 🟡 queued | Procedure body scrape (mirrors Phase 4 — colonoscopy/biopsy reports) | — | not yet ticketed |

---

## 6. Known gaps — prioritized

Ordered by clinical value × scraping feasibility.

**Tier A — high value, scraping path likely similar to what we've built:**
1. **DiagnosticReport bodies** — in flight (Phase 4)
2. **Procedure bodies** — same shape as labs; colonoscopy/pathology reports
3. **Future Appointments → FHIR Appointment** — already shown in UI from `/api/profile`; just need to persist
4. **AllergyIntolerance** — every portal lists prominently; cheap to add

**Tier B — high value, harder scraping:**
5. **CareTeam** — Stanford UI shows it; would require a sidebar scrape (not visible in main URL flow)
6. **Letters folder** — Stanford messaging sidebar; structurally similar to messages
7. **Stanford Procedure list bodies** — same as Tier A item 2 above but specifically the procedures-and-surgeries history

**Tier C — clinical-but-niche, or out of patient-portal scope:**
8. **MedicationStatement** (adherence) — patient-reported, not always in portals
9. **Immunization** — all 13 currently in HAPI come from `bina-historical-import` (a one-time consolidated dump). Per-portal scrapers don't surface immunizations at all yet. Mayo native scrape would re-cover most; UCSF/MSKCC/Stanford each carry their own slice.
10. **Mayo native scrape** — the `bina-historical-import` bucket is the *only* path through which Mayo MM-monitoring labs (Free Kappa/Lambda Light Chain, Kappa/Lambda Ratio — 68 of the 70 historical Observations) reach HAPI. No Mayo mobile scrape exists; Apple Health Records only carries a small slice. Single point of failure for the user's primary disease-monitoring data.
11. **Questionnaire/QuestionnaireResponse** — pre-appointment intake forms
12. **ServiceRequest** — pending orders / referrals
13. **CarePlan / Goal** — chronic-disease management; rare in our portals

**Tier D — explicitly out of scope today:**
- Imaging pixel data (PACS access not available via patient portals)
- Billing / EOBs

---

## 7. Per-portal status snapshot

| Portal | Visits | Messages | Labs | Conditions | Meds | Allergies | Care team |
|---|---|---|---|---|---|---|---|
| Stanford | ✅ via mobile | ✅ Phase 3 | ⚠️ Phase 4 | ✅ | ✅ | ❌ | ❌ |
| MSKCC | ✅ legacy | ✅ legacy (376) | 🟡 some in HAPI | 🟡 | 🟡 | ❌ | ❌ |
| UCSF | 🟡 legacy | ⚠️ aggregated via Stanford | 🟡 some in HAPI | 🟡 | 🟡 | ❌ | ❌ |
| Apple Health Records | metadata only | n/a | metadata only | n/a | n/a | n/a | n/a |

---

## Appendix: how to refresh the counts

```bash
for rt in Patient Practitioner Encounter Appointment Condition Procedure \
         Observation DiagnosticReport DocumentReference Communication \
         MedicationRequest MedicationStatement Medication AllergyIntolerance \
         Immunization CarePlan CareTeam Goal ServiceRequest Coverage \
         RelatedPerson Device Questionnaire QuestionnaireResponse \
         Organization Location; do
  total=$(curl -s "http://localhost:8090/fhir/${rt}?_count=0&_total=accurate" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('total','?'))")
  printf "  %-25s %s\n" "$rt" "$total"
done
```

Bump the "Last counts taken" date when you update. If a count's status emoji changes, append a line to the changelog.

---

## Changelog

- **2026-08-10** — v5. Phase 6 (Stanford orion endpoints via after-auth orchestrator): Procedures 59→62 (Jan 2026 colonoscopy body finally captured — the case that motivated Phase 4), Appointments 0→4 (first Appointment resources in HAPI). Auto-scrape orchestrator now runs 4 tasks per sign-in (labs, messages, clinical triad, orion) with zero taps beyond MFA — see `feedback-auto-run-after-auth` memory. Next gap: Medicines (only ~52 of 739 MedRequests are Stanford-native today; endpoint returns HTML — hidden-JSON discovery is next scout target).
- **2026-06-25** — v4. Phase 5 Stanford clinical-triad ingest: Allergies (3→4), Immunizations (13→33; 13 native rows fan out to 20 dose-events), Conditions (65→79). Endpoint contracts mined from the v1.12 portal-scout's full-surface capture and templated through `ScrapeJobs.stanfordClinicalLoadList`. Same pattern unblocks the remaining 12 sections mapped in `tools/portal-scout/specs/stanford-v1.json`.
- **2026-06-23 (later)** — v3. Patient consolidation: canonical `Patient/bina-user-urisarid` + 15 sub-identity Patients (one per data source); 43,879 resources reassigned to point at the matching sub-identity. Subject coverage now 100% across all major resource types (was 5%). Known limitation: HAPI's `Patient/$everything` doesn't follow `Patient.link` by default — the live app uses per-type `subject=Patient/X` searches and doesn't hit this. See `tools/patient_consolidation/consolidate_patients.py`.
- **2026-06-23** — v2. Phase 4 complete for Stanford labs: 3,555 structured component-result Observations added across 479 DRs. Discovered systemic pre-existing gap: 1,991/2,103 DRs lack `subject` reference (only 112 are patient-linked), and 7 Patient resources exist for one human — needs dedup + backfill.
- **2026-06-17** — v1 created. Reflects Phase 3 (messages) shipped, Phase 4 (lab bodies) in flight. Supersedes the per-resource sections of `SCRAPER_AUDIT_2026-05.md`.
