# Product Insights Log

**Purpose:** Living record of product, positioning, UX, and monetization insights that emerge from technical work. Each entry captures the insight, what surfaced it, why it matters, the conditions under which it might not hold, and what would validate or disprove it. Append-only. Like `CONCLUSIONS_LOG.md`, but for claims about *users and markets* rather than claims about *EHR data*.

**Maintenance discipline:**
- Insight derived from a conclusion or hypothesis in `CONCLUSIONS_LOG.md` → link to it; revisit when that conclusion changes.
- New patient enrolled → revisit any insight whose `generalization_risk` depends on N=1 evidence.
- Insight begins to be tested in real onboarding/UX → move from `raw-idea` to `testing`.
- Quarterly: scan for stale `raw-idea` entries; either promote or drop.

**Status values:** `raw-idea` (captured, not examined) · `hypothesis` (testable claim about user behavior or market) · `testing` (under live validation) · `validated` (evidence supports it) · `disproven` · `superseded`

---

## Index

| ID | Status | Insight (short) | First captured |
|---|---|---|---|
| P-001 | raw-idea | "Log in once, see everything" as primary onboarding promise — lead with strong-aggregator portals before asking about specific institutions | 2026-05-29 |

---

## Entries

### P-001 — "Log in once, see everything" as primary onboarding promise

- **Status:** `raw-idea`
- **Insight:** Onboarding leads with "Do you have a MyChart account at any of [list of known strong aggregators]?" *before* asking the patient to enumerate their providers. The promise to the user is "log in once and see everything you'd otherwise have to chase across portals." Positions BinaHealth against both per-portal patient-facing apps and slow record-assembly services (PicnicHealth).
- **Origin:** Surfaced during the cross-institution attribution discussion that produced `CONCLUSIONS_LOG.md` entries **C-001** (Happy Together aggregation exists), **C-003** (MSKCC ≈ 57% cross-institution content), and **C-004** (Stanford ≈ 9%). Empirically, one well-chosen login can substitute for several.
- **Why it matters:**
  - **Onboarding completion rate.** Asking users to enumerate every provider they've ever seen is a known abandonment driver; "do you have one of these?" is concrete and answerable.
  - **Sellable promise.** "Log in once" is a positioning weapon — short, memorable, demonstrably true (for the right patient + portal combinations).
  - **Operational simplicity.** Fewer OAuth/WebView flows per user → less token-refresh maintenance, fewer breakage points, faster initial sync.
  - **Differentiation vs. PicnicHealth.** They take weeks to assemble; we'd be near-instant for the aggregator-covered portion of the record.
- **Dependencies — must be resolved before this can be tested in production:**
  - `CONCLUSIONS_LOG.md` **H-002** (native scrape ⊇ linked-accounts view): if native scrape is strictly richer, "log in once" overpromises and we need a "log in once for breadth, add native for depth at high-value institutions" nuanced flow.
  - `CONCLUSIONS_LOG.md` **H-003** (aggregator strength is portal-config not patient-specific): if false, we cannot pre-rank a static aggregator list; we'd have to probe per-patient at connection time, which makes the onboarding promise harder to make crisply.
  - Patient N≥3 to validate that aggregator rankings generalize.
- **Test plan:**
  - Once mobile app exists: build two onboarding variants. Variant A = current "list your providers" flow. Variant B = "do you have MyChart at [list]?" flow. Measure: (a) onboarding completion rate, (b) median time-to-first-useful-record, (c) self-reported perceived completeness at 30 days.
  - Before the app: synthetic test by asking 5–10 prospective users which framing feels easier and which evokes more trust in the product. Low-fidelity but informative.
- **Risks / when this might be wrong:**
  - **Wedge mismatch.** Strong aggregators may not align with the patient populations that most need consolidation. MSKCC is a strong aggregator (per C-003) but its patient base is ~150k cancer patients/year — narrow. If MSKCC turns out to be unusually broad-aggregating *because* its patients tend to be highly cross-institutional, that's a sample bias rather than a generalizable portal property. Need to identify strong aggregators among broadly-used institutions (large IDNs, Kaiser-equivalent regional networks).
  - **Patients may not know whether they have MyChart at a given institution.** Especially common with linked accounts they accepted years ago. The flow needs a "I'm not sure" branch.
  - **Aggregator strength may degrade.** Epic/customers can change linked-accounts policies. Today's strong aggregator may not be tomorrow's. The list cannot be static-forever.
  - **Cherry-picking risk in messaging.** Promising "log in once" and then needing the user to add three more accounts to fill gaps damages trust. Must be honest about scope upfront.
- **Adjacent product moves this unlocks:**
  - **"Institutions we noticed you haven't connected" suggestions** (refined 2026-05-31 via CONCLUSIONS_LOG C-010). Must distinguish **four** states of an organizationId, not two:
    1. *Connected, active* — recent messages within the expected cadence for the user's care pattern.
    2. *Connected, dormant* — linked but quiet. Could be intentional (no care happening) or could be early-warning of imminent lapse.
    3. *Disconnected, was previously linked* — Mayo Clinic case in audit data. The connection silently expired (per C-010). Historical data preserved in our vault; new data not flowing. Should prompt reconnection with the framing "you had Mayo connected; we still have your old records; reconnect to resume sync."
    4. *Never connected, but referenced* — Hoag case (in user's UI list with no message activity) or an institution mentioned in a referral letter but never linked. Should prompt with framing "we noticed references to [Institution] in your records; do you have a portal there?"
  - **Coverage transparency dashboard.** Show the user explicitly: "via your MSKCC login we have X% of your UCSF data. Connect UCSF directly to add the missing Y%." Turns the H-002 native-vs-linked-accounts gap into a feature rather than a hidden limitation.
  - **Silent-disconnect detection** (new — from C-010). Heuristic monitoring per organizationId: last-seen-message timestamp; drop-off in expected message cadence relative to the patient's care pattern; flag for reconnection prompt. MyChart itself doesn't proactively notify users when linked accounts lapse, which makes this a differentiating capability for BinaHealth, not a parity feature.
  - **Apple Health Records as a coverage manifest** (new — from C-018 + P-CHANNEL-SCOPE-DISCIPLINE). On first onboarding, asking the user to authorise AHR is cheap (one-tap, no per-portal authorisation). AHR's clinical-record DocRefs then drive two things: (1) the "institutions you may have forgotten" suggestion list (state #4 in the four-state model — "never connected but referenced"); (2) a continuous QA layer against direct portal scrapes — if AHR knows about 165 Stanford notes and our Stanford scraper captured only 140, the missing 25 are visible to both us and the user. AHR is not a content source — it's a manifest that improves discovery and surfaces drift.
- **Re-evaluation trigger:**
  - H-002 resolved.
  - H-003 resolved.
  - Patient N=3 enrolled (aggregator rankings comparable across patients).
  - First user research session on real onboarding flows.
