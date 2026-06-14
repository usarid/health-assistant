/// JS scripts the host injects into the WebView per scrape job.
class ScrapeJobs {
  /// Per-visit Stanford scrape. Runs on a
  /// /signedin/appointments/after-visit-summary/csn=X&encType=3 page.
  ///
  /// Two layouts in the wild:
  ///   - **Inline view**: Clinical Notes tab opens to a single rendered note
  ///     in `.pgSection`. Old code path — capture `.pgSection.outerHTML` and
  ///     send.
  ///   - **List view**: Clinical Notes tab opens to a list of "VIEW NOTE"
  ///     buttons (one per note). These are the multi-note visits — typically
  ///     hospital stays with nursing + progress + consults + discharge notes
  ///     all under one CSN. Original v3 scraper missed these entirely
  ///     (capture=0 on 9/106 visits in 2026-06-13 run). New code path:
  ///     iterate buttons, click → wait for body → capture → back → repeat.
  ///
  /// Sends to Dart via callHandler('saveNote', payload):
  ///   - single-note: { csn, html, error? }
  ///   - multi-note:  { csn, html: '', notes: [{label, html}, ...] }
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

  // Diagnostic emit — only payload metadata (counts, lengths, URL paths).
  // Never note text or labels. Surfaces in the in-app diag strip when the
  // user toggles diagnostics on; otherwise lives in debugPrint only.
  function diag(stage, fields) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      const payload = { stage };
      if (fields) for (const k in fields) payload[k] = fields[k];
      window.flutter_inappwebview.callHandler('noteDiag', payload);
    }
  }
  function urlPathOnly(u) {
    // Drop query/fragment (which contain csn=...) — keep just the path.
    try { return new URL(u).pathname; } catch (_) { return ''; }
  }

  const m = location.href.match(/csn=([^&]+)/);
  const csn = m ? decodeURIComponent(m[1]) : 'unknown';

  function clickClinicalNotesTab() {
    const candidates = Array.from(document.querySelectorAll('li, a, button'))
      .filter(el => /Clinical Notes/i.test(el.textContent || ''));
    const tab = candidates.find(el => (el.textContent || '').trim() === 'Clinical Notes')
      || candidates[0];
    if (tab) tab.click();
    return !!tab;
  }

  function findViewNoteButtons() {
    return Array.from(document.querySelectorAll('a, button'))
      .filter(el => /^\\s*view\\s*note\\s*\$/i.test(el.textContent || ''));
  }

  async function ensureListView() {
    let btns = findViewNoteButtons();
    if (btns.length > 0) return btns;
    clickClinicalNotesTab();
    const t = Date.now();
    while (Date.now() - t < 5000) {
      await new Promise(r => setTimeout(r, 300));
      btns = findViewNoteButtons();
      if (btns.length > 0) return btns;
    }
    return [];
  }

  if (!clickClinicalNotesTab()) {
    send({ csn, html: '', error: 'no-notes-tab' });
    return;
  }

  // Race window: poll for one of:
  //   (a) VIEW NOTE buttons → list view
  //   (b) Stanford's "No Notes Available" copy anywhere on the page
  //       → empty visit (no care-team notes shareable for this encounter)
  //   (c) .pgSection grows past 200 chars → inline note rendered
  // Check buttons FIRST each tick — on a multi-note visit, .pgSection may
  // have the list text >200 chars even when buttons are the right path.
  // 'empty' check is next, scanning document.body (not just .pgSection),
  // because Stanford renders the empty-state panel in a sibling container
  // for some visit types — first run showed the regex never matched when
  // scoped to .pgSection.
  function bodyHasNoNotesAvailable() {
    return /no\\s+notes\\s+available/i.test(document.body?.textContent || '');
  }
  const startedAt = Date.now();
  let mode = null;
  while (Date.now() - startedAt < $pollMs) {
    await new Promise(r => setTimeout(r, 400));
    if (findViewNoteButtons().length > 0) { mode = 'list'; break; }
    if (bodyHasNoNotesAvailable()) { mode = 'empty'; break; }
    const section = document.querySelector('.pgSection');
    if (section && section.textContent.length > 200) { mode = 'inline'; break; }
  }
  // Final disambiguation after timeout: maybe the empty-state panel
  // appeared just past the poll window. Cheap — only runs when we'd
  // otherwise give up.
  if (!mode && bodyHasNoNotesAvailable()) mode = 'empty';

  diag('mode-detected', { mode: mode || 'timeout', elapsedMs: Date.now() - startedAt });

  if (mode === 'empty') {
    send({ csn, html: '', error: 'no-notes-available' });
    return;
  }

  if (mode === 'inline') {
    send({ csn, html: document.querySelector('.pgSection').outerHTML });
    return;
  }

  if (mode === 'list') {
    const initialButtonCount = findViewNoteButtons().length;
    diag('list-entered', { buttonCount: initialButtonCount });
    const notes = [];
    // Dedup ledger: every successful capture's (length, first-200-chars) is
    // recorded. If a later iteration produces a match, the click didn't
    // actually fire (Stanford redisplayed the previous note — diag found
    // this happens at ~1 in 7 iterations on long lists). Without dedup
    // we'd mislabel note N's body as belonging to note M.
    const seenCaptures = [];
    function isDuplicate(html) {
      if (!html) return false;
      const head = html.slice(0, 200);
      return seenCaptures.some(c => c.len === html.length && c.head === head);
    }

    for (let i = 0; i < initialButtonCount; i++) {
      const btns = await ensureListView();
      diag('iter-start', { i, btnsAvailable: btns.length });
      if (i >= btns.length) {
        diag('iter-aborted', { i, reason: 'no-button-at-index' });
        break;
      }
      const btn = btns[i];

      // Snapshot row label (note title + signer + date) for downstream
      // categorization. Strips "VIEW NOTE" itself, collapses whitespace.
      const row = btn.closest('tr') || btn.closest('li') || btn.closest('div');
      const label = (row?.textContent || '')
        .replace(/view\\s*note/gi, '')
        .replace(/\\s+/g, ' ')
        .trim()
        .slice(0, 200);

      const beforeUrl = location.href;
      const beforeText = document.querySelector('.pgSection')?.textContent || '';
      diag('pre-click', {
        i,
        urlPath: urlPathOnly(beforeUrl),
        sectionLen: beforeText.length,
        labelLen: label.length,
      });
      btn.click();

      // Wait for the per-note body to render. After a back-link click the
      // section is reset to empty, so any non-empty .pgSection that's
      // grown past a small floor (100 chars — long enough to filter out
      // loading-state placeholders, short enough to accept brief notes
      // like refill stubs, declined-vaccine entries, etc.) is a real note.
      // Diag showed successful captures resolve in 400-810ms; cap the
      // poll at 5s instead of 15s so failed iterations don't bleed the
      // session timeout budget.
      let html = '';
      let finalSectionLen = 0;
      let sectionChanged = false;
      const t0 = Date.now();
      while (Date.now() - t0 < 5000) {
        await new Promise(r => setTimeout(r, 400));
        const section = document.querySelector('.pgSection');
        const text = section?.textContent || '';
        finalSectionLen = text.length;
        if (text !== beforeText) sectionChanged = true;
        if (section && text.length > 100 && text !== beforeText) {
          html = section.outerHTML;
          break;
        }
      }

      const duplicate = isDuplicate(html);
      diag('post-click', {
        i,
        urlPath: urlPathOnly(location.href),
        urlChanged: location.href !== beforeUrl,
        sectionLen: finalSectionLen,
        sectionChanged,
        capturedLen: html.length,
        duplicate,
        pollMs: Date.now() - t0,
      });

      if (html && !duplicate) {
        seenCaptures.push({ len: html.length, head: html.slice(0, 200) });
        notes.push({ label, html, htmlLength: html.length });
      } else {
        // Push the label anyway so the downstream count of attempted
        // notes is accurate; convert_mobile_batch_to_v3_notes.py skips
        // entries with empty html.
        notes.push({ label, html: '', htmlLength: 0 });
      }

      // Navigate back to the list. Prefer history.back() when click changed
      // the URL; otherwise look for an in-page back affordance.
      let backMethod = 'none';
      if (location.href !== beforeUrl) {
        history.back();
        backMethod = 'history.back';
      } else {
        const back = Array.from(document.querySelectorAll('a, button'))
          .find(el => /back\\s+to|return/i.test(el.textContent || ''));
        if (back) {
          back.click();
          backMethod = 'back-link-click';
        }
      }
      await new Promise(r => setTimeout(r, 800));
      diag('post-back', {
        i,
        method: backMethod,
        urlPath: urlPathOnly(location.href),
        buttonsNow: findViewNoteButtons().length,
      });
    }

    diag('list-done', {
      capturedCount: notes.filter(n => n.htmlLength > 0).length,
      emptyCount: notes.filter(n => n.htmlLength === 0).length,
    });

    if (notes.length === 0) {
      send({ csn, html: '', error: 'list-view-no-notes-captured' });
    } else {
      send({ csn, html: '', notes });
    }
    return;
  }

  send({ csn, html: '', error: 'timeout-waiting-for-pgSection' });
})();
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
