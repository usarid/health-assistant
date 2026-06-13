import 'stanford_config.dart';

/// JS scripts the host injects into the WebView per scrape job.
class ScrapeJobs {
  /// Per-visit Stanford scrape. Runs on a
  /// /signedin/appointments/after-visit-summary/csn=X&encType=3 page.
  ///
  /// Two things happen:
  ///   1. Fire-and-forget keepalive pings — every visit's scrape
  ///      contributes to keeping both Stanford sessions warm. At ~10s per
  ///      visit this is plenty of cadence.
  ///   2. Click the Clinical Notes tab, poll for the .pgSection container,
  ///      extract its outerHTML, call back to Dart via the saveNote handler.
  ///
  /// Calls window.flutter_inappwebview.callHandler('saveNote', { csn, html, error? }).
  static String stanfordSingleNote({int pollMs = 15000}) {
    return '''
(async () => {
  function send(payload) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler('saveNote', payload);
    } else {
      console.warn('[scraper] no flutter_inappwebview handler');
    }
  }

  // (1) Keepalive intentionally DISABLED for this experiment (2026-06-13).
  //     Working hypothesis: Stanford's anti-abuse flags injected keepalive
  //     fetches differently than its own-page-code ones, causing session
  //     revocation after a single rapid visit. If the batch completes
  //     without keepalive, we know keepalive was the trigger. (The
  //     Dart-side Timer is also short-circuited in scrape_screen.dart for
  //     this run.)

  // (2) Scrape the Clinical Notes tab
  const m = location.href.match(/csn=([^&]+)/);
  const csn = m ? decodeURIComponent(m[1]) : 'unknown';

  const candidates = Array.from(document.querySelectorAll('li, a, button'))
    .filter(el => /Clinical Notes/i.test(el.textContent || ''));
  const tab = candidates.find(el => (el.textContent || '').trim() === 'Clinical Notes')
    || candidates[0];

  if (!tab) {
    send({ csn, html: '', error: 'no-notes-tab' });
    return;
  }

  tab.click();

  // Stanford renders the body inside a .pgSection div. Real content is
  // ~9-200 KB; skeleton-only is ~750 chars. Threshold of 200 chars catches
  // any successful render. Poll-window upper bound configurable by caller.
  const startedAt = Date.now();
  while (Date.now() - startedAt < $pollMs) {
    await new Promise(r => setTimeout(r, 400));
    const section = document.querySelector('.pgSection');
    if (section && section.textContent.length > 200) {
      send({ csn, html: section.outerHTML });
      return;
    }
  }

  send({ csn, html: '', error: 'timeout-waiting-for-pgSection' });
})();
''';
  }

  /// Standalone keepalive — fired by the Dart-side Timer.periodic every 30s
  /// as a safety net if a per-visit scrape hangs and stops contributing to
  /// the per-visit keepalive in stanfordSingleNote().
  static String keepalive() {
    final urls = StanfordConfig.keepaliveUrls.map((u) => "'$u'").join(', ');
    return '''
for (const u of [$urls]) {
  try { fetch(u, { method: 'GET', credentials: 'include', mode: 'cors' }); }
  catch (_) {}
}
true;
''';
  }

  /// Injected on every load that lands on a Stanford login page. Does TWO
  /// things on a tap of the "Sign In" button:
  ///   1. If there are stored creds (autofillEmail/autofillPassword passed
  ///      in), set them into the form fields BEFORE the user sees the page.
  ///      They tap Sign In themselves.
  ///   2. When the Sign In button is tapped (whether autofilled or
  ///      hand-typed), capture the field values and call back to Dart via
  ///      the 'capturedCredentials' handler. Dart decides whether to ask
  ///      the user to save them.
  ///
  /// Idempotent — re-injecting on a page that already wired up is a no-op.
  static String loginAutofillAndCapture({
    String? autofillEmail,
    String? autofillPassword,
  }) {
    final emailJs = autofillEmail == null
        ? 'null'
        : "'${autofillEmail.replaceAll(r"\", r"\\").replaceAll("'", r"\'")}'";
    final passwordJs = autofillPassword == null
        ? 'null'
        : "'${autofillPassword.replaceAll(r"\", r"\\").replaceAll("'", r"\'")}'";
    return '''
(() => {
  if (window.__binaLoginWired) return 'already-wired';
  window.__binaLoginWired = true;

  function findEmailInput() {
    return document.querySelector('input[type="email"]')
        || document.querySelector('input[autocomplete="username"]')
        || document.querySelector('input[name*="user" i], input[name*="email" i]')
        || document.querySelector('input[type="text"]:not([type="search"])');
  }
  function findPasswordInput() {
    return document.querySelector('input[type="password"]');
  }
  function findSignInButton() {
    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"]'));
    return buttons.find(b => /sign\\s*in/i.test(b.textContent || b.value || ''))
        || document.querySelector('form button[type="submit"]')
        || document.querySelector('form input[type="submit"]');
  }
  function setValue(el, val) {
    // React/Angular friendly setter — bypass framework's tracking.
    const proto = el.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  const emailInput = findEmailInput();
  const passwordInput = findPasswordInput();
  const signIn = findSignInButton();

  if (!emailInput || !passwordInput) {
    // Not a login page (or markup changed). Bail without harm.
    return 'no-login-fields';
  }

  // (1) Autofill if we have stored creds
  const autofillEmail = $emailJs;
  const autofillPassword = $passwordJs;
  if (autofillEmail) setValue(emailInput, autofillEmail);
  if (autofillPassword) setValue(passwordInput, autofillPassword);

  // (2) Hook the Sign In button to capture before submit. Use 'mousedown'
  // (and 'click') so we run BEFORE form submission JS clears values.
  function captureAndForward() {
    const emailVal = emailInput.value || '';
    const passwordVal = passwordInput.value || '';
    if (!emailVal || !passwordVal) return;
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler('capturedCredentials', {
        portal: 'stanford',
        email: emailVal,
        password: passwordVal,
        wasAutofilled: emailVal === autofillEmail,
      });
    }
  }
  if (signIn) {
    signIn.addEventListener('mousedown', captureAndForward, { capture: true });
    signIn.addEventListener('click', captureAndForward, { capture: true });
  }
  const form = emailInput.closest('form');
  if (form) form.addEventListener('submit', captureAndForward, { capture: true });

  return 'wired';
})();
''';
  }
}
