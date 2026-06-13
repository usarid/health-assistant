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
| P-002 | raw-idea | Productizing local credential storage (saved-login autofill) needs lawyer review against Apple App Store guideline 5.1.5 + each portal's ToS BEFORE multi-tenant ship | 2026-06-13 |

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

---

### P-002 — Productizing saved-login autofill needs lawyer review

- **Status:** `raw-idea`
- **Insight:** The mobile app captures portal credentials (with explicit user consent) into iOS Keychain and autofills them on subsequent launches. For single-user-on-own-device this is the same pattern Safari and every password manager already uses — no novel risk. **But for productized multi-tenant shipping, two distinct legal reviews are gating:**
  1. **Apple App Store Review Guideline 5.1.5** (and the broader 5.1 family on data use). Section 5.1.5 specifically: "Apps must obtain user consent before collecting location data." More relevantly for us, 5.1.1(i-v) around data minimization, consent, and "data collected from people in healthcare-related apps must be handled carefully and with full user knowledge." Stanford/UCSF/etc. credentials are arguably "data collected from people" — Apple may interpret storing them as triggering additional review hurdles even though they never leave the device.
  2. **Each portal's Terms of Service.** Stanford MyHealth, UCSF MyChart, etc. all have ToS the patient agreed to. Some Epic-derived portal ToS explicitly prohibit automated access or credential sharing with third-party apps. "Storing the user's password on the user's own phone for re-use" probably skates close to such language even if it isn't sharing. Worth a defensible interpretation before shipping.
- **Origin:** Surfaced 2026-06-13 during mobile iteration 2 work, when implementing credential autofill via Keychain. The single-user version is fine; the productized version needs review.
- **Why it matters:**
  - **App Store rejection risk** — getting bounced on submission is a multi-week delay if 5.1.5/5.1.1 concerns surface late. Better to know the standard up front.
  - **Portal hostility risk** — Stanford specifically demonstrated active anti-automation behavior (C-021, C-022). If the portal's lawyers interpret "stored credentials + automated form fill" as a ToS violation, they could legally pressure Apple to pull the app, or sue. Unlikely-but-real for a small shop.
  - **Patient trust** — the value prop is "you control your records." That promise is undermined if a portal's lawsuit or App Store pull yanks the user's access mid-use.
- **What would resolve this:**
  - Healthcare-app attorney review of: (a) App Store guideline current interpretation for credential storage in patient-facing health apps; (b) representative portal ToS language re: automated access and credential storage; (c) what consent language and disclosures we need at credential-save time.
  - Possibly Apple TestFlight + Review precedent search — find similar apps (PicnicHealth, Lucy, Apple Health Records itself indirectly) and see what they do.
  - A documented threat model: what's the actual harm if credentials leak from a compromised user device, and is our Keychain handling sufficient mitigation? (Probably yes — same as every password manager — but document it.)
- **Until reviewed — interim posture for any product that ships:**
  - **Default to no credential storage.** Make autofill opt-in with a clear explainer screen, not the default flow.
  - **Per-portal opt-in.** A user enabling autofill for Stanford shouldn't imply consent for UCSF.
  - **Clear "Forget login" everywhere visible.** Not buried in settings.
  - **Never sync credentials off-device.** No iCloud Keychain sync of OUR stored creds, no backend storage, no cross-device sharing. This is one of the easier defenses against "you shared my password" claims.
  - **Don't transmit credentials anywhere except the portal itself, via the WebView the user can see.** Specifically: NOT to BinaHealth's backend, NOT to logs.
- **Re-evaluation trigger:**
  - Before any productized multi-tenant ship.
  - If/when a portal updates ToS in a way that could affect this.
  - If Apple updates 5.1.5 or guidance on health-credential storage.
