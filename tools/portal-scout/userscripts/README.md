# Portal login autofill — userscripts

Browser-side analog of the mobile scraper's iOS-Keychain autofill. Lets us drive
Stanford (and future portals) via the Chrome MCP without an AI ever entering
credentials into a form. Credentials live in Tampermonkey's per-script local
storage, isolated from the page DOM and never transmitted.

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

## Adding another portal

Each portal gets its own `<portal>-login-autofill.user.js`. Copy this script,
update `@match`, the `KEY_USER`/`KEY_PASS` namespace prefix, and the menu
labels. The form-detection heuristic (`input[type="password"]` + adjacent
username field) covers most Epic MyChart-style portals without modification.
