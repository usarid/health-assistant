# v3 scraper runbook

How to: verify v3 works against UCSF (a known-good baseline), then discover and
run it against Stanford, then compare both outputs against what we already have.

## Step 1 — verify v3 against UCSF

This catches any runtime bugs before we touch a new portal.

1. Open https://mychart.ucsf.edu in a browser, log in, navigate to the Visits page.
2. Open DevTools → Console.
3. Paste the entire contents of `tools/v3/runtime/scrape_runtime.js`. Hit return.
4. Paste this:

   ```js
   const cfg = await (await fetch('/UCSFMyChart/__not_a_real_path__')).json().catch(() => null) || {};
   ```

   …no, that won't work. Instead paste the contents of `tools/v3/configs/ucsf.json`
   into a variable. The quickest way:

   ```js
   // Paste the CONTENTS of tools/v3/configs/ucsf.json between the parens.
   const cfg = JSON.parse(`PASTE_JSON_HERE`);
   ```

   Or for nicer ergonomics, paste this and follow the prompt:

   ```js
   const ucsfJson = prompt('Paste ucsf.json contents:');
   const cfg = JSON.parse(ucsfJson);
   ```

5. Run:

   ```js
   const runner = new ScrapeRuntime(cfg);
   const results = await runner.run(['visits', 'notes']);
   console.log('visits:', results.visits.length, 'notes:', results.notes.length);
   // Copy to clipboard:
   copy(JSON.stringify(results));
   ```

6. Save the clipboard contents to `~/ucsf-v3-output.json` (any path is fine —
   you'll pass it to the comparison tool below).

**Expected output:** roughly 139 visits and 108 notes (matches the prior
`ucsf_visits_full.json` and `ucsf_notes_full.json` counts).

**Validation:**

```bash
python3 tools/v3/compare_to_baseline.py ucsf-visits ~/ucsf-v3-output.json
python3 tools/v3/compare_to_baseline.py ucsf-notes  ~/ucsf-v3-output.json
```

Expected: high CSN intersection, very few v3-only or baseline-only orphans.

## Step 2 — discover Stanford's portal-specific values

We need: Stanford's Epic SPA instance number for visits, the endpoint paths
for visit details and note content, and the request body field names.

1. Open https://myhealth.stanfordhealthcare.org (or whichever URL gets you to
   Stanford MyHealth) in a browser, log in, navigate to the Visits page.
2. Open DevTools → Console.
3. Paste the entire contents of `tools/v3/runtime/discover_portal.js`.
4. In the same browser tab, click around: open Visits (so the visits list
   loads), then click into a past visit, then expand a clinical note. This
   makes the relevant API calls fire so the helper captures them.
5. Back in the console, run:

   ```js
   discoverPortal();
   ```

6. Copy the printed JSON. Paste it to the assistant in the next message. The
   assistant fills in `tools/v3/configs/stanford.json` from your capture.

## Step 3 — run v3 against Stanford

Once `tools/v3/configs/stanford.json` is filled in:

1. In the Stanford MyHealth tab, console:

   ```js
   // (Re-paste tools/v3/runtime/scrape_runtime.js if you've reloaded)
   const cfg = JSON.parse(`PASTE_STANFORD_JSON_HERE`);
   const runner = new ScrapeRuntime(cfg);
   const results = await runner.run(['visits', 'notes']);
   copy(JSON.stringify(results));
   ```

2. Save to `~/stanford-v3-output.json`.

## Step 4 — compare

```bash
# Stanford visits sanity (vs the existing raw export)
python3 tools/v3/compare_to_baseline.py stanford-visits ~/stanford-v3-output.json

# Stanford notes coverage check against AHR's manifest (per C-018)
python3 tools/v3/compare_to_baseline.py stanford-notes ~/stanford-v3-output.json
```

The Stanford notes comparison is the interesting one — there's no prior
baseline for Stanford notes (Stanford note content was never scraped per C-017),
so the comparison is the AHR coverage manifest. Each AHR fingerprint not
captured by the v3 scrape is a scraping gap to triage.

## Notes on safety

- All scraping runs in your browser tab with your own credentials. No data
  leaves the tab except the JSON you explicitly `copy()` and paste.
- The discovery helper hooks `fetch()` to record API calls. It does not
  modify any request bodies or change what the portal does — it observes.
  When you're done, just reload the tab to remove the hook.
- The v3 runtime never sends results anywhere; it returns the data and your
  call (the `copy()` + save) decides where it goes.
