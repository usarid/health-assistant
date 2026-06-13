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

  /// Injected on every load that lands on a Stanford login page. Stanford
  /// is an Angular SPA — login form fields are typically NOT in the DOM
  /// when onLoadStop first fires, they appear a beat or two later. So
  /// instead of probing once, we install a MutationObserver that wires
  /// up the moment the inputs+button exist, then disconnects.
  ///
  /// What "wiring up" does, on tap of the Sign In button:
  ///   1. If there are stored creds (autofillEmail/autofillPassword passed
  ///      in), set them into the form fields BEFORE the user sees the page.
  ///      They tap Sign In themselves.
  ///   2. When the Sign In button is tapped (whether autofilled or
  ///      hand-typed), capture the field values and call back to Dart via
  ///      the 'capturedCredentials' handler. Dart decides whether to ask
  ///      the user to save them.
  ///
  /// Idempotent — re-injecting on a page that's already wired is a no-op.
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

  const autofillEmail = $emailJs;
  const autofillPassword = $passwordJs;

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
    // Stanford's "SIGN IN" affordance isn't a <button> — empirically the
    // CSS selector for <button> + input[type=submit] missed it. Widen to
    // anchors and role=button elements, plus any clickable text match.
    const candidates = Array.from(document.querySelectorAll(
      'button, input[type="submit"], a, [role="button"], [tabindex]'));
    const byText = candidates.find(el =>
      /sign\\s*in/i.test(el.textContent || el.value || ''));
    if (byText) return byText;
    return document.querySelector('form button[type="submit"]')
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

  function emit(name, payload) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler(name, payload);
    }
  }

  function tryWire() {
    const emailInput = findEmailInput();
    const passwordInput = findPasswordInput();
    if (!emailInput || !passwordInput) return false;

    window.__binaLoginWired = true;

    if (autofillEmail) setValue(emailInput, autofillEmail);
    if (autofillPassword) setValue(passwordInput, autofillPassword);

    const signIn = findSignInButton();
    const form = emailInput.closest('form');
    emit('loginDiag', {
      stage: 'wired',
      hasEmail: !!emailInput,
      hasPassword: !!passwordInput,
      hasSignIn: !!signIn,
      hasForm: !!form,
      url: location.href.split('?')[0],
    });

    function captureAndForward(eventType) {
      const emailVal = emailInput.value || '';
      const passwordVal = passwordInput.value || '';
      emit('loginDiag', {
        stage: 'capture-fired',
        via: eventType,
        emailLen: emailVal.length,
        passwordLen: passwordVal.length,
      });
      if (!emailVal || !passwordVal) return;
      emit('capturedCredentials', {
        portal: 'stanford',
        email: emailVal,
        password: passwordVal,
        wasAutofilled: emailVal === autofillEmail,
      });
    }

    // Broad-net event listeners — multiple paths to the same handler so
    // whichever Stanford uses to actually submit, we catch it.
    if (signIn) {
      signIn.addEventListener('mousedown', () => captureAndForward('signin-mousedown'), { capture: true });
      signIn.addEventListener('click', () => captureAndForward('signin-click'), { capture: true });
      signIn.addEventListener('touchstart', () => captureAndForward('signin-touchstart'), { capture: true });
    }
    if (form) {
      form.addEventListener('submit', () => captureAndForward('form-submit'), { capture: true });
    }
    passwordInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') captureAndForward('password-enter');
    }, { capture: true });
    // Document-level fallback: any click that happens while both fields
    // have content. Fires more than once in some flows; capturedCredentials
    // handler on Dart side dedups against currently-stored value.
    document.addEventListener('click', (e) => {
      if (!emailInput.value || !passwordInput.value) return;
      // Only fire for clicks on elements that look like submit/login affordances
      const target = e.target;
      const text = (target.textContent || target.value || '').trim();
      if (/sign\\s*in|continue|log\\s*in|submit/i.test(text)) {
        captureAndForward('document-click:' + text.slice(0, 20));
      }
    }, { capture: true });

    console.log('[bina] login form wired (email + password + signIn=' + !!signIn + ' form=' + !!form + ')');
    return true;
  }

  if (tryWire()) return 'wired-immediately';

  // Form not yet in DOM — observe and re-attempt as it appears.
  const observer = new MutationObserver(() => {
    if (tryWire()) observer.disconnect();
  });
  observer.observe(document.body, { childList: true, subtree: true });

  // Stop observing after 30s no matter what so we don't leak the observer
  // across long-lived sessions.
  setTimeout(() => observer.disconnect(), 30000);
  return 'observing-for-form';
})();
''';
  }
}
