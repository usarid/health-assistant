# mobile-staged

Day-one Flutter prototype for the BinaHealth mobile app. The point of this
directory is to ship the Dart code + setup script that the eventual `mobile/`
Flutter project will use, while we wait for Xcode to finish downloading on the
dev machine.

## Goal of day-one

Prove the host-WebView architecture by scraping ONE Stanford clinical note
end-to-end:

1. App launches → WebView loads Stanford MyHealth login.
2. User logs in inside the WebView (handles MFA, full credential control).
3. URL settles on `/signedin/...` → app shows a "Scrape this visit" button.
4. Tap it → app drives a top-level `controller.loadUrl(after-visit-summary URL)`
   for a hardcoded test CSN. Top-level nav gives `Sec-Fetch-Dest: document`,
   which is the one thing page-JS scraping can't fake (proven 2026-06-08 against
   Stanford).
5. `onLoadStop` fires → app injects scraper JS that clicks the Clinical Notes
   tab, polls for `.pgSection`, captures `outerHTML`, calls back to Dart via
   `window.flutter_inappwebview.callHandler('saveNote', { csn, html })`.
6. Dart's `saveNote` handler writes `{csn, html, savedAt}` to the app's
   documents directory.

Success criterion: one JSON file on disk with a 100KB+ clinical-note HTML
body. That validates everything the full product needs at the WebView layer:
top-level nav, lifecycle-injected userscripts, JS↔Dart bridge, local IO.

## What this is NOT yet

- No loop over multiple visits (iteration 2)
- No portal-config loading (iteration 2)
- No keepalive (iteration 2 — already designed; `tools/v3/configs/stanford.json`
  has the URLs)
- No BinaHealth backend POST (iteration 3)
- No multi-portal support (iteration 3)
- No nice UI (later)

## How to run (after Xcode is installed)

```bash
cd /Users/urisarid/Public/BinaHealth/mobile-staged
./setup.sh         # creates ../mobile/, copies these files in, opens sim, runs
```

The setup script handles Xcode first-launch / license acceptance, then
`flutter create mobile && flutter pub get && flutter run -d "iPhone 15"`.

## Files

```
mobile-staged/
  README.md                            ← this file
  setup.sh                             ← one-shot bring-up script
  pubspec.yaml                         ← Flutter project deps
  lib/
    main.dart                          ← app entry
    screens/
      scrape_screen.dart               ← the WebView + scrape orchestration
    scrape/
      scrape_jobs.dart                 ← injected JS (string constants)
    storage/
      local_writer.dart                ← file IO for captured notes
```

## Architectural reference

See `docs/CONCLUSIONS_LOG.md` C-021 for the Stanford clinical-note recipe
(endpoints, body shapes, Sec-Fetch-Dest gating). The mobile app implements the
"native host owns the WebView" pattern that today's findings made necessary.
