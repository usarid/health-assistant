# Personal Health Vault — Data Quality Checklist

Last updated: 2026-04-04

## Summary of Completed Fixes

| Pass | What | Count | Script |
|------|------|-------|--------|
| 1 | Interpretations (H/L/N) from value vs reference range | 1,910 | `apply_quality_patches.py` |
| 1 | Negative interpretations for "Not Detected" results | 114 | `apply_quality_patches.py` |
| 1 | Numeric strings (`<10`, `<0.1`) → valueQuantity with comparator | 78 | `apply_quality_patches.py` |
| 1 | Narrative extraction (TB, monoclonal protein) | 3 | `apply_quality_patches.py` |
| 2 | HTML stripped from pathology report values | 55 | `fix_remaining_quality.py` |
| 2 | Plain numeric strings (pH, Specific Gravity, ratios) → valueQuantity | 66 | `fix_remaining_quality.py` |
| 2 | SEE COMMENTS resolved (cryoglobulin, cultures, generic) | 6 | `fix_remaining_quality.py` |
| 2 | CBC differential reference ranges + interpretations added | 411 | `fix_remaining_quality.py` |
| 3 | Same-source exact duplicate observations deleted | 1,462 | `deduplicate_observations.py` |
| **Total** | | **4,105** | |

---

## Known Issues — Scraper Fixes Needed

These should be fixed in the scraping/ingestion code to prevent recurrence on future data loads.

### S1. Stanford MyHealth scraper creates duplicate observations
- **Severity:** High
- **Scope:** ~1,400 duplicate clusters found (1,427 extra observations)
- **Root cause:** Likely overlapping date ranges in scraper runs, or same observation appearing in multiple DiagnosticReports and being created separately for each
- **Fix:** Deduplicate by (LOINC code, date, value) before inserting into HAPI. Use deterministic IDs based on content hash (like `make_id` in `fhir_utils.py`) so re-running the scraper is idempotent
- **Status:** Retroactively fixed via `deduplicate_observations.py`; scraper not yet patched

### S2. SEE COMMENTS / SEE NOTE values not resolved during scraping
- **Severity:** Medium
- **Scope:** ~14 observations across Stanford and MSK-CC
- **Root cause:** Scraper captures the value field ("SEE COMMENTS") but doesn't parse the comment/note text that contains the actual result
- **Examples:** Cryoglobulin at MSK-CC shows "SEE COMMENTS" but the comment text says "Negative." QMPTS Interpretation says "SEE COMMENTS" but the actual interpretation is in a nearby text field
- **Fix:** Scraper should extract the comment/note text alongside the value. When value is "SEE COMMENTS" or "SEE NOTE", look for the actual result in adjacent fields
- **Status:** Partially fixed retroactively (cryoglobulin, cultures); QMPTS and some others remain

### S3. HTML markup stored in observation values
- **Severity:** Medium
- **Scope:** 55 observations (pathology reports from Stanford)
- **Root cause:** Scraper stores raw HTML from pathology report fields (Diagnosis, Gross Description, Microscopic Description, etc.) directly into valueString without stripping tags
- **Fix:** Run `strip_html()` on all valueString content during scraping
- **Status:** Retroactively fixed via `fix_remaining_quality.py`

### S4. Numeric values stored as strings
- **Severity:** Medium
- **Scope:** ~144 observations across all sources
- **Subcategories:**
  - Comparator values: `<10`, `>60`, `<0.12` → should be valueQuantity with comparator
  - Plain numbers: `"0.5"`, `"1.007"`, `"5.5"` → should be valueQuantity
- **Root cause:** Scraper doesn't distinguish between numeric and text values; stores everything as valueString
- **Fix:** During scraping, attempt to parse valueString as a number. If it parses (with optional `<`/`>` prefix), store as valueQuantity instead
- **Status:** Retroactively fixed via passes 1 and 2

### S5. Missing interpretations despite having value + reference range
- **Severity:** Low-Medium
- **Scope:** ~1,950 observations (mostly Stanford)
- **Root cause:** Source portals often don't include interpretation flags in their FHIR/API output, even when the value is clearly outside the reference range
- **Fix:** Compute interpretation (H/L/N) during ingestion whenever value and reference range are both present
- **Status:** Retroactively fixed via `apply_quality_patches.py`

### S6. Missing reference ranges for well-known tests
- **Severity:** Low
- **Scope:** ~675 tagged clinical observations
- **Root cause:** Some tests (CBC differential percentages, pH, Specific Gravity) don't include reference ranges in the portal data
- **Fix:** Maintain a lookup table of well-known reference ranges and apply during ingestion when missing
- **Status:** Partially fixed (CBC differentials done); many metadata fields (Specimen, Fasting, Reviewed By) legitimately don't have ranges

### S7. Missing LOINC codes on portal-scraped observations
- **Severity:** High
- **Scope:** Majority of portal-scraped observations (~95%+ from Stanford, MSK-CC, UCSF, Sutter)
- **Root cause:** Epic MyChart web UI displays test results by display name but does not expose LOINC codes in the patient-facing HTML. Only C-CDA exports and FHIR API responses reliably include them.
- **Example:** 20 Ferritin observations in HAPI; only 3 (from C-CDA/Apple Health) have LOINC codes. The other 17 from portal scrapers have no coding at all.
- **Impact:** Timeline only found 1 of 20 Ferritin results; search by LOINC code misses most data. Affects every metric, not just Ferritin.
- **Fix (short-term):** Timeline now also searches by `code:text` display name as fallback. LOINC codes are backfilled retroactively using `loinc_mapper.py` (display-name-to-LOINC lookup with ~220 mappings, 94%+ coverage).
- **Fix (long-term):** Epic FHIR API access will include proper LOINC codes.
- **Status:** UI workaround applied; retroactive backfill via `loinc_mapper.py` ready to run. Scraper fix pending.

### S8. LOINC mapper must be updated when new test names appear
- **Severity:** Medium (ongoing)
- **Scope:** Every ingestion run may introduce new display-name variants not yet in the mapper
- **Root cause:** Different portals and labs use different display names for the same test (e.g., "Alk P'tase, Total" vs "Alkaline Phosphatase" vs "ALP")
- **Principle:** When ingesting data, always run `loinc_mapper.py --stats` afterward to check for new unmatched names. Add mappings to `LOINC_MAP` for any real lab tests, then re-run. Metadata fields (Interpretation, Impression, Reviewed By, etc.) should be left unmapped.
- **Status:** Ongoing maintenance task

---

## Known Issues — Data Reconciliation

### R1. Cross-source duplicate observations
- **Severity:** Medium
- **Scope:** ~90 clusters where the same test appears from multiple sources
- **Patterns identified:**
  - **MSK-CC ↔ Stanford (15 clusters):** Same blood draw at MSK flows to Stanford via Care Everywhere with 2-day date lag and slightly rounded values (e.g., RBC 5.56 vs 5.65). Some values differ enough to possibly be separate draws (Ferritin 31 vs 53.7)
  - **Sutter ↔ UCSF (19 clusters):** Tests shared between institutions via Care Everywhere
  - **Historical PDFs ↔ untagged (74 clusters):** Overlap between manually loaded data and Apple Health or other imports
- **Challenge:** Can't simply pick one and delete — need to determine which source is "authoritative" (closest to where the test was performed), and values may differ due to rounding, unit conversion, or genuinely different draws
- **Fix:** Build cross-source dedup with conservative matching: require same LOINC, ≤3 day window, AND value agreement within tolerance. Keep the source closest to the performing lab
- **Status:** Not yet addressed

### R2. C-CDA imports have shifted dates
- **Severity:** Low
- **Scope:** Affects cross-source matching
- **Root cause:** C-CDA documents record dates differently (sometimes result date vs collection date vs reporting date), leading to 1-3 day discrepancies for the same test
- **Example:** UCSF Cryoglobulin collected Nov 13, 2023 appears as Nov 21, 2023 in C-CDA import
- **Fix:** Use wider date windows for cross-source matching; prefer portal-scraped dates over C-CDA dates

### R3. Spurious full-text search matches
- **Severity:** Low (UI issue, not data issue)
- **Scope:** Affects search results in Personal Health Vault web UI
- **Example:** Searching "cryoglobulin" returns a "Diagnosis Comments" pathology report from 2024-03-25 that mentions the word but isn't a cryoglobulin test
- **Fix:** UI could distinguish between "code/name match" vs "text-only match" and rank accordingly

---

## Known Issues — Data Completeness

### C1. Observations with no extractable value
- **Severity:** Low
- **Scope:** ~32 tagged observations
- **Categories:**
  - **Fasting status (16):** Could be set to "Yes"/"No" from narrative text — partially addressable
  - **Interpretation fields (3):** Celiac, protein electrophoresis — actual result not in FHIR narrative
  - **COMMENT fields (3):** Lab comments, not actual test results — may not need values
  - **Culture results (3):** Actual culture results not captured in narrative
  - **Others:** Calprotectin interpretation, K/L Ratio, Corrected Calcium, MPV — values exist in source portal but weren't captured by scraper
- **Fix:** Improve scraper to capture all observation values, not just primary ones

### C2. Sutter comparator values
- **Severity:** Low
- **Scope:** 14 observations (eGFR ">60", H.pylori "<0.40", CRP "<2.9", etc.)
- **Confirmed via portal:** These are legitimate values (not reference ranges mistakenly stored as values)
- **Fix:** Convert to proper FHIR valueQuantity with comparator field
- **Status:** Fixed via `fix_sutter_comparators.py`

### C3. LOINC code assignment (data cleansing)
- **Severity:** High
- **Scope:** ~3,900+ observations lacking LOINC codes across all portal-scraped sources
- **Root cause:** Portal scrapers don't expose LOINC codes (see S7)
- **Fix:** `loinc_mapper.py` maintains a display-name-to-LOINC lookup table (~220 mappings). Run retroactively on all existing data, and as part of every ingestion pipeline via `assign_loinc(obs)`.
- **Principles:**
  - LOINC mapping is a **data cleansing** step, not just an ingestion step
  - After each ingestion, run `--stats` to identify new unmatched display names
  - Add new mappings to `LOINC_MAP` for real lab tests; leave metadata fields unmapped
  - The mapper may need updates even without new data sources (e.g., when a lab changes its display name conventions)
  - After each mapper run, run `validate_loinc.py` to check for misassigned codes (wrong specimen type, implausible unit/LOINC pairings, etc.)
  - Avoid mapping ambiguous bare names (e.g., "color", "appearance") — require qualified forms ("urine color", "color, urine") to prevent silent misassignment
- **Status:** Mapper built with 94%+ coverage; pending first full run

---

## Future: Data Maintenance & Ingestion

### M1. New institutional data (Epic portal results)
- **Short-term:** Screenshot new results in portal → AI ingestion
- **Long-term:** Epic FHIR API access (pending approval) for automated pull
- **Key concerns:**
  - Must apply all scraper fixes (S1-S7) to new ingestion pipeline
  - Must run `loinc_mapper.py --stats` after each ingestion to catch new display-name variants (S8)
  - LOINC mapper may need new entries added before backfill run

### M2. Apple Health integration
- **Current state:** Bulk export loaded, but no incremental update mechanism
- **Plan:** Build iOS app that pulls from HealthKit and pushes to FHIR server
- **Open question:** Are all Watch data types included (sleep stages, HRV, respiratory rate)?
- **Open question:** Does the current Apple Health export include sleep trend data from the Watch?

### M3. Conneqt blood pressure monitor
- **Current state:** Sends systolic/diastolic to Apple Health, but has richer data in its own app (trends, pulse wave, etc.)
- **Short-term:** Monthly screenshots of Conneqt app trends → AI ingestion
- **Long-term:** Check for Conneqt API or export capability

### M4. Oura Ring
- **Open question:** Does Oura send all its data to Apple Health, or only a subset?
- **Data types to verify:** Sleep stages, readiness score, temperature deviation, SpO2, HRV
- **If incomplete:** Build Oura API integration (they have a documented REST API)

---

## Scraper Code Locations

| Source | Scraper | Status |
|--------|---------|--------|
| Stanford MyHealth | TBD | Needs S1-S6 fixes |
| MSK-CC MyChart | TBD | Needs S2, S4, S5 fixes |
| UCSF MyChart | TBD | Needs S4, S5 fixes |
| Sutter MyChart | TBD | Needs C2 fix |
| Apple Health | TBD | Bulk export only |
| Historical PDFs | `create_historical_fhir.py` | Complete |
| Supplemental PDFs | Various | Complete |
