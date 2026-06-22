# Portal-scout userscripts

Two scripts that together let us drive a logged-in patient portal through the
Chrome MCP and reverse-engineer its data APIs, **without** any AI entering
credentials or observing PHI in transit.

1. **`stanford-login-autofill.user.js`** — browser-side analog of the mobile
   scraper's iOS-Keychain autofill. Stores Stanford credentials in
   Tampermonkey's per-script local storage and fills/submits the sign-in form
   automatically.
2. **`api-capture.user.js`** — recon helper that intercepts every fetch / XHR
   on a portal page and records URL, method, headers, request body, response
   body, plus every sessionStorage write. Captures download as a JSON file to
   your Downloads folder for offline analysis.

## Trust posture

- Storage backend: Tampermonkey's `GM_setValue` — Chrome IndexedDB, accessible
  only to the userscript itself. Not visible to the page, not visible to other
  scripts.
- On-disk encryption: depends on Chrome's profile-level encryption (OS keychain
  on macOS via `kCFBundleIdentifierKey`). Same trust posture as Chrome's
  built-in password manager and similar to iOS Keychain — local-only, never
  shipped over the network.
- **The AI is not in the loop at credential-handling time.** The userscript
  runs in the browser; the password is never observed by Claude or by any tool
  call's input/output.

If you want stricter handling, skip the userscript and let Chrome's native
password manager autofill manually — the rest of the recon workflow still
works, you'll just click "fill from saved" once per session.

## Install (one-time)

1. Install Tampermonkey for Chrome:
   <https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo>
2. In this repo, open
   `tools/portal-scout/userscripts/stanford-login-autofill.user.js` in Chrome
   (drag-drop into a tab works) — Tampermonkey detects the `@UserScript` header
   and offers to install. Click "Install".
3. Visit any page on `myhealth.stanfordhealthcare.org`. Click the Tampermonkey
   icon → "Set Stanford credentials" → enter username + password.

That's it. The next time you land on a Stanford signed-out page, the form is
filled and submitted. You still complete MFA yourself.

## Use

- **Sign in**: just navigate to <https://myhealth.stanfordhealthcare.org/>. The
  script detects the login form, fills it, submits.
- **Re-trigger**: Tampermonkey icon → "Autofill now" (clears the once-per-tab
  guard and re-runs).
- **Wipe credentials**: Tampermonkey icon → "Clear Stanford credentials".

## API capture — use

1. Install `api-capture.user.js` the same way (Tampermonkey Dashboard → `+` →
   paste → Cmd+S).
2. Navigate to the portal page where you want to start observing (e.g. the
   labs list).
3. Tampermonkey icon → **"▶ Start API capture"**.
4. Drive the UI as you normally would (click into a lab, scroll, paginate).
   The script intercepts every fetch / XHR on Stanford/MyChart domains and
   records full request + response bodies into Tampermonkey storage.
5. Tampermonkey icon → **"■ Stop API capture"**.
6. Tampermonkey icon → **"⤓ Download captures"** — a JSON file lands in your
   Downloads folder (named `portal-captures-<ts>.json`). Move it under
   `tools/portal-scout/captures/<portal>/<flow>/` for offline analysis; the
   directory is gitignored.

The script is portal-agnostic; the `@match` lines decide where it runs. Add
more `@match` directives to point it at additional portals (UCSF, MSKCC, etc.).

## Adding another portal

Each portal gets its own `<portal>-login-autofill.user.js`. Copy
`stanford-login-autofill.user.js`, update `@match`, the `KEY_USER`/`KEY_PASS`
namespace prefix, and the menu labels. The form-detection heuristic
(`input[type="password"]` + adjacent username field) covers most Epic
MyChart-style portals without modification.

The API capture script is shared — extend its `@match` list rather than
forking it.
