# Conclusions Log

**Purpose:** Living record of empirical conclusions about EHR data acquisition and processing. Each entry captures a claim, the evidence supporting it, the sample it was derived from, the conditions under which it might not generalize, and a trigger for re-evaluation. Append-only. When an entry is superseded, mark it `superseded` and link to the replacement — never silently overwrite.

**Maintenance discipline:**
- New patient enrolled → revisit every entry with `patients_in_sample = 1`. Update sample, confidence, or supersede.
- New EHR vendor encountered → revisit every entry tagged `epic-specific` to confirm scope.
- A converter change that affects field preservation → log a provenance audit entry below.
- Quarterly: scan for `re_evaluation_trigger` dates that have passed.

**Status values:** `confirmed` (multi-sample evidence) · `n1-observed` (one patient only) · `hypothesis` (testable claim, not yet tested) · `disproven` · `superseded`

---

## Principles

Foundational stances that govern all data work in BinaHealth. Listed here — not in `PRODUCT_INSIGHTS_LOG.md` — because they are claims about how we *treat data*, not about what we sell or how users behave. Every conclusion or hypothesis below should be readable against these principles; when a conclusion contradicts a principle, the principle needs revision, not the conclusion.

### P-DATA-IS-GOLD

Data is gold. Missing, truncated, stale, or improperly-scraped data is *really bad* — every downstream insight (the AI assistant's advice, the analyst's trend lines, the medication reconciliation, the user's own decisions) inherits that incompleteness without flagging it. Once data is lost in transit from source to vault, downstream reasoning silently degrades and is suspect by definition.

Operational implications:
- **Capture more, not less.** When uncertain whether a field is needed, preserve it via the provenance contract. Cheap to drop later; expensive to recover later.
- **Detect partial captures.** Every sync routine must compare actual scope against expected scope and flag deltas. "Got *some* data" is never acceptable as success.
- **Detect staleness.** Per C-010, MyChart linked-accounts lapse silently. Per-source freshness monitoring is mandatory.
- **Flag, don't proceed.** When the data layer is known-incomplete, the assistant and analyst should warn the user explicitly, not silently work with what they have.
- **Re-verifiability.** Every datum should retain enough provenance (source file, scraper version, organizationId, timestamp) to be re-fetched or re-derived from source if it's ever lost.

C-011 is a textbook violation of this principle: full data was captured in raw scrape but never made it to the queryable layer, and v1's assistant has been operating on a 20%-completeness slice of MSKCC messages without any signal that it was missing the rest.

### P-DEDUP-CARRY-PROVENANCE

When de-duplicating data, never erase — replace with references. Doctor's notes routinely include dumps of prior data (recent labs, problem list, medication list, quoted prior messages) that the system already has as structured resources. These dumps should be recognized as duplicates and replaced with typed references to the canonical source, so the doctor's decision context is preserved without re-storing the data.

Why this matters:
- **Storage and query quality.** Without dedup, the same Observation appears as discrete data, embedded in notes, and quoted in messages — distorting any frequency, recency, or trend analysis.
- **Clinical reasoning preservation.** The doctor's "Plan:" section is anchored to the context they cited above. Stripping the context loses the linkage between observations and decisions.
- **Cross-source canonicalization.** The same lab value scraped via MSKCC's linked-accounts view of UCSF *and* scraped via UCSF natively should resolve to one canonical Observation with multiple `provenance` entries, not duplicate.

Currently aspirational — the system doesn't do this yet. See H-005 for the testable claim and design questions.

### P-STRUCTURED-FIRST

Prefer structured extraction over text re-parsing. When source data has structure — HTML DOM, JSON fields, FHIR resources, XML — scrapers must preserve that structure into the raw export. Rendering structured content to a textContent blob and then re-parsing it downstream loses information at the extraction step and creates a fragile sub-system of pattern-matching that scales poorly across patients, portal versions, and resource types.

Operational implications:
- **Scrapers don't `textContent` containers.** Walk the DOM by semantic class/selector and emit one field per piece of meaning. Where the portal's class names vary across customer instances, the per-portal scrape-config (see "scraper architecture v3" task) declares the mapping; the scraper itself stays generic.
- **Converters trust structure.** A converter that finds itself parsing a free-text blob with regex is consuming the output of a scraper bug. Fix it upstream.
- **JSON APIs are first-class.** When a portal exposes both an HTML view and a JSON API for the same data (as Epic does for conversation details), prefer the API. The existing messages scraper already does this for the `messages[]` array; the inbox subject is the residual case that should also move to a JSON source if Epic exposes one, or to semantic DOM scraping otherwise.
- **Audit existing scrapers against this principle.** Every `textContent`-based extraction in current scrapers (visits, messages, notes) is a candidate for replacement with semantic extraction.

This principle was prompted by `tools/v2/convert_messages.py`'s regex iterations over the MSKCC `preview` field — an HTML inbox structure that was textContent'd into a single mangled string by `ingest/scrapers/scrape_messages.js` and then reverse-engineered downstream. The regex is acceptable as one-time tactical work for the v1↔v2 diff baseline (the C-011 finding it surfaced is robust to subject-extraction quality), but is explicitly deprecated for ongoing or productized use.

---

## Index

| ID | Status | Claim (short) | Last touched |
|---|---|---|---|
| C-001 | n1-observed | Epic MyChart implements cross-institution linked accounts ("Happy Together") with materially different aggregation strengths per portal | 2026-05-31 |
| C-002 | confirmed | Captured `organizationId` was not preserved from scraper output into FHIR resources — raw data retains it; retrofit possible without re-scraping | 2026-05-31 |
| C-003 | n1-observed | MSKCC MyChart is an extreme aggregator: ~99% of its scraped Communications are cross-institution (only 3 of 376 are MSKCC-native) | 2026-05-31 |
| C-004 | n1-observed | Stanford MyHealth is structurally outside the MSKCC/UCSF/Sutter/Mayo/JMH linked-account cluster in both directions | 2026-05-31 |
| C-005 | confirmed | Patient deduplication is currently absent — multiple `Patient` resources exist per human | 2026-05-29 |
| C-006 | confirmed | Stanford and MSKCC scrapes use structurally incompatible identifier extraction — but both retain recoverable WP-24 tokens; gap is converter-side, not API-side | 2026-05-31 |
| C-007 | confirmed | MSKCC scrape captures per-thread records with nested messages; Stanford scrape captures per-message records — different granularities for the same logical layer | 2026-05-31 |
| C-008 | n1-observed | Six distinct `organizationId` values identified in MSKCC scrape with confirmed institutional attribution (UCSF, Sutter, Mayo, John Muir Health, Altais, MSKCC-native) | 2026-05-31 |
| C-009 | confirmed | METHODOLOGY: clinician name is not a reliable proxy for Epic organization; platform sender labels and `organizationId` are the stronger signals | 2026-05-31 |
| C-010 | n1-observed | MyChart linked-accounts connections expire silently over time — historical data persists in receiving portal, new data stops flowing, reconnection is user-initiated and unprompted | 2026-05-31 |
| C-011 | confirmed | v1 production silently lost ~80% of MSKCC message content by loading from preview-only file instead of full-nested-message file | 2026-06-04 |
| C-012 | confirmed | v1 production discards ~17% of clinical note text by stripping HTML to plaintext before storage; v2 preserves HTML and recovers the structural content | 2026-06-04 |
| C-013 | confirmed | Stanford visits ingestion was complete in v1 — no data-loss bug analogous to C-011/C-012; the v1 converter captured CSN and core fields cleanly. v2 adds provenance tags and preserves the previously-dropped `_orgKey` (Stanford organisation token) and IsLocal flag | 2026-06-04 |
| C-014 | confirmed | v1 stores medication name in `medicationReference.display` (731 of 739 MRs), not the FHIR-canonical `medicationCodeableConcept.text` (8 MRs) — entity-linking code must check both | 2026-06-04 |
| C-015 | confirmed | Across 32 PMH-bearing UCSF notes, 9 unique diagnoses (incl. GERD 2002, Low back pain 2003, Bacterial overgrowth, Claudication, SVT) have no matching v1 Condition; after routing ANA-positive→Observation and Allergy→AllergyIntolerance, the true Condition gap is 7 | 2026-06-04 |
| C-016 | confirmed | Vitals in clinical notes are 100% redundant with same-day Observations — perfect dedup target | 2026-06-04 |
| C-017 | confirmed | AHR-ingested DocumentReferences across ALL institutions are metadata stubs — Binary URLs are populated but Binary resources are not in HAPI. **AHR intentionally exports references not content; the Binary tokens are Epic-format and the source EHR (identified by AHR's `custodian` field) can resolve them via Epic on FHIR.** 461 of 463 DocRefs are theoretically recoverable | 2026-06-04 |
| H-001 | hypothesis | The Epic `WP-24…` thread token is portable: identical across portals for the same underlying conversation | 2026-05-31 |
| H-002 | hypothesis | Native portal scrape yields strictly more information per conversation than the same conversation viewed via a linked-accounts aggregator | 2026-05-29 |
| H-003 | hypothesis | "Strong aggregator" status is an Epic customer configuration property, not patient-specific; the same portal aggregates the same way for any patient | 2026-05-29 |
| H-004 | hypothesis | Apple Health Records and MyChart scraping return overlapping but not subset/superset clinical-note coverage for the same institution | 2026-05-29 |
| H-005 | partial-confirmation | Doctor's notes can be de-duplicated against structured data via entity linking + temporal matching, with quoted dumps replaced by typed references to canonical resources (carries P-DEDUP-CARRY-PROVENANCE) — empirical prototype 2026-06-04 confirmed mechanism for medications | 2026-06-04 |

---

## Entries

### C-001 — Epic MyChart linked accounts ("Happy Together") aggregation varies by portal

- **Status:** `n1-observed` (strengthened 2026-05-31)
- **Claim:** Epic MyChart customer instances expose data from other Epic instances via linked accounts. The *breadth* of that aggregation is not uniform — some portals surface essentially all of the patient's cross-institution data, others surface little or none.
- **Evidence (revised 2026-05-31, using authoritative `organizationId` from raw scrape):** 376 MSKCC-tagged Communications break down by source organization as: UCSF 227, Sutter Health 117, Mayo Clinic 12, John Muir Health 9, Altais Health Solutions 8, MSKCC-native 3. Stanford-tagged Communications (316) all originate from Stanford with no cross-institution conversations identifiable. Stanford and the MSKCC/UCSF/Sutter/Mayo/JMH cluster do not visibly share conversations in either direction (user-confirmed by memory of UI inspection).
- **Earlier (now superseded) evidence used a keyword classifier and undercounted UCSF threads ~2.4× (~145 vs actual 344, since 117 of the 227 were UCSF-via-MarinHealth-affiliation but lacked the "ucsf" keyword).** This is a methodology lesson — see C-009.
- **Patients in sample:** 1 (Patient A)
- **Portals in sample:** MSKCC (extreme aggregator), Stanford (non-aggregator vs this cluster)
- **Confidence:** High for the *existence* claim and the *MSKCC-is-an-extreme-aggregator* observation in this dataset. Low for whether MSKCC's behavior generalizes to other patients (H-003). Low for whether Stanford's structural isolation is permanent or product of patient's specific linked-account state.
- **Generalization risk:**
  - Aggregation breadth may depend on the patient's individual linked-accounts configuration in each portal, not the portal itself. (See H-003.)
  - The Stanford-isolation observation could change if the patient enrolls in a Stanford-affiliated linked-accounts program; this is a static-in-time observation.
- **Re-evaluation trigger:**
  - Patient N+1 enrollment.
  - First non-Epic EHR encountered (claim is explicitly Epic-scoped).
  - When H-001 is resolved (changes how attribution can be measured).

---

### C-002 — Scraper-captured `organizationId` was dropped before reaching FHIR

- **Status:** `confirmed`. Recovery path identified 2026-05-31.
- **Claim:** The messages scraper (`ingest/scrapers/scrape_messages.js`) explicitly captures `organizationId` per conversation and logs the cross-institution count. None of the resulting `Communication` resources in HAPI carry an `organizationId`-derived tag. The field was lost between scrape and load.
- **Evidence:** Inspection of `Communication.meta.tag` across all 692 Communications shows only `mskcc-mychart-scrape` (376) and `stanford-myhealth-scrape` (316) under system `http://example.org/source`. No `urn:epic:org`, `urn:happy-together:source-org`, or `http://local/institution`-system tags present. The scraper output JSON contained the data; the converter did not propagate it.
- **Recovery path (added 2026-05-31):** The raw scrape JSON is still on disk at `/Users/urisarid/usarid@gmail.com/Medical/Synthesis/health-assistant/data/raw-exports/`:
  - `mskcc_messages_full.json` (2.5 MB, extracted 2026-04-01) — 376 threads each with cleanly populated `organizationId` and full WP-24 token taxonomy (`Thread.id`, `Thread.messages[].wmgId`, `Thread.messages[].author.wprKey`).
  - `stanford_messages_full.json` (797 KB, extracted 2026-04-01) — 316 per-message records; each `message.body` contains an `eMid=WP-24…` URL from which the per-message WP-24 token can be extracted.
  - Retrofit therefore does NOT require re-scraping. A revised `process_messages.py` against these raw files can backfill the existing 692 Communications with correct organizationId and WP-24 token identifiers.
- **Patients in sample:** 1
- **Confidence:** High. Direct file + database inspection.
- **Generalization risk:** None — this is a code-level observation about the existing pipeline, not an inferred behavior.
- **Action items:**
  - [Phase A' retrofit, planned] Rewrite the messages converter to (a) preserve `organizationId` as a structured tag, (b) emit both portal-local and canonical (WP-24-based) identifiers, (c) extract Stanford's eMid from body content.
  - Adopt the provenance contract (specified at end of doc) to prevent recurrence.
- **Re-evaluation trigger:** When the provenance contract lands. Verify by writing a test that any field in scraper output that isn't explicitly listed in a "drop-rationale" file must appear somewhere in the FHIR output.

---

### C-003 — MSKCC MyChart is an extreme cross-institution aggregator (N=1)

- **Status:** `n1-observed` (revised 2026-05-31, much stronger)
- **Claim:** When this patient logs into MSKCC's MyChart, the linked-accounts view exposes Communications from essentially every Epic-using institution the patient has connected (or has been connected to historically). Only 3 of 376 captured threads were MSKCC-native; the rest came from UCSF (227), Sutter Health (117), Mayo Clinic (12), John Muir Health (9), and Altais Health Solutions (8).
- **Evidence:** Direct `organizationId` attribution on raw scrape (see C-008 for organizationId→institution mapping evidence).
- **Note on the small native count (3 of 376):** User confirmed (2026-05-31) that they have had very few MSKCC-native message interactions, so the disproportion reflects actual care pattern, not scrape incompleteness.
- **Patients in sample:** 1
- **Generalization risk:**
  - Strongest version of H-003 (portal-config, not patient-specific). If H-003 is false, this conclusion is patient-specific and MSKCC's behavior could be different for other patients.
  - Two historical organizations (Mayo, John Muir) are present in this data but the user has since lost those linked-account connections (see C-010). This conclusion describes MSKCC's view at scrape time, not necessarily today.
- **Re-evaluation trigger:** Patient N+1 connects MSKCC. If their MSKCC scrape also shows broad cross-institution content, H-003 confidence increases.

---

### C-004 — Stanford MyHealth is structurally outside this linked-account cluster

- **Status:** `n1-observed` (significantly revised 2026-05-31)
- **Claim:** Stanford's MyHealth portal appears not to participate in linked-account sharing with the MSKCC/UCSF/Sutter/Mayo/JMH/Altais cluster — in either direction. No Stanford-native conversation surfaces in MSKCC's view, and no non-Stanford conversation surfaces in Stanford's view, beyond patient outgoing references to other care.
- **Evidence:** Stanford raw scrape contains 316 records, all from Stanford providers. MSKCC raw scrape contains 376 threads spanning 6 organizationIds; none correspond to Stanford. User confirmed by memory (2026-05-31): never seen a Stanford message in MSKCC/UCSF UI, or vice versa. The ~9% "Stanford-tagged with cross-institution keywords" from initial keyword analysis was patient-outgoing references to other care plus classifier false positives, not actual cross-listings.
- **Earlier framing as "weak aggregator" was misleading** — it implied a continuous spectrum where Stanford was on the low end. The evidence supports a categorical claim instead: Stanford is structurally not joined to this cluster.
- **Note:** Stanford may participate in *other* linked-account clusters (e.g., with Stanford Health Care Tri-Valley, Stanford Children's, other Stanford affiliates). This conclusion is scoped to "MSKCC/UCSF/Sutter/Mayo/JMH/Altais cluster," not "any cluster."
- **Patients in sample:** 1
- **Generalization risk:** Same as C-003 — could be patient-specific linked-account configuration. Also possible: Stanford participates in some clusters but specifically not this one for legal/business reasons; another patient might see different topology.
- **Re-evaluation trigger:** Patient N+1 connects Stanford. Also: discovery of any Stanford-cluster aggregation behavior.

---

### C-005 — Patient deduplication is absent

- **Status:** `confirmed`
- **Claim:** The HAPI FHIR database currently contains 7 distinct `Patient` resources for one human (the only enrolled patient). No canonical patient entity exists; institutional identifiers are scattered across separate resources rather than aggregated as `Patient.identifier` entries.
- **Evidence:** `GET /fhir/Patient` returns 7 entries — all the same human (Patient A) or empty-name — with distinct IDs: one Epic-format ID carrying both Stanford and UCSF Epic identifiers; one MSKCC patient ID; one MSKCC MRN; three UUID-keyed records from separate scrape runs; one local-only ID assigned by an early ingestion script.
- **Patients in sample:** 1
- **Generalization risk:** None for the existence of the problem. The shape of the fix may need to differ for patients with truly different identities at different institutions (e.g., maiden vs. married name) where merging requires explicit user confirmation.
- **Action items:**
  - Define a canonical patient identifier (probably one BinaHealth-issued UUID per human).
  - Migrate all 7 records' identifiers onto the canonical Patient.
  - Update all references in other resources to point to the canonical Patient.
  - Update converters to use the canonical Patient when minting new resources, never create new `Patient` resources from scrape data.
- **Re-evaluation trigger:** Patient N+1. The migration design must support multi-patient from day one.

---

### C-006 — Stanford and MSKCC scrapes used incompatible identifier extraction; both are recoverable

- **Status:** `confirmed` (2026-05-31)
- **Claim:** The Stanford and MSKCC message scrapes captured identifiers in entirely different fields and granularities — but both retain the underlying Epic WP-24 token, so the gap is converter-side, not API-side.
  - **MSKCC scrape:** Per-thread records. Stores `Thread.id` (per-thread WP-24 token, e.g. `WP-24fkW7q3JQ-2BYpX9t476k1uQw-3D-3D-24UX0…`), `Thread.messages[i].wmgId` (per-message WP-24 token), `Thread.messages[i].author.wprKey` (per-participant WP-24 key), and `Thread.organizationId` (per-source-org WP-24 key).
  - **Stanford scrape:** Per-message records. Stores `message.id` as a Stanford-local 9-digit numeric (e.g. `109754415`). The WP-24 per-message token is embedded in `message.body` as `eMid=WP-24…` inside the Stanford SSO disclaimer footer — present on all 316 messages but not extracted as an identifier.
  - **Comparable tokens between the two:** Stanford's `eMid` ↔ MSKCC's `wmgId` (both per-message WP-24). Stanford has no thread-level token captured; thread-level dedup across these two scrapes is not currently possible.
- **Evidence:** Raw scrape inspection 2026-05-31. MSKCC: 5,081 WP-24 occurrences across 1,670 unique tokens (376 thread + 809 wmgId + 6 wprKey + 6 organizationId + duplicates across nested context). Stanford: 316 WP-24 occurrences = 316 unique = exactly one per message, all inside body fields, none in id fields.
- **Implication for retrofit:** Stanford converter needs a 3-line addition to regex-extract eMid; MSKCC converter needs to retain the WP-24 tokens it already has access to. Both are small.
- **Implication for H-001:** Test target is now precisely defined — when UCSF native scrape lands (Phase A'), the WP-24 in (Stanford's eMid | UCSF's wmgId | MSKCC's wmgId) for the same message should be identical.
- **Patients in sample:** 1
- **Confidence:** High — direct file inspection.
- **Re-evaluation trigger:** When the retrofit converter is written and the WP-24 tokens are populated into Communication identifiers in HAPI.

---

### C-007 — MSKCC scrape captures per-thread records; Stanford captures per-message

- **Status:** `confirmed` (2026-05-31)
- **Claim:** The two scrapes operate at different logical granularities. MSKCC's scraper hits `/MyChart/api/conversations/GetConversationDetails` which returns a thread with nested message array. Stanford's scraper hits an endpoint that returns one message per record (no thread wrapper).
- **Implications:**
  - Comparing thread counts across the two scrapes (376 vs 316) is not directly meaningful — they're different units of analysis.
  - Aggregating Stanford messages into threads requires either (a) Stanford to expose a thread-level identifier we extract, (b) heuristic grouping (subject + participants + time window), or (c) a separate API call to a Stanford thread endpoint we haven't yet identified.
  - Cross-portal dedup (per H-001) currently only works at the per-message level, not per-thread.
- **Evidence:** Top-level structure inspection. MSKCC top-level: `{extractedAt, source, totalThreads, threads:[…]}` with 376 threads each containing `messages:[…]`. Stanford top-level: 316-item list of message records.
- **Patients in sample:** 1
- **Confidence:** High.
- **Re-evaluation trigger:** Discovery of Stanford thread-level API endpoint. UCSF native scrape (will tell us if UCSF's API behaves like MSKCC's or Stanford's).

---

### C-008 — Six `organizationId` values in MSKCC scrape mapped to specific institutions

- **Status:** `n1-observed` (2026-05-31)
- **Claim:** The MSKCC raw scrape's 376 threads carry exactly 6 distinct `organizationId` values (5 non-empty + 1 empty). Mapping established via combined evidence from platform sender labels, top-sender names cross-referenced to web verification, and content keyword distribution. Mapping:

| organizationId (truncated) | Threads | Institution | Confidence anchor |
|---|---|---|---|
| `WP-24yD5PqNx65CfT8gg…` | 227 | **UCSF MyChart** (hosts UCSF Health and MarinHealth-affiliate content) | Platform sender label "UCSF MyChart Messaging User"; user confirmed MarinHealth uses UCSF's MyChart |
| `WP-24Jp9qQ5GJa…` | 117 | **Sutter Health** | Top sender (a concierge PCP) is publicly listed on sutterhealth.org; user confirmed |
| `WP-24zwNkFkkFFFLLU…` | 12 | **Mayo Clinic** | Top senders include Mayo hematology clinicians; thread body cites "Mayo Health Tapestry IRB #19-000001" |
| `WP-24cDE-2BFR1y…` | 9 | **John Muir Health** | Platform sender label literally "John Muir Health MyChart Team" |
| `WP-24TrGp4k5RnhZW…` | 8 | **Altais Health Solutions** | Top sender (a gastroenterologist) publicly listed on altais.com; user confirmed |
| (empty) | 3 | **MSKCC** (native, this portal) | Senders are MSKCC hematology/oncology providers |

- **Important nuances:**
  - **MarinHealth content is inside the UCSF organizationId**, because MarinHealth uses UCSF's MyChart instance under their clinical affiliation (post-2010, when Sutter management ended). MarinHealth is not part of UCSF in an ownership sense.
  - **Two organizationIds (Mayo, John Muir Health) correspond to connections the user has since lost** — see C-010. Their historical data persists in MSKCC's view at scrape time.
  - **Hoag** is in the user's currently-connected MyChart list but is absent from this scrape — likely connected after the scrape window (April 2026) or with no message activity yet.
- **Patients in sample:** 1
- **Confidence:** Very high for the mapping (user confirmed each via memory + external web verification of clinicians).
- **Generalization risk:** The specific organizationIds and counts are patient-specific by definition. The *mechanism* (organizationId reliably identifies source Epic instance) is portable.
- **Re-evaluation trigger:** When the retrofit lands and we can test the mapping by tag-counting in HAPI. Patient N+1 (will introduce new organizationIds we'll need to map similarly).

---

### C-009 — METHODOLOGY: clinician name is not a reliable proxy for Epic organization

- **Status:** `confirmed` (2026-05-31)
- **Claim:** When determining institutional attribution of message content, the clinician's name (or human-known practice affiliation) is a *weaker* signal than the platform sender label or the `organizationId`. Concierge doctors, doctors with multiple appointments, doctors at affiliated/JV practices, and doctors whose practices have changed ownership all create attribution traps.
- **Evidence of the trap, in chronological order during this audit:**
  - **A concierge PCP** appeared as a top sender (122 mentions) in one organizationId's threads. I initially labeled the org "UCSF" based on the clinician's training affiliation visible in public listings. User correction: the clinician is a concierge doctor whose Epic messages route via Sutter (per the clinician's own practice page on sutterhealth.org). The correct org was Sutter Health.
  - **The patient's primary care physician** at a UCSF-affiliated MarinHealth clinic was classified `Sutter/PCP` in the existing `process_messages.py` keyword list. User correction: this PCP is at MarinHealth, which is now affiliated with UCSF and uses UCSF's MyChart. The "Sutter" classification reflected MarinHealth's pre-2010 management contract with Sutter. Outdated knowledge embedded in the classifier was actively misleading.
- **Stronger signals (in order of reliability):**
  1. `organizationId` on the raw API response — this is the authoritative source.
  2. Platform sender labels (e.g., "UCSF MyChart Messaging User", "John Muir Health MyChart Team"). These are system-generated and name the actual Epic instance.
  3. Clinician's *current* practice affiliation, verified from their practice's actual website.
  4. Body content keywords (weakest — confounded by patient outgoing references to other care).
- **Discipline forward:** Never silently translate from clinician name to institutional tag. Always anchor on organizationId from raw scrape; verify any clinician-name-based mapping against the practice's current public listing.
- **Patients in sample:** 1, but the *mechanism* is independent of patient.
- **Confidence:** High.
- **Generalization risk:** None — this is a methodology rule, not a patient-specific claim.
- **Re-evaluation trigger:** Any time we're tempted to use clinician name as a primary attribution signal.

---

### C-010 — MyChart linked-accounts connections expire silently

- **Status:** `n1-observed` (2026-05-31)
- **Claim:** Linked-account connections in MyChart (Epic's "Happy Together"/Care Everywhere patient-facing feature) become inactive over time without user notification. Historical data captured before the lapse persists in the receiving portal's view; new data from the lapsed source stops arriving. Reconnection is user-initiated, requires the user to notice the gap, and is not surfaced proactively by the MyChart UI.
- **Evidence:** User confirmed (2026-05-31): "MyChart accounts occasionally disconnect, e.g., I don't see Mayo Health there any more. Sutter Health was also disconnected and I needed to reconnect." Mayo and John Muir Health appear in the April 2026 MSKCC raw scrape with thread dates spanning 2022-2023 but no recent activity; both are absent from the user's currently-active MyChart linked-accounts list.
- **Implications for the product:**
  - **The depth-data pipeline silently degrades** for every user over time if reconnection isn't prompted. Detection heuristics needed: per-organizationId last-seen-message timestamp, drop-off in expected message cadence relative to patient's care pattern.
  - **"Institutions you may have forgotten" feature** (PRODUCT_INSIGHTS_LOG.md P-001 adjacent move) must distinguish four states, not two:
    1. *Connected, active* — recent messages
    2. *Connected, dormant* — linked but quiet (could be intentional, could be early-warning of imminent lapse)
    3. *Disconnected, was previously linked* — Mayo case
    4. *Never connected, but referenced* — Hoag case (in UI list, no messages yet) or org mentioned in a referral but not linked
  - **OAuth refresh tokens** (90-day Epic patient-access lifetime) are the direct analog of this for our own SMART-on-FHIR integrations. Same underlying ratchet, different surface.
- **Implications for the retrofit:** All 12 Mayo Communications and 9 John Muir Communications already in HAPI represent real care and must be preserved. The retrofit must not conflate "connection currently active" with "data is valuable."
- **Implications for H-001:** Probably orthogonal — WP-24 tokens are Epic global IDs, not session-bound, so disconnect/reconnect cycles likely don't change them. Worth verifying when test runs.
- **Patients in sample:** 1
- **Confidence:** High for the existence claim (user-confirmed). Low for the mechanism (whether lapse is time-based, activity-based, policy-based, security-based).
- **Generalization risk:** Likely a property of MyChart linked-accounts itself, not patient-specific. Other patients should experience the same silent expiration.
- **Re-evaluation trigger:** Patient N+1 has been enrolled long enough to observe one or more disconnect events.

---

### C-011 — v1 production silently lost ~80% of MSKCC message content via wrong source file

- **Status:** `confirmed` (2026-06-04)
- **Claim:** v1 production messages were loaded from `mskcc_messages_scraped.json` (157 KB, preview-only — first ~200 chars of the first message per thread) rather than `mskcc_messages_full.json` (2.5 MB, complete nested message payloads). The converter (`ingest/scrapers/process_messages.py`) handles full nested responses correctly when given them; it just never received them. Net result: 373 of 376 MSKCC threads in v1 contain only a single preview-truncated snippet, where the raw scrape had captured 2–13+ messages per thread including all the clinically substantive provider replies.
- **Evidence:** v1↔v2 diff run 2026-06-04 (`tools/v2/out/diff_communications_report.jsonl`). 158 MSKCC pairs show `payload_compare = len-differs` (v1=1, v2=2-13+ messages). 26 single-message pairs show `payload_compare = body-differs` where v1 contains a ~200-char preview snippet and v2 contains the full body. Sample: a representative cross-institution thread has 1 payload in v1 and 5 in v2; v1 is missing substantive provider replies that include treatment-milestone updates and follow-up recommendations. **The v1 AI assistant has been answering questions on this 20%-completeness slice for the entire production lifetime.**
- **Root cause:** Loader file selection. Both files coexisted in `data/raw-exports/`; the loader script chose the smaller (older?) one. The provenance contract would have caught this if `source_file` had been preserved as a tag on every resource.
- **Patients in sample:** 1
- **Confidence:** Very high — direct evidence in two HAPI instances and on disk.
- **Generalization risk:** Other resource types (Observation, DocumentReference, Encounter) likely have analogous loader-file-selection bugs. Phase F of the rebuild plan (extend v2 to remaining resource types) is now elevated in priority — this is the kind of finding that recurs across resource types.
- **Action items:**
  - v2 rebuild fixes this (already loaded from `mskcc_messages_full.json`).
  - Update provenance contract spec to make `source_file` a **required** tag on every resource, with a CI lint that fails if absent.
  - Add a "smallest-file-wins" warning: when two raw exports exist for the same resource type, prefer the larger one absent explicit configuration that documents why.
- **Violates principle:** P-DATA-IS-GOLD.
- **Re-evaluation trigger:** When v2 extends to other resource types and we can check whether the same bug pattern recurred elsewhere. Expect at least one analogous finding.

---

### C-012 — v1 strips ~17% of clinical note text by rendering HTML to plaintext before storage

- **Status:** `confirmed` (2026-06-04)
- **Claim:** v1's UCSF clinical-notes ingestion stripped HTML to plaintext before storing in `DocumentReference.content.attachment.data`. v2 preserves the original HTML (stored with `contentType=text/html`, base64-encoded). On a per-note basis, the stripped-text recovered from v2's preserved HTML is on average 17% longer than v1's stored plaintext. The recovered content is structural — formatted sections, tables, headers, footers — that the v1 stripping discarded silently.
- **Evidence:** v1↔v2 diff 2026-06-04 (`tools/v2/convert_notes.py` + `/tmp/diff_notes_v2.py`). 102/108 notes matched by (date, author). Of those: 94 pairs have v2 producing more text than v1, 2 have v2 producing less, 6 are identical. Total stripped-text chars: v1 = 593,753, v2 = 694,543 (+17.0%).
- **Why some pairs are smaller in v2 (2 of 102):** likely v1 included some boilerplate (e.g., document footers) that v2's strip_html discards differently. Not a data loss in v2 — same source HTML retained, different stripping. Worth a spot-check but lower priority than the 94-pair gain.
- **Implications:**
  - **Operationalizes P-DATA-IS-GOLD for note content.** Strip-and-store loses structure; preserve-original + strip-on-render keeps options open.
  - **Required for H-005** (note dedup via entity linking). The entity-linking pass needs the document's structural cues — labs are typically in a `<table>` or labeled section, not free prose. Plaintext loses those cues.
  - **Index/search implications.** Existing API consumers reading `Communication.payload[].contentString` see only stripped text. The HTML attachment is a separate channel. The API may want a derived "as_text" view at query time, computed from HTML, rather than re-storing both.
- **Action items:**
  - v2 captures this correctly already; no further converter work needed for notes.
  - When the API moves to consume from v2, add an HTML-strip-on-render helper rather than a stored plaintext copy.
- **Patients in sample:** 1, but the mechanism (strip-before-store loses structure) is patient-independent.
- **Confidence:** High.
- **Generalization risk:** Other resource types likely have analogous strip-before-store bugs. DocumentReferences for Stanford, MSKCC etc. should be checked when their v2 converters land.
- **Re-evaluation trigger:** When Stanford/MSKCC notes get v2 converters and we can confirm the pattern.

**Related side-finding** (not promoted to its own entry): 6 UCSF "Hospital Visit" notes in the raw scrape have empty `date`, `provider`, and `department` metadata. v2 faithfully preserves the empty fields. v1 mysteriously had dates for 4 of those 6 — extraction mechanism unknown. Possibly v1 ingestion path retrieved metadata from a richer scrape that isn't checked in. Worth investigating if v3 scraper architecture aims to recover these; not blocking.

---

### H-005 — Hypothesis: doctor's notes can be de-duplicated via entity linking + temporal matching, with quoted dumps replaced by typed references

- **Status:** `partial-confirmation` (prototype 2026-06-04). Carries principle P-DEDUP-CARRY-PROVENANCE.

**Empirical results from medication-extraction prototype (`tools/v2/h005_med_entity_linking.py`, 2026-06-04):**

Tested against a single UCSF Office Visit note (`docref-ucsf-b7a8f3cc8c22`, 184 KB HTML, 25 tables, 16 KB stripped text, dated 2024-10-03).

- **Extraction:** 22 unique candidate medication strings extracted from all table cells using two heuristics — Epic mixed-case detection (`[a-z][A-Z]`) and dose-form suffix detection (`\d+\s*(mg|mcg|...) (tablet|capsule|...)`). Of the 22, ~14 are real meds and ~8 are SIG strings ("Take 1 tablet by mouth twice daily") that the heuristic incorrectly picked up.
- **Match rate:** 14 of 22 candidates (64%) had at least one exact match against v1's MedicationRequest corpus by normalised-name. After excluding the SIG-string false positives, **the match rate on real medications was ~14 of ~14 (essentially 100%)**.
- **Multi-match resolution:** each name typically matched many MRs (e.g., losartan → 22 MRs, atorvastatin → 22, bortezomib → 25), because the patient has been prescribed each medication multiple times over years.
- **Date-aware selection works.** Picking the MR with `authoredOn` closest to the note date produces sensible "best matches":
  - tamsulosin: ±0 days (prescribed at this exact visit)
  - losartan: ±6 days (same prescription cycle)
  - atorvastatin: ±48 days, amlodipine: ±70 days, propranolol: ±150 days
  - aspirin/clopidogrel: ±272 days (older active regimen)
- **All 739 v1 MRs have `authoredOn`** populated (100%), so temporal matching is universally available.

**What this means for H-005:**

The core hypothesis works for medications. A note that includes a "Current Medications" or "Patient-reported Medications" table can be processed into a list of typed `MedicationRequest` references with high fidelity. The replacement output ("Patient is currently on [MedicationRequest:e9H0wcP7rXlk1erKgzLyzgns] (losartan), [MedicationRequest:e7x3VCzBN9LHAMx1GSy.8AOM] (tamsulosin), …") preserves the doctor's clinical reasoning context without re-storing the dose-form-sig text.

**Production gaps (known after prototype, not yet built):**

1. **Cell filtering.** The SIG strings that leaked in are easy to filter once we know the pattern ("Take" / "by mouth" / numbered-quantity prefix). Estimate: small fix.
2. **Multi-ingredient combinations.** "ascorbic acid, vitamin C" and "cholecalciferol, vitamin D3" failed because normalisation kept trailing commas. Needs splitting on commas and trying each component.
3. **Brand→generic crosswalk.** Currently works only because Epic stores both the generic + brand (e.g., "losartan (COZAAR)") so the generic survives normalisation. If a note had only brand text ("COZAAR 50 mg tablet"), match would fail. RxNorm lookup needed.
4. **Strength validation.** A match on "amlodipine" might be the wrong amlodipine MR if doses differ. Need to compare dose-form from the note cell against the matched MR's strength.
5. **Cross-resource pattern.** This validates medications. The same approach for AllergyIntolerance (Allergies table), Condition (Past Medical History table), and Procedure (Surgical History table) should work — each note we inspected has those tables. Worth a follow-up prototype.

**Surprises along the way:**

- v1 stores med names in `medicationReference.display`, not `medicationCodeableConcept.text` — see C-014. The first prototype run found 7 distinct names; switching fields found 214.
- Epic's mixed-case formatting (amLODIPine, hydroCHLOROthiazide) is preserved both in the note HTML AND in v1's stored MR text — match is essentially free, no case normalisation needed.
- The patient's medication history is dense (22 losartan MRs, 22 atorvastatin MRs) — date-based selection isn't optional; without it the reference is ambiguous.
- The note's "Current Medications" list is a *snapshot* of the patient's regimen, not a list of just-prescribed orders. Some meds matched ±0 days (prescribed at this visit), most matched 1-9 months (ongoing regimen). This is correct clinical workflow — the note describes the state at the moment of authorship.
- 27 tables in a single note. The note isn't just prose — it's a heavily structured document with embedded clinical data dumps. Entity-linking on tables is the high-leverage surface; prose-level extraction is the long tail.

**Status remains `partial-confirmation`** because:
- Tested on N=1 patient, N=1 note, single resource type (medications).
- Production gaps above need closing before the replace-with-reference output is reliable enough to ship.
- Need to validate on other resource types (allergies, conditions, procedures) and other portals (Stanford, MSKCC have notes too).

**Follow-up prototype 2026-06-04 (`tools/v2/h005_multi_resource_entity_linking.py`):** extended the mechanism to Allergies, Past Medical History, and Vitals on the same note.

- **Allergies → AllergyIntolerance:** 1/1 = 100% match. The v1 corpus is small (3 AIs, of which 2 are noise — 'unknown' and 'nka') so the test is qualitative; the mechanism does what it should.
- **PMH → Condition:** 12 exact + 2 partial = 14/30 match (47%). Of the 16 unmatched, ~8 are comment-only sub-rows (production prototype gap, easy fix) and ~8-9 are real diagnoses not represented in v1 → this surfaced **C-015**, an analogue of C-011 for problem lists.
- **Vitals → Observation:** 4/4 real vital values matched, all ±0 days from the note (same-day vitals panel). BP, Pulse, SpO2, Weight all resolved to the same Observation panel (`eA7wCL2.BuYlcWycMIRRWt2VO0yc3B5t`). Strong same-day-correspondence signal, same as for medications prescribed at this visit (tamsulosin ±0d).

**Two patterns generalised:**
- **Same-day correspondence** — when a clinical event happens during a visit (vitals collected, med prescribed), the note's table entry and the structured resource share a precise date. Date-based selection is the strongest single signal.
- **Notes contain more than HAPI knows** — both meds (via fresh prescription noted) and conditions (via doctor-maintained problem list) can be richer in notes than in the structured store. Entity linking is therefore bidirectional: dedup *and* augment.

**Next prototype experiments worth running** (in approximate decreasing value):

1. Run the multi-resource prototype across all 18 UCSF Office Visit notes; aggregate the unmatched-real-diagnoses count to size the C-015 gap.
2. Test against Stanford notes (different EHR's note format — does the same table-extraction approach work?).
3. Compare structured-table extraction against prose-level extraction in the same note (does prose add anything tables don't already capture, or is it pure redundancy?).
4. Build the actual "replace-with-reference" rendering and inspect for readability.
5. Map ANA positive and similar "lab finding" PMH entries to Observation, not Condition — needs a routing layer.

- **Re-evaluation trigger:** Any of the above experiments produces a meaningfully different result. Or new patient enrols.

---

### H-001 — Hypothesis: Epic `WP-24…` thread tokens are portable across portals
- **Claim:** When ingesting a clinical note that contains a "Recent labs:" dump, a problem list snapshot, a medication list, or a quoted prior message, the duplicative content can be:
  1. Identified via structured-data lookups (LOINC matching for labs, ICD-10/SNOMED for problems, RxNorm for medications, message identifiers for quotes).
  2. Temporally bounded (date-of-note ± window, matched against existing resources' effective dates).
  3. Replaced in place by typed references (e.g., `[Observation: CBC w/diff 2026-03-15]`) that preserve the doctor's reasoning context without storing redundant data.
- **Why it matters:**
  - **Assistant accuracy.** Without dedup, a query like "when was my last Hgb test" returns false-positive matches in notes that cite the lab in passing. The dedup-with-reference approach lets the assistant distinguish "the doctor saw this data" from "this is new data."
  - **Storage and reindex cost.** Note narrative containing duplicated lab dumps inflates resource size, full-text index size, and embedding cost.
  - **Clinical reasoning preservation.** The doctor's "Plan:" section is anchored to the cited context. Replacing with references preserves linkage; stripping context loses it.
  - **Cross-source canonicalization.** Same lab value scraped via MSKCC's linked-accounts view of UCSF *and* scraped via UCSF natively should resolve to one canonical Observation with multiple `provenance` entries, not two duplicates.
- **Test plan:**
  - Pick 5 known clinical notes from UCSF/Stanford that contain a labs-dump section.
  - Run an entity-linking pass against existing HAPI Observations using LOINC + date matching.
  - Manually validate: did each cited value resolve to the right Observation? Count false matches and missed matches.
  - Generate the "replace with reference" output. Inspect for readability — does the dereferenced note still convey clinical meaning?
- **Open design questions:**
  - **Matching aggressiveness.** False matches (linking the wrong Observation) are worse than no match. What confidence threshold?
  - **UI surfacing.** Inline reference expansion, hover popups, separate context panel?
  - **Quoted messages.** When a note quotes a prior message from another clinician, and we already have that message as a Communication resource, the same dedup pattern applies. Worth handling uniformly.
  - **Storage shape.** Store de-duplicated note + reverse-map of replacements? Or store original + a derived "duplicates resolved" view? The second is safer (original is never lost — P-DATA-IS-GOLD); the first is cheaper.
  - **Handling near-duplicates.** What if the note quotes a slightly-out-of-date version of a Medication list? Reference + note-of-divergence? Or treat as new?
- **Dependencies:**
  - Entity linking infrastructure not yet built. Will need terminology service for code matching (existing `lib/loinc_synonyms.py` and `lib/loinc_mapper.py` are starting points for labs).
  - Patient-canonical identity (C-005) must be resolved first so references point to the right patient's resources.
  - DocumentReference Binary content must be reliably retrievable (a separate problem flagged in the audit).
- **Re-evaluation trigger:** Once Phase F (extend v2 to DocumentReference and Observation) lands and we have a substantial cross-source corpus to test against.

---

### C-013 — Stanford visits ingestion has no v1 data-loss bug; minor v2 enrichments

- **Status:** `confirmed` (2026-06-04)
- **Claim:** Unlike C-011 (messages) and C-012 (notes), v1's Stanford visits ingestion is essentially complete. All 139 raw visits map cleanly to v1 Encounters by CSN, and the v1 converter preserves the core clinical fields (visit type, period, provider, service department). v2 reproduces the same 139 Encounters with:
  - Added provenance tags (source-portal, source-org, source-org-id, source-file, scraper-version, converter-version).
  - Preservation of the `_orgKey` WP-24 token from the raw scrape (carried as `urn:bina:source-org-id` tag) — previously dropped in v1.
  - Preservation of IsLocal + clinical-note-available + cancelled + no-show + ED + surgery flags as `urn:bina:encounter-flag` tags. Per C-001/C-004 the IsLocal flag is meaningful for cross-institution attribution; all 139 Stanford visits are IsLocal=true, consistent with C-004.
  - Canonical Epic encounter identifier (`urn:bina:epic:encounter`) duplicated alongside the portal-local identifier, in anticipation of cross-portal dedup (H-001 carried over to encounters).
- **Methodology note** (worth remembering for other resource types): FHIR R4 `Encounter` has no `note` field. HAPI silently drops unknown fields during ingest. v2's first cut emitted Encounter with `note` and lost the IsLocal text until a verification query against v2 HAPI surfaced 0/139 notes populated. Fix: use `meta.tag` for boolean flags on resource types that don't model free-text notes. This is the v2-converter-side cost of P-DATA-IS-GOLD — always validate that what the converter emits actually round-trips through HAPI.
- **Evidence:** v1↔v2 diff 2026-06-04. v1 has 139 Stanford Encounters tagged `stanford-myhealth-visits`; v2 has 139 tagged `urn:bina:source-org|Stanford`. 100% CSN intersection. v2 tag distribution: 139 is-local, 105 clinical-note-available, 2 surgery (matches expected from raw).
- **Patients in sample:** 1
- **Confidence:** High.
- **Generalization risk:** Stanford visits handled well in v1 doesn't mean Stanford test results / UCSF visits / etc. will be. The next conversion (Stanford test results) is the relevant next data point.
- **Re-evaluation trigger:** When v2 expands to other resource types or to patient N+1.

---

### C-017 — AHR-ingested DocumentReferences across ALL institutions are metadata stubs (Binary content missing)

- **Status:** `confirmed` (2026-06-04)
- **Claim:** Across v1's full DocumentReference corpus (673 resources), the institutions with significant counts (Stanford 166, UCSF ~284 of 392, Sutter 67, Mayo 14, MSKCC 10) carry DocumentReferences whose `content[0].attachment.url` is a `Binary/<id>` reference — but the referenced Binary resources are not in HAPI. Sample test: 30 documents per institution, 0 of 30 Binary URLs resolved for Stanford, UCSF (AHR-sourced subset), Sutter, Mayo, or MSKCC. HAPI has only 133 Binary resources total, none of which match any sampled Binary ID. **The only DocumentReferences in v1 with usable inline content are the 108 UCSF clinical notes ingested via the direct UCSF scraper (`scrape_ucsf_notes.js`) and 2 patient-entered records — everything else is dead-link metadata.**
- **Background:** CLAUDE.md notes this scraping limitation qualitatively for Stanford ("Stanford DocumentReferences exist but their Binary content was never ingested"). This entry generalises the finding: the pattern is the Apple Health Records ingestion pipeline. AHR captures DocumentReference metadata cleanly but doesn't follow through to fetch the Binary content the references point to.
- **Why it matters:**
  - **The clinical-narrative corpus available to the assistant is ~16% of what it appears to be.** 108 of 673 DocumentReferences have content (the UCSF-scraped notes). The other 565 are dead pointers. Any "search across my clinical notes" query is silently working with a 1-in-6 coverage slice.
  - **The H-005 findings (C-015, C-016) are lower bounds.** They were measured against the 108 notes that have content. Adding the missing ~565 notes' worth of PMH dumps, allergy lists, and vital tables would likely surface significantly more unmatched diagnoses (C-015 gap could double or more).
  - **The fix is patient-side.** AHR can't be modified; the gap is what AHR returns. The fix is to scrape each institution's notes directly using its API (the pattern proven by `scrape_ucsf_notes.js`).
- **Root cause (audited 2026-06-04):** Apple Health Records exports DocumentReference metadata *intentionally without* the referenced Binary resources. Inspection of the raw AHR export at `~/usarid@gmail.com/Medical/New exports/apple_health_export/clinical-records/`:
  - 463 DocumentReference files
  - 0 with inline `data`
  - 462 with url-only references (`"url": "Binary/<id>"`)
  - 0 Binary-*.json files in the export (`ls Binary-*` returns nothing)
  - The ingestion pipeline (`ingest/apple/ingest_clinical.py`) is correct — it just passes the DocRefs through. The data isn't there to ingest.
- **But the content IS recoverable via Epic on FHIR.** AHR's DocRefs carry the information needed to fetch each Binary from its source:
  - **`custodian.display`** identifies the source EHR cleanly: UCSF 214 (incl. MarinHealth), Stanford 165, Sutter 67, Mayo 14, plus 2 non-Epic (Cerner-based DHMG). All four Epic-based custodians have Epic on FHIR endpoints.
  - **`identifier.system` OIDs** are Epic instance identifiers (`urn:oid:1.2.840.114350.1.13.266.2.7.2.727879` → UCSF, etc.) — a second, machine-readable signal.
  - **Binary URLs are Epic-format opaque tokens** (e.g., `Binary/eowYamhSa84WbGDUQg4DLOEc.UB8iWqXdgktZyppPDgw3`) — the same format Epic on FHIR's `/Binary/{id}` endpoint serves natively.
  - **`api/epic_oauth.py` already implements the PKCE flow** for patient-facing Epic on FHIR — the infrastructure is in place. Per-EHR OAuth tokens are the only missing piece.
- **The recovery path (per AHR DocRef):**
  1. Read `custodian.display` → map to known Epic FHIR endpoint (lookup table).
  2. Use that source EHR's stored OAuth token.
  3. GET `<source-fhir-base>/Binary/<id>` with `Authorization: Bearer <token>`.
  4. Replace `attachment.url` with `attachment.data` (base64 of the response) — or store the Binary resource locally and keep the URL pointing at HAPI.
  - Total addressable: 461 of 463 DocRefs (excludes 2 Cerner-based).
- **Gating constraint (audited 2026-06-04, this is the binding blocker):** the existing Epic on FHIR setup does NOT have the OAuth tokens needed for recovery.
  - The single row in `epic_tokens` is connected to `fhir.epic.com/interconnect-fhir-oauth/...` — Epic's **test sandbox**, not any real EHR. The patient_id `eD.LxhDyX35TntF77l7etUA3` is the sandbox test patient. Expired 2026-04-16.
  - **0 of 4 real source EHRs** (UCSF, Stanford, Sutter, Mayo) have authorised OAuth tokens.
  - **The schema is single-tenant** (`CHECK (id = 1)`); only one token can be stored. The table must be redesigned to be one row per (patient × source EHR `fhir_base`) before any multi-EHR OAuth can work.
  - **`EPIC_CLIENT_ID` is not set in the running container's env** — the OAuth flow can't even be initiated against a real endpoint until the app's client_id is configured.
- **Required to make recovery real (in order):**
  1. Confirm/complete production Epic on FHIR app registration (one-time, vendor side).
  2. Submit the app for approval at each of UCSF, Stanford, Sutter, Mayo (Epic's "publish to customers" mechanism, or per-customer vetting if required). Timeline: typically days-to-weeks per customer.
  3. Code change: rewrite `epic_tokens` schema as `(fhir_base, patient_id) → token` and update `_get_token` / `_store_token` accordingly.
  4. Code change: `/api/epic/auth-url` accepts a `fhir_base` parameter to drive per-EHR auth.
  5. User-flow: log into each source EHR's MyChart in sequence, complete OAuth callback for each.
  6. Build the recovery script that walks AHR DocRefs and dispatches Binary fetches per the lookup table.
- **Steps 1-2 are outside the codebase** and gate everything else. Steps 3-4 are small. Steps 5-6 are the actual recovery and can be staged as a single session once 1-2 are in place.
- **Why it matters:**
  - **The assistant's clinical-narrative corpus would grow from 108 to up to 569 notes (108 + 461) — a 5× expansion of textual signal.**
  - **C-015 (PMH gap) and C-016 (vitals-redundant) findings are lower bounds.** Recovering Stanford / Sutter / Mayo notes will surface more PMH entries, more conditions absent from v1, more vital-sign panels for the same-day-correspondence test.
  - **No new authorisation surface is required** if the existing Epic on FHIR app is already approved for the relevant customer instances. The recovery is mechanical.
- **Patients in sample:** 1
- **Confidence:** Very high — direct null result across the AHR export plus structural recovery analysis.
- **Generalization risk:** Patient-specific in the sense that different patients have different source EHRs and OAuth coverage. The mechanism is generic — any AHR-ingested patient should have recoverable Binaries proportional to their Epic on FHIR authorisation coverage.
- **Action items:**
  - **Recovery prototype:** pick one AHR DocRef per Epic-based custodian, attempt the Epic on FHIR Binary fetch, verify the returned content is the expected HTML clinical note. ~1 hour.
  - **Production recovery script:** iterate over all 461 recoverable DocRefs, fetch Binaries, store inline in v2 HAPI. Concurrency + rate-limiting against per-EHR limits.
  - **Document the mapping:** `custodian.display` → Epic FHIR base URL → OAuth token (config in `tools/v2/patient_config/` or similar, gitignored).
- **Violates principle:** P-DATA-IS-GOLD — but the violation is at the AHR export boundary, not in our code. Recovery is the fix.
- **Re-evaluation trigger:** Recovery prototype runs; or new patient enrols with different EHR mix.

---

### C-016 — Vital signs in clinical notes are 100% redundant with same-day Observations

- **Status:** `confirmed` (2026-06-04, cumulative across all UCSF notes)
- **Claim:** Of 108 UCSF clinical notes, 8 carry a "Vitals" table (the in-person visits where vitals are actually measured; telemedicine notes don't). Across those 8 notes, 32 individual vital-sign mentions (BP, Pulse, SpO2, Weight, Temp) were extracted. **Every single one (32/32, 100%) matched a same-day Observation** in v1's vital-signs corpus. Every note's vitals fully corresponded to a same-day Observation panel. Match deltas: 100% Δ=0 days.
- **What it means:**
  - **Perfect dedup target.** Vitals mentioned in notes are not new information — they are *the same canonical record* the structured Observation panel captured. The note quotes; the structured record is the source of truth.
  - **Replace-with-reference is unambiguously safe** for vitals. No risk of losing data by inlining the reference: every value is already in the Observation.
  - **Generalises the "same-day correspondence" pattern.** First seen in the medications prototype (tamsulosin ±0d for a fresh prescription at this visit), confirmed at scale for vitals. When a clinical event happens during a visit, the note and the structured record share a precise date — they're not parallel observations; they're the same observation rendered in two formats.
- **Patients in sample:** 1
- **Confidence:** Very high. 100% match rate on 32 measurements is not coincidental.
- **Generalization risk:** Vitals are structurally simple (one metric, one numeric value, one timestamp). Labs may be similar but have richer reference ranges and units. Imaging won't follow this pattern.
- **Implication for assistant code:** the existing assistant likely treats note-mentioned vitals and Observation vitals as parallel sources. Should be treated as one. Audit recommended.
- **Re-evaluation trigger:** When applied to labs in DiagnosticReport / Observation. Or when applied across other portals (Stanford notes).

---

### C-015 — Clinical notes' PMH section is a richer problem list than v1's structured Condition store

- **Status:** `confirmed` (2026-06-04, cumulative analysis across all 108 UCSF notes)
- **Claim:** Across all 108 UCSF clinical notes in v2, 32 notes carry a "Past Medical History" table. After tightening the comment-row filter, the cumulative deduplicated diagnoses across those 32 notes number **22 unique normalised names**. 13 of them match a `Condition` in v1's HAPI (59%); **9 of them — every single one appearing in at least 2 notes, with 4 of them appearing in all 32 PMH-bearing notes — have no matching `Condition` in v1.** The gap is bounded but high-confidence-real. List, with note-recurrence and first-recorded date where present:

  | Note recurrence | Date in PMH | Diagnosis |
  |---|---|---|
  | 32 / 32 | — | Abdominal bloating |
  | 32 / 32 | — | ANA positive (note: a lab finding, routes to Observation not Condition) |
  | 32 / 32 | — | Bacterial overgrowth syndrome |
  | 32 / 32 | — | Skipped heart beats |
  | 24 / 32 | Childhood | Allergy (umbrella; better modelled as AllergyIntolerance) |
  | 24 / 32 | 2002 | GERD (gastroesophageal reflux disease) |
  | 24 / 32 | 2003 | Low back pain |
  | 15 / 32 | — | Claudication |
  | 9 / 32 | — | SVT (supraventricular tachycardia) |

  After routing "ANA positive" to Observation and "Allergy" to AllergyIntolerance, the true Condition gap is **7 diagnoses**. v1's Condition store would grow from 65 to 72.
- **Why this matters:**
  - **Analogue of C-011 for problem lists.** v1's clinical reasoning (assistant, analyst, profile-builder) sees 65 Conditions; the actual clinician-curated problem list in a single note is materially larger. Across 108 notes the gap is likely substantially bigger.
  - **GERD (2002), Low back pain (2003), Paronychia (10/15/2015)** — entries in PMH carry precise onset dates that v1 doesn't have for the few Conditions it does store.
  - **The fix is entity-linking the other direction.** v1's Condition store can be *augmented* from PMH table extraction across all clinical notes — not just dedup'd from them. This is the inverse of H-005's "replace with reference" goal; it's "extract to populate."
- **Methodology limitation found:** the prototype's bullet-row detector also picks up sub-comment rows ("Using PPI to control", "Neg BMBx + PET scan.") that aren't diagnoses. Counted as 8 of the 16 "unmatched" cases. Production needs explicit comment-row filtering.
- **Patients in sample:** 1
- **Confidence:** High for the existence of the gap; the count (9+ unmatched real diagnoses) is a lower bound — single-note test.
- **Generalization risk:** Other notes will surface more unique diagnoses. The cumulative gap across 108 notes likely exceeds 50 real diagnoses missing from v1's Condition store.
- **Action items:**
  - Treat C-015 as a v3-pipeline requirement: PMH-table extraction → Condition resource synthesis with deterministic IDs (deduped across notes that share diagnoses).
  - Audit the existing assistant/analyst for "list my conditions" type queries — they're currently incomplete.
- **Violates principle:** P-DATA-IS-GOLD.
- **Re-evaluation trigger:** When the H-005 prototype is run across all 108 UCSF notes and the cumulative unmatched-diagnoses count is measured.

---

### C-014 — v1 stores medication name in `medicationReference.display`, not the FHIR-canonical text field

- **Status:** `confirmed` (2026-06-04)
- **Claim:** v1 production's MedicationRequest resources put the human-readable medication name in `medicationReference.display` (731 of 739 MRs, 99%) rather than in `medicationCodeableConcept.text` (8 of 739, 1% — mostly patient-entered/patient-reported additions). 717 of 739 also carry a `contained[]` Medication resource with full ATC + RxNorm coding. Any text-based entity-linking pass that only consults `medicationCodeableConcept.text` will see essentially empty data.
- **Why this matters:**
  - **Direct cost of the C-009 / P-DATA-IS-GOLD pattern.** An H-005 prototype that checked only the canonical FHIR field initially saw 7 distinct med names from 739 MRs; after also checking `medicationReference.display`, it found 214 distinct names. The text was always there — just in a different field than the obvious one.
  - **Future H-005 production code must read both** `medicationReference.display` and `medicationCodeableConcept.text`, and ideally also walk into `contained[].code.coding` for RxNorm-coded entity linking once an RxNorm crosswalk is wired in.
  - **Existing API consumers (the assistant, the analyst)** likely have analogous gaps — code expecting the canonical place will under-count medications. Worth auditing.
- **Patients in sample:** 1
- **Confidence:** High — direct query against HAPI.
- **Generalization risk:** Other resource types may have analogous storage-shape surprises. For Observation we know the canonical place (`valueQuantity` / `code`) is used; for AllergyIntolerance / Condition / Procedure we haven't checked.
- **Re-evaluation trigger:** When the H-005 prototype is generalised to other resource types or when API consumers are audited.

---

### H-001 — Hypothesis: Epic `WP-24…` thread tokens are portable across portals

- **Status:** `hypothesis` (test plan refined 2026-05-31; existing data does not support a test)
- **Claim:** The `WP-24…` tokens in MyChart conversation identifiers are global Epic identifiers — i.e., the same conversation (or message) viewed from MSKCC and from UCSF carries the same `WP-24…` value, and can therefore be used as a canonical deduplication key. Specifically:
  - **Per-thread:** the value MSKCC stores in `Thread.id` matches what other portals store in their equivalent thread identifier.
  - **Per-message:** the value MSKCC stores in `Thread.messages[i].wmgId` matches what other portals store as the message identifier (Stanford's `eMid`, UCSF's equivalent).
- **Why existing data can't test this:**
  - Stanford ↔ MSKCC have no shared conversations (C-001/C-004); the two scrapes target disjoint cluster memberships.
  - We have no UCSF native scrape yet — so the 227 UCSF threads visible via MSKCC have no comparison target.
- **Refined test plan (depends on Phase A' Sub-step: UCSF native messages scrape):**
  1. Run UCSF native message scrape — Phase A' deliverable.
  2. Pick a high-volume cross-listed conversation (e.g., any in the MSKCC scrape with `organizationId = WP-24yD5PqNx…` (UCSF)) and locate the same conversation in the UCSF native scrape by subject + sent timestamp + participants.
  3. Compare `Thread.id` and per-message `wmgId` between the two scrapes.
  4. Repeat for 10–20 conversations spanning recent and older dates to test stability over time.
  - **If identical across 100% of test pairs:** H-001 confirmed → `WP-24` is the canonical conversation+message identifier; dedup runs on this.
  - **If identical with sporadic exceptions:** H-001 partially confirmed → canonical with caveats (investigate exceptions; possibly cluster-renumbering events on the Epic side).
  - **If different:** H-001 disproven → canonical key must come from elsewhere (combination of subject + first-message timestamp + participant identities). Update dedup design.
- **Dependencies blocking other work:** C-002 retrofit (already designed without H-001 dependence), conversation dedup design (in revised Phase E — held until H-001 resolved).
- **Re-evaluation trigger:** Completion of UCSF native messages scrape. Or, alternative test path: a single manual interactive verification where the user logs into MSKCC and UCSF in adjacent browser tabs and inspects the URL of the same conversation in both.

---

### H-002 — Hypothesis: native portal scrape ⊇ linked-accounts view for same conversation

- **Status:** `hypothesis`
- **Claim:** For any single conversation, the data available by scraping the originating portal natively is at least as rich as (probably strictly richer than) the data available from a linked-accounts view in a different portal. Specifically: full HTML bodies, attachments, attachment metadata, encounter linkage.
- **Evidence:** None yet. Inference from Epic's general architecture — Care Everywhere typically exchanges document summaries rather than full native objects.
- **Test plan:**
  - Pick 5 conversations known to be UCSF-native and visible in MSKCC's view.
  - Scrape them via both MSKCC's API and UCSF's API.
  - Compare field-by-field: body length, attachment list, encounter reference presence, participant list.
  - Tabulate gaps.
- **Implication if confirmed:** Even with strong aggregators, native scrape adds incremental value for institutions the patient cares most about. Mobile app should still support per-institution login for "high-value" providers.
- **Implication if disproven:** One strong-aggregator login is genuinely sufficient. Mobile app onboarding simplifies further.
- **Re-evaluation trigger:** Same prerequisite as H-001 (two simultaneous sessions).

---

### H-003 — Hypothesis: aggregator strength is portal-configurable, not patient-specific

- **Status:** `hypothesis`
- **Claim:** "Strong aggregator" status (broad linked-accounts view) is a property of how the Epic customer organization has configured Care Everywhere participation and MyChart linked-accounts UI, not a property of the individual patient. Therefore: if MSKCC is a strong aggregator for patient A, it will be a strong aggregator for patient B too (assuming patient B has accounts at overlapping institutions).
- **Evidence:** None yet — only one patient in the system.
- **Test plan:** Compare aggregator strength rankings across patients once N ≥ 3.
- **Implication if confirmed:** The mobile-app onboarding can carry a static "ranked-by-aggregator-strength" list of Epic customer instances and use it for any patient.
- **Implication if disproven:** Aggregator strength must be probed per-patient at connection time.
- **Re-evaluation trigger:** Patient N=3.

---

### H-004 — Hypothesis: Apple Health Records and MyChart scraping have non-subset overlap

- **Status:** `hypothesis`
- **Claim:** For the same institution, Apple Health Records ingestion and direct MyChart scraping return overlapping but neither-subsumes-the-other clinical data. AHR may have data that scraping misses (e.g., if AHR uses a different Epic API surface) and vice versa.
- **Evidence:** None directly tested. The HAPI Communication count includes nothing from AHR (zero AHR-tagged Communications), but DocumentReference counts suggest AHR contributes ~564 documents under tags we haven't fully mapped.
- **Test plan:** For one institution (UCSF), compare resource-by-resource what AHR delivers vs. what `scrape_ucsf_*.js` delivers. Use the comparison pipeline from revised Phase D.
- **Implication if confirmed:** Channel deduplication needs to happen across the channel boundary, not just within each channel. Belt-and-suspenders strategy is justified.
- **Implication if disproven:** One channel is dominant and the other is redundant — significant simplification.
- **Re-evaluation trigger:** When the comparison pipeline lands.

---

## Provenance contract (specification — implementation pending)

Operational rules that all scrapers and converters must follow. Violations should fail CI and create a new conclusion log entry documenting the regression.

1. **Every scraper output JSON record carries a `_provenance` block** containing at minimum: `scraped_at` (ISO8601), `source_portal` (e.g. "mskcc.mychart"), `source_org_id` (the Epic `organizationId` if known, else `null`), `source_url` (the specific endpoint hit), `scraper_version` (semver).
2. **Every converter preserves every scraper field** into the FHIR output, either as a typed FHIR element OR as `Resource.meta.tag` / `Resource.meta.extension` OR by entry in a `dropped-fields.yaml` registry with a rationale. No silent drops.
3. **Every FHIR resource produced from scrape data carries provenance tags** under documented system URIs: `urn:bina:source-portal`, `urn:bina:source-org`, `urn:bina:scraper-version`.
4. **A CI lint scans scraper outputs and converter outputs**, computes field coverage, and fails if any scraper field neither appears in FHIR output nor in `dropped-fields.yaml`.
5. **A "field dictionary" doc** maps every observed scraper-output field to its FHIR destination or its drop-rationale. Updated alongside the converter code.

C-002 is the test case: applying this contract retroactively must surface the `organizationId` loss.

---

## Two-key identifier discipline (specification — implementation pending)

1. Every scraped resource gets a **local identifier** that includes the portal of capture: `urn:{portal}:{resource-type}:{native-id}`. Always populated.
2. Every scraped resource gets a **canonical identifier** when a portable global ID is derivable: `urn:epic:global-{resource-type}:{global-id}`. Populated when known; empty placeholder otherwise.
3. **Dedup runs at converter-time on the canonical identifier.** Resources without a canonical ID are flagged for manual review or for hash-based fuzzy dedup, never silently inserted as duplicates.
4. The canonical-ID derivation strategy is documented per resource type, and the strategy itself is logged here as a hypothesis until empirically tested.

H-001 (WP-24 portability) is the prerequisite test for the Communication resource type. Similar tests will be needed for `Encounter` (Epic CSN portability), `MedicationRequest`, etc.
