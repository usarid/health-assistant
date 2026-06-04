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
| H-001 | hypothesis | The Epic `WP-24…` thread token is portable: identical across portals for the same underlying conversation | 2026-05-31 |
| H-002 | hypothesis | Native portal scrape yields strictly more information per conversation than the same conversation viewed via a linked-accounts aggregator | 2026-05-29 |
| H-003 | hypothesis | "Strong aggregator" status is an Epic customer configuration property, not patient-specific; the same portal aggregates the same way for any patient | 2026-05-29 |
| H-004 | hypothesis | Apple Health Records and MyChart scraping return overlapping but not subset/superset clinical-note coverage for the same institution | 2026-05-29 |
| H-005 | hypothesis | Doctor's notes can be de-duplicated against structured data via entity linking + temporal matching, with quoted dumps replaced by typed references to canonical resources (carries P-DEDUP-CARRY-PROVENANCE) | 2026-06-04 |

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

- **Status:** `hypothesis` (2026-06-04). Carries principle P-DEDUP-CARRY-PROVENANCE.
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
