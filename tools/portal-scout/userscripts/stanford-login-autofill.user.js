// ==UserScript==
// @name         Stanford MyHealth: autofill login (Bina Health)
// @namespace    https://github.com/usarid/BinaHealth
// @version      0.1.0
// @description  Mirrors the mobile scraper's iOS-Keychain autofill pattern. Stores
//               your Stanford credentials in Tampermonkey's per-script local storage,
//               then autofills + submits the sign-in form whenever you land on a
//               Stanford MyHealth signed-out page. MFA is still done by you.
//               Credentials never leave the browser; the AI is not in the loop.
// @match        https://myhealth.stanfordhealthcare.org/*
// @match        https://mychart.stanfordhealthcare.org/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const KEY_USER = 'stanford.username';
  const KEY_PASS = 'stanford.password';
  // One autofill attempt per tab-session; prevents loops if the page reloads
  // with an error after submit.
  const SESSION_TRIED_FLAG = 'binahealth.autofill-tried';

  // ── Menu commands ────────────────────────────────────────────────────
  GM_registerMenuCommand('Set Stanford credentials', () => {
    const u = prompt('Stanford username (stored locally in Tampermonkey only):',
                     GM_getValue(KEY_USER, ''));
    if (u == null) return;
    const p = prompt('Stanford password (stored locally in Tampermonkey only):');
    if (p == null) return;
    GM_setValue(KEY_USER, u);
    GM_setValue(KEY_PASS, p);
    alert('Stanford credentials saved.');
  });

  GM_registerMenuCommand('Clear Stanford credentials', () => {
    GM_deleteValue(KEY_USER);
    GM_deleteValue(KEY_PASS);
    sessionStorage.removeItem(SESSION_TRIED_FLAG);
    alert('Stanford credentials cleared.');
  });

  GM_registerMenuCommand('Autofill now', () => {
    sessionStorage.removeItem(SESSION_TRIED_FLAG);
    tryAutofill();
  });

  // ── Form detection ──────────────────────────────────────────────────
  // Generic across most Epic MyChart-style portals: a non-disabled password
  // input, plus its sibling username field (text/email/autocomplete).
  function findLoginForm() {
    const passEl = document.querySelector('input[type="password"]:not([disabled])');
    if (!passEl) return null;
    const scope = passEl.form || document;
    const userEl = scope.querySelector(
      'input[autocomplete~="username"]:not([disabled]),' +
      'input[type="email"]:not([disabled]),' +
      'input[type="text"]:not([disabled])'
    );
    return userEl ? { userEl, passEl, form: passEl.form } : null;
  }

  // React-friendly value setter — bypasses the synthetic event system so the
  // framework actually picks up the change.
  function setValue(el, val) {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function tryAutofill() {
    if (sessionStorage.getItem(SESSION_TRIED_FLAG)) return;
    const user = GM_getValue(KEY_USER, '');
    const pass = GM_getValue(KEY_PASS, '');
    if (!user || !pass) return;
    const f = findLoginForm();
    if (!f) return;
    sessionStorage.setItem(SESSION_TRIED_FLAG, '1');
    setValue(f.userEl, user);
    setValue(f.passEl, pass);
    // Brief delay so the page's own validators see the new values.
    setTimeout(() => {
      if (f.form && typeof f.form.requestSubmit === 'function') f.form.requestSubmit();
      else if (f.form) f.form.submit();
    }, 150);
    obs.disconnect();
  }

  // SPAs render the form asynchronously — watch for it for up to 10s.
  const obs = new MutationObserver(tryAutofill);
  obs.observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(() => obs.disconnect(), 10_000);
  tryAutofill();
})();
