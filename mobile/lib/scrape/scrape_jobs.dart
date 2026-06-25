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

  // Wait for the Clinical Notes tab to mount before clicking it. The
  // Stanford SPA can take 2-5s on heavily-loaded pages (especially hospital
  // stays with many notes) — a one-shot check right after settle was
  // failing intermittently with 'no-notes-tab' on visits we'd successfully
  // captured before. Also bail fast if "No Notes Available" is already on
  // the page (means the visit has no notes — no tab to click).
  async function waitAndClickClinicalNotesTab(timeoutMs) {
    const t = Date.now();
    while (Date.now() - t < timeoutMs) {
      if (/no\\s+notes\\s+available/i.test(document.body?.textContent || '')) {
        return { result: 'empty', elapsedMs: Date.now() - t };
      }
      if (clickClinicalNotesTab()) {
        return { result: 'clicked', elapsedMs: Date.now() - t };
      }
      await new Promise(r => setTimeout(r, 300));
    }
    return { result: 'no-tab', elapsedMs: Date.now() - t };
  }

  const tw = await waitAndClickClinicalNotesTab(5000);
  diag('tab-wait', tw);
  const tabResult = tw.result;
  if (tabResult === 'empty') {
    send({ csn, html: '', error: 'no-notes-available' });
    return;
  }
  if (tabResult === 'no-tab') {
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

    // One click cycle: scroll into view, click, poll for new content.
    // Returns { html, finalSectionLen, sectionChanged, pollMs } where html
    // is '' if nothing new appeared within 5s.
    async function clickAndPoll(btn, beforeText) {
      btn.scrollIntoView({ block: 'center', behavior: 'instant' });
      await new Promise(r => setTimeout(r, 200));
      btn.click();

      // 100 char floor accepts brief notes (refill stubs, declined-vaccine
      // entries); diag showed successful captures resolve in 400-810ms.
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
      return { html, finalSectionLen, sectionChanged, pollMs: Date.now() - t0 };
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

      // First attempt
      let r = await clickAndPoll(btn, beforeText);
      let html = r.html;
      let duplicate = isDuplicate(html);
      let attempts = 1;

      // Stanford occasionally serves the sibling note's content when we
      // click a different button immediately after a successful capture
      // (proven via user-confirmed screenshots: the "missing" notes DO
      // have real content in MyHealth). The click appears to coalesce
      // with the previous one. Retry with a longer cool-off + a fresh
      // re-find of the button (DOM may have re-rendered).
      while (duplicate && attempts < 3) {
        diag('dedup-retry', { i, attempt: attempts, prevLen: html.length });
        await new Promise(r2 => setTimeout(r2, 1500));
        const freshBtns = await ensureListView();
        if (i >= freshBtns.length) break;
        const freshBtn = freshBtns[i];
        // Re-fetch beforeText too — back-nav may have reset .pgSection
        const bt = document.querySelector('.pgSection')?.textContent || '';
        r = await clickAndPoll(freshBtn, bt);
        html = r.html;
        duplicate = isDuplicate(html);
        attempts += 1;
      }

      diag('post-click', {
        i,
        urlPath: urlPathOnly(location.href),
        urlChanged: location.href !== beforeUrl,
        sectionLen: r.finalSectionLen,
        sectionChanged: r.sectionChanged,
        capturedLen: html.length,
        duplicate,
        attempts,
        pollMs: r.pollMs,
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
  /// Scrape one page of the Stanford messages folder (inbox or outbox).
  /// Caller must have navigated the WebView to /signedin/messages/<folder>
  /// (optionally with a page param) before injecting this.
  ///
  /// Stanford's message lists are rendered as tables of rows; each row
  /// contains an `<a>` linking to /signedin/messages/detail/<folder>/<id>
  /// — that link gives us the stable Epic message ID and (via its text)
  /// the subject. Surrounding row cells carry the other-party display
  /// name and the received/sent timestamp.
  ///
  /// Returns via the 'messageList' handler:
  ///   { url, folder, rows: [{id, subject, otherParty, date, isUnread, isReply}],
  ///     pagination: {hasNext, nextHref?}, error? }
  ///
  /// Robust to either `<tr>` table rows or `<li>` list rows — selectors
  /// fall back across both. Polls for up to 8s for rows to appear (Epic
  /// SPA mount latency).
  /// [cursor] is Stanford's `nextPageBeginMessageId` from a prior page's
  /// response — pass it on subsequent pages to get the next slice via
  /// cursor-based pagination. Null/omitted on the first page.
  static String stanfordMessageList({String? cursor}) {
    final cursorJs = cursor == null
        ? 'null'
        : "'${cursor.replaceAll(r"\", r"\\").replaceAll("'", r"\'")}'";
    return '''
(async () => {
  const cursor = $cursorJs;
  function emit(payload) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler('messageList', payload);
    }
  }

  // Outer safety net — any uncaught exception inside the body (e.g. a
  // ReferenceError from a typo, a future TDZ bug, a missing-property
  // access against a moved DOM) gets reported as a structured error
  // instead of leaving Dart to time out at 60s with no diag fields.
  // Without this, a one-character typo silently burns the whole batch.
  try {

  function findRows() {
    // Lenient selector: matches both absolute (/signedin/messages/detail/...)
    // and relative (/messages/detail/...) href attributes. a.href getter
    // always returns the absolute URL, which we then regex.
    const links = Array.from(document.querySelectorAll(
      'a[href*="messages/detail/"]'));
    const rows = [];
    for (const a of links) {
      const m = a.href.match(/\\/messages\\/detail\\/(inbox|outbox)\\/(\\d+)/);
      if (!m) continue;
      const folder = m[1];
      const id = m[2];
      const subject = (a.textContent || '').trim();

      // Find the enclosing row, prefer tr/li, fallback to any ancestor
      // whose immediate children look like cells.
      let row = a.closest('tr') || a.closest('li');
      if (!row) {
        let p = a.parentElement;
        while (p && p !== document.body) {
          const kids = p.children;
          if (kids.length >= 3 && kids.length <= 8) { row = p; break; }
          p = p.parentElement;
        }
      }
      let otherParty = '', date = '';
      if (row) {
        // Tables: row.children are <td>s. Lists: row.children are <span>/<div>s.
        // We assume the link's containing cell is the subject; the next
        // two non-empty cells are the other-party + date.
        const cells = Array.from(row.children).filter(
          c => (c.textContent || '').trim().length > 0);
        const linkCell = a.closest('td') || a.closest('li > *') || a.parentElement;
        const linkIdx = cells.indexOf(linkCell);
        const after = linkIdx >= 0 ? cells.slice(linkIdx + 1) : cells.slice(1);
        if (after[0]) otherParty = (after[0].textContent || '').trim();
        if (after[1]) date = (after[1].textContent || '').trim();
      }
      // Unread detection: Stanford bolds unread subjects; check inline style
      // or class names or the subject's font-weight.
      const isUnread =
        /unread/i.test((row?.className || '') + ' ' + (a.className || '')) ||
        parseInt(window.getComputedStyle(a).fontWeight) >= 600;
      const isReply = /^re\\b/i.test(subject);

      rows.push({ folder, id, subject, otherParty, date, isUnread, isReply });
    }
    // De-dup by id (sometimes Stanford renders a link twice — e.g. icon + text)
    const seen = new Set();
    return rows.filter(r => {
      const key = r.folder + ':' + r.id;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  // Resolve the current folder up-front. The primer + diagnostics
  // reference `folder` before the poll loop runs, so it must be in
  // scope here, not declared later (const has a TDZ — touching it
  // before the declaration throws ReferenceError and kills the whole
  // script silently, then Dart waits for an emit that never fires).
  const __folderMatch = location.pathname.match(/\\/messages\\/(inbox|outbox)/);
  const folder = __folderMatch ? __folderMatch[1] : 'unknown';

  // ── XHR + fetch interception ─────────────────────────────────
  // Capture the URLs of every network call the SPA makes during the
  // poll window. URL-only, no request/response bodies (URLs are
  // routing info, not patient data — PHI-safe). This is the killer
  // diagnostic: once we see the actual API endpoint Stanford's SPA
  // hits to load the list, we may be able to call it directly and
  // skip iframe + render-wait entirely.
  const t0 = Date.now();
  const xhrLog = [];
  try {
    const origFetch = window.fetch;
    window.fetch = function(input, init) {
      const url = (typeof input === 'string') ? input : (input && input.url) || '';
      xhrLog.push({ kind: 'fetch', method: (init && init.method) || 'GET',
                    url: url.slice(0, 200), at: Date.now() - t0 });
      return origFetch.apply(this, arguments);
    };
    const OrigXHR = window.XMLHttpRequest;
    const origOpen = OrigXHR.prototype.open;
    const origSend = OrigXHR.prototype.send;
    OrigXHR.prototype.open = function(method, url) {
      this.__binaMethod = method;
      this.__binaUrl = (url || '').slice(0, 200);
      this.__binaOpenAt = Date.now() - t0;
      xhrLog.push({ kind: 'xhr', method, url: this.__binaUrl, at: this.__binaOpenAt });
      return origOpen.apply(this, arguments);
    };
    // Also capture XHR request bodies for the two endpoints we care
    // about — lets us see exactly what the SPA's working call sends,
    // so we can match it. Bodies on /Mailbox/Page and /Outbox/Page are
    // filter + pagination params; never message content.
    OrigXHR.prototype.send = function(body) {
      const u = this.__binaUrl || '';
      if (/\\/(Mailbox|Outbox)\\/Page/.test(u)) {
        let bodyStr = '';
        try {
          bodyStr = (body == null) ? ''
            : (typeof body === 'string') ? body
            : (body && body.toString) ? body.toString()
            : JSON.stringify(body);
        } catch (_) { bodyStr = '<unserializable>'; }
        xhrLog.push({
          kind: 'xhr-body',
          url: u,
          method: this.__binaMethod || '',
          body: bodyStr.slice(0, 400),
          at: Date.now() - t0,
        });
      }
      return origSend.apply(this, arguments);
    };
  } catch (_) {}

  // ── Direct API call (primary path) ──────────────────────────────
  // XHR interception from the prior diag revealed the Stanford SPA
  // calls these JSON endpoints internally for the message list:
  //   POST /Private/Ajax/V1/Mailbox/Page  (inbox)
  //   POST /Private/Ajax/V1/Outbox/Page   (sent)
  // The session cookies are already on the wrapper origin (myhealth.*),
  // so we can call them ourselves with credentials: 'include'. No SPA,
  // no iframe, no DOM. This bypasses the entire SPA-not-rendering
  // problem we hit on direct-loadUrl.
  //
  // Response shape is unknown — try a few common Epic conventions for
  // pulling messages out of the JSON, and fall back to dumping the
  // top-level structure into the diag if none of them match.
  // Cycle through plausible request-body shapes — Stanford's endpoint
  // accepts {} for outbox but returns empty for inbox. We don't yet
  // know which body the SPA itself sends for inbox (the XHR body
  // capture didn't fire because we short-circuit before the primer).
  // Each candidate is tried in sequence; first one that returns rows
  // wins. Cursor pagination overrides the body entirely.
  function bodyCandidatesFor(folder, cursor) {
    // Confirmed via DevTools capture of the SPA's own scroll-pagination
    // POST: the `payload` field is a discriminated union —
    //   payload: false          → first page (default request)
    //   payload: "<cursorStr>"  → next page starting at that cursor
    // The string value is the bare cursor we received in
    // myHealthMailboxPage.nextPageBeginMessageId (with the ^ suffix).
    return [{ payload: cursor || false }];
  }

  async function fetchOnce(endpoint, body) {
    try {
      const resp = await fetch(endpoint, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify(body),
      });
      const text = await resp.text();
      try { return { ok: resp.ok, status: resp.status, data: JSON.parse(text) }; }
      catch (e) { return { ok: false, status: resp.status, error: 'json-parse-failed',
                            sample: text.slice(0, 500) }; }
    } catch (e) {
      return { ok: false, error: 'fetch-failed', message: (e && e.message) || '' };
    }
  }

  async function fetchListViaAPI() {
    const endpoint = folder === 'inbox'
      ? '/Private/Ajax/V1/Mailbox/Page'
      : '/Private/Ajax/V1/Outbox/Page';
    const candidates = bodyCandidatesFor(folder, cursor);
    const attempts = [];
    let lastSuccess = null;
    for (const body of candidates) {
      const t0attempt = Date.now();
      const r = await fetchOnce(endpoint, body);
      const rows = (r.ok && r.data) ? extractRowsFromApiResponse(r.data) : null;
      const mp = r.data && r.data.myHealthMailboxPage;
      const responseCursor = (mp && mp.nextPageBeginMessageId) || null;
      // Stanford's server-side status; e.g. {code: 1} usually means OK,
      // negative codes signal auth/permission/validation failures.
      const metaCode = (r.data && r.data.meta && r.data.meta.code) ?? null;
      attempts.push({
        body, status: r.status || null, ok: r.ok || false,
        rowsExtracted: rows ? rows.length : 0,
        elapsedMs: Date.now() - t0attempt,
        responseCursor,
        metaCode,
      });
      if (r.ok) lastSuccess = { result: r, rows };
      // Success criterion differs for paginated vs first-page calls:
      //   - First page (no cursor): any rows returned wins.
      //   - Paginated (cursor sent): rows AND a different cursor returned.
      //     If responseCursor === cursor, Stanford ignored our body shape
      //     and returned the same page again — keep trying other candidates.
      const advanced = !cursor || responseCursor !== cursor;
      if (rows && rows.length > 0 && advanced) {
        return { ...lastSuccess.result, rows, attempts };
      }
    }
    // No candidate produced rows. Return the last successful response
    // (for shape diagnostics) or the last failure.
    if (lastSuccess) return { ...lastSuccess.result, rows: lastSuccess.rows, attempts };
    return { ...attempts[attempts.length - 1], rows: null, attempts };
  }

  // PHI-safe structural snapshot of an arbitrary JSON value — keys,
  // array lengths, first-item keys if applicable. NO values, no message
  // text, no sender names. Recurses 3 levels deep so we can see all the
  // way down to messageList[0]'s field names, which is what we need to
  // map onto our row shape.
  function describeShape(v, depth) {
    depth = depth || 0;
    if (depth > 3) return { truncated: true };
    if (v === null) return { type: 'null' };
    if (Array.isArray(v)) {
      return { type: 'array', length: v.length,
               firstItem: v.length > 0 ? describeShape(v[0], depth + 1) : null };
    }
    if (typeof v === 'object') {
      const keys = Object.keys(v);
      const out = { type: 'object', keys: keys.slice(0, 30) };
      if (depth < 3) {
        out.children = {};
        for (const k of keys.slice(0, 30)) {
          out.children[k] = describeShape(v[k], depth + 1);
        }
      }
      return out;
    }
    return { type: typeof v };
  }

  // Try to extract message rows from various plausible response shapes.
  // Returns null if nothing matched, or an array of {id, subject, ...}.
  function extractRowsFromApiResponse(data) {
    if (!data || typeof data !== 'object') return null;
    // Confirmed: Stanford returns { meta, myHealthMailboxPage: { more, messageList } }.
    // Listed first; the other candidates remain as a defense against
    // tenant or version variation.
    const mp = data.myHealthMailboxPage;
    const candidates = [
      mp && mp.messageList,
      mp && mp.Messages,
      data.Messages, data.messages,
      data.Items, data.items,
      data.Results, data.results,
      data.Data, data.data,
      Array.isArray(data) ? data : null,
    ].filter(c => Array.isArray(c) && c.length >= 0);
    if (candidates.length === 0) return null;
    const arr = candidates[0];
    return arr.map(m => {
      // Confirmed via DevTools capture of Stanford's actual response.
      // myHealthMailboxPage.messageList[].* fields:
      //   id, senderName, title, dateSent, dateTimeSent, read,
      //   myHealthAttachments, incompleteTasks
      // For outbox the equivalent of senderName is recipient(s);
      // Stanford may use 'recipientName' or similar — fall back through
      // the legacy names just in case.
      const id = m.id || m.Id || m.ID || m.MessageId || '';
      const subject = m.title || m.subject || m.Title || m.Subject || '';
      const otherParty = m.senderName || m.recipientName || m.toName
                      || m.from || m.to || m.From || m.To || '';
      const date = m.dateSent || m.dateTimeSent || m.dateReceived
                || m.date || m.Date || '';
      const isUnread = (m.read === false) || !!(m.IsUnread || m.unread);
      const isReply = /^re\\b/i.test(String(subject));
      return {
        folder,
        id: String(id),
        subject: String(subject),
        otherParty: typeof otherParty === 'string' ? otherParty : JSON.stringify(otherParty),
        date: String(date),
        isUnread,
        isReply,
        hasAttachments: !!(m.myHealthAttachments || m.hasAttachments),
        incompleteTasks: !!m.incompleteTasks,
      };
    }).filter(r => r.id);
  }

  // Timing breakdown for the whole script — emitted in the final
  // diag so we can pinpoint where each ms goes per page.
  const timings = {};
  function mark(label, ms) { timings[label] = ms; }

  // JS-bundle grep — one-shot when we have a cursor (i.e., page 2+).
  // Fetches the SPA's own JS sources, finds any usage of /Mailbox/Page,
  // and returns a snippet of surrounding code. The SPA's pagination
  // call shape is constructed in there; reading the grep result tells
  // us the exact body without needing to simulate a scroll on the
  // rendered list. Runs only on pagination pages to avoid the cost on
  // first-page success.
  async function grepSpaJs(needle) {
    try {
      const allScripts = Array.from(document.querySelectorAll('script[src]'))
        .map(s => s.src).filter(u => u && /\\.js(\\?|\$)/i.test(u));
      const needleVariants = [
        needle, needle.replace(/^\\//, ''),
        needle.split('/').pop() + 'Page', needle.toLowerCase(),
      ];
      // Per-script fetch result records (always populated, even on error)
      // so we can see WHY a fetch failed when zero hits come back.
      // Static JS bundles are public — drop credentials so the request
      // isn't CORS-blocked as a cross-origin credentialed call.
      const probeResults = await Promise.all(allScripts.slice(0, 30).map(async url => {
        const t0 = Date.now();
        try {
          const r = await fetch(url, { mode: 'cors' });
          if (!r.ok) return { url, status: r.status, error: 'http-' + r.status, ms: Date.now() - t0 };
          const text = await r.text();
          return { url, status: r.status, length: text.length, text, ms: Date.now() - t0 };
        } catch (e) {
          return { url, error: 'fetch-threw:' + ((e && e.message) || String(e)).slice(0, 80), ms: Date.now() - t0 };
        }
      }));
      const hits = [];
      const probedUrls = [];
      for (const p of probeResults) {
        probedUrls.push({ url: p.url, length: p.length || 0,
                          status: p.status || null, error: p.error || null,
                          ms: p.ms });
        if (!p.text) continue;
        for (const v of needleVariants) {
          const idx = p.text.indexOf(v);
          if (idx >= 0) {
            hits.push({
              url: p.url, needle: v,
              snippet: p.text.substring(Math.max(0, idx - 400), idx + 800),
            });
            break;
          }
        }
      }
      return {
        hits,
        probedCount: probeResults.filter(p => p.text).length,
        totalScripts: allScripts.length,
        probedUrls,  // ALWAYS populated; tells us what we tried
      };
    } catch (e) {
      return { error: (e && e.message) || String(e) };
    }
  }

  // Make the API call NOW, before priming the SPA — if it works we're
  // done in <1s and the SPA priming is moot.
  const apiStart = Date.now();
  const apiResult = await fetchListViaAPI();
  mark('apiCallMs', Date.now() - apiStart);
  const apiRows = apiResult.rows || null;
  const apiShape = (apiResult.ok && apiResult.data) ? describeShape(apiResult.data) : null;

  // Extract the next-page cursor from Stanford's response (when present).
  // Outbox always carries currentPageBeginMessageId / nextPageBeginMessageId
  // when more pages exist; inbox may or may not.
  const mp = apiResult.data && apiResult.data.myHealthMailboxPage;
  const nextCursor = (mp && mp.more && mp.nextPageBeginMessageId)
    ? mp.nextPageBeginMessageId : null;

  // PAGINATION JS-GREP DIAGNOSTIC: when we have a cursor but no body
  // variant ADVANCED the cursor, grep the SPA's JS bundles for the
  // pagination call shape. The trigger is "responseCursor never differs
  // from input cursor across any attempt" — i.e., Stanford ignored
  // every body shape we tried. Row count is irrelevant; what matters is
  // whether any attempt moved the cursor forward.
  let jsGrepHits = null;
  if (cursor && apiResult.ok) {
    const attempts = apiResult.attempts || [];
    const anyAdvanced = attempts.some(
      a => a.responseCursor && a.responseCursor !== cursor);
    if (!anyAdvanced) {
      const needle = folder === 'inbox' ? '/Mailbox/Page' : '/Outbox/Page';
      jsGrepHits = await grepSpaJs(needle);
    }
  }

  // PRIMER-AS-DIAGNOSTIC: if the API returned 0 rows on page 1 (no
  // cursor), we've exhausted body guesses. Trigger the SPA to fire
  // its OWN /Mailbox/Page or /Outbox/Page call so the XHR-body
  // interception captures the exact body shape it uses. Adds ~3s
  // to a folder we'd otherwise return empty for; the captured body
  // ends up in the next emit's xhrLog and is what we need to hardcode.
  //
  // Only runs on first page of a folder (no cursor) AND only when
  // rows == 0 — pagination/successful pages skip this entirely.
  let primerForBodyCapture = null;
  if (apiResult.ok && !cursor && (!apiRows || apiRows.length === 0)) {
    primerForBodyCapture = [];
    const t0p = Date.now();
    function pLog(stage) { primerForBodyCapture.push({ stage, atMs: Date.now() - t0p }); }
    pLog('primer-start');

    function findFolderLinkRaw(targetFolder) {
      const wantText = targetFolder === 'inbox' ? 'inbox' : 'sent';
      const wantHref = targetFolder === 'inbox' ? '/messages/inbox' : '/messages/outbox';
      return Array.from(document.querySelectorAll('a, [role="link"]')).find(a => {
        const txt = (a.textContent || '').trim().toLowerCase();
        const href = a.getAttribute('href') || '';
        return txt === wantText && href.indexOf(wantHref) !== -1;
      });
    }

    const oppositeFolder = folder === 'inbox' ? 'outbox' : 'inbox';
    const oppLink = findFolderLinkRaw(oppositeFolder);
    if (oppLink) {
      oppLink.click();
      pLog('clicked-opposite');
      await new Promise(r => setTimeout(r, 1200));
    } else { pLog('opposite-link-not-found'); }
    const backLink = findFolderLinkRaw(folder);
    if (backLink) {
      backLink.click();
      pLog('clicked-target');
      await new Promise(r => setTimeout(r, 1500));
    } else { pLog('target-link-not-found'); }
    pLog('primer-done');
  }

  // SHORT-CIRCUIT: if the API call succeeded (HTTP 200), trust its
  // answer — even if it returned 0 rows. The SPA priming + 30s DOM
  // long-poll exists only as a fallback for the case where the API is
  // unreachable or auth-blocked. Saves ~35s per page in the empty-folder
  // and authenticated-but-no-results cases.
  if (apiResult.ok) {
    mark('totalMs', Date.now() - t0);
    emit({
      url: location.href,
      pathname: location.pathname,
      folder,
      rowCount: (apiRows || []).length,
      rows: apiRows || [],
      nextCursor,
      pagination: { hasNext: !!nextCursor, nextCursor },
      pollMs: 0,
      pollTimedOut: false,
      pageTitle: document.title,
      bodyLength: (document.body?.textContent || '').length,
      xhrLog,
      api: {
        status: apiResult.status,
        ok: true,
        rowsExtracted: (apiRows || []).length,
        shape: apiShape,
        attempts: apiResult.attempts || null,
        more: !!(mp && mp.more),
      },
      primerForBodyCapture,  // null on success path; populated on 0-row path
      jsGrepHits,             // populated only on pagination-stalled pages
      rowSource: (apiRows && apiRows.length > 0) ? 'api' : 'api-empty',
      timings,
      error: (apiRows && apiRows.length > 0) ? null : 'api-returned-zero-rows',
    });
    return;
  }

  // ── In-app nav primer (FALLBACK PATH) ────────────────────────
  // Only reached when the direct API call failed (network error or
  // non-2xx). The SPA-priming + DOM long-poll path tries to coax the
  // SPA into rendering rows we can scrape from the DOM as a last resort.
  mark('apiFailedFellThroughAt', Date.now() - t0);
  // 30s direct-loadUrl poll proved the Stanford SPA is inert under
  // direct deep-linking: bodyLength stayed at 7.5KB, zero XHRs, zero
  // iframes mounted. The SPA expects in-app navigation (a click within
  // its own chrome). On entry, we look for the folder sidebar link
  // matching the current folder (Inbox / Sent) and click it. This
  // is what wakes the messaging module up.
  //
  // Folder link finder: matches by text + href substring. Tries the
  // OPPOSITE folder first and back, in case clicking the active folder
  // is a no-op for the SPA router.
  function findFolderLink(targetFolder) {
    const wantText = targetFolder === 'inbox' ? 'inbox' : 'sent';
    const wantHref = targetFolder === 'inbox' ? '/messages/inbox' : '/messages/outbox';
    return Array.from(document.querySelectorAll('a, [role="link"]')).find(a => {
      const txt = (a.textContent || '').trim().toLowerCase();
      const href = (a.getAttribute('href') || '');
      return txt === wantText && href.indexOf(wantHref) !== -1;
    });
  }
  const primerEvents = [];
  function primerLog(stage, detail) {
    primerEvents.push({ stage, atMs: Date.now() - t0, ...(detail || {}) });
  }
  primerLog('start', { folder, pathname: location.pathname });

  // Step 1: ensure the SPA has finished its initial chrome mount.
  // The Inbox/Sent heading is visible at body length ~7.5KB after
  // ~2-3s — wait up to 5s for "Inbox" / "Sent Messages" h2 to be in
  // the DOM before trying the click primer.
  const t_pre = Date.now();
  while (Date.now() - t_pre < 5000) {
    const headings = Array.from(document.querySelectorAll('h1, h2'))
      .map(h => (h.textContent || '').trim().toLowerCase());
    if (headings.some(h => h === 'inbox' || h === 'sent messages')) break;
    await new Promise(r => setTimeout(r, 300));
  }
  primerLog('chrome-ready', { bodyLen: (document.body?.textContent || '').length });

  // Step 2: prime via sidebar click. We try BOTH the target folder
  // and (if needed) the opposite folder + back, since clicking the
  // active route may be a no-op for the SPA router.
  let primerLink = findFolderLink(folder);
  if (primerLink) {
    primerLink.click();
    primerLog('clicked-target-folder', { href: primerLink.getAttribute('href') });
    // Brief wait for SPA to react
    await new Promise(r => setTimeout(r, 1500));
  } else {
    primerLog('target-folder-link-not-found');
  }

  // If still no body growth after the target-folder click, try the
  // opposite-then-back sidewinder.
  if ((document.body?.textContent || '').length < 12000) {
    const oppositeFolder = folder === 'inbox' ? 'outbox' : 'inbox';
    const oppLink = findFolderLink(oppositeFolder);
    if (oppLink) {
      oppLink.click();
      primerLog('clicked-opposite-folder', { folder: oppositeFolder });
      await new Promise(r => setTimeout(r, 1500));
      const backLink = findFolderLink(folder);
      if (backLink) {
        backLink.click();
        primerLog('clicked-target-folder-again');
        await new Promise(r => setTimeout(r, 1500));
      }
    }
  }

  // ── Long poll: track iframe appearances, attempt to dismiss any
  // interstitial modal/banner, find rows when they appear ──────
  // 30s window. Skipped entirely if the direct API call already
  // returned rows (the common-case fast path).
  let rows = apiRows && apiRows.length > 0 ? apiRows : [];
  const iframeAppearances = [];
  let dismissAttempts = 0;
  while (rows.length === 0 && Date.now() - t0 < 30000) {
    rows = findRows();
    if (rows.length > 0) break;

    // Track every new iframe we see with its arrival time
    const currentIframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of currentIframes) {
      const key = (f.src || '') + ':' + (f.id || '') + ':' + (f.name || '');
      if (!iframeAppearances.some(ia => ia.key === key)) {
        iframeAppearances.push({
          key,
          atMs: Date.now() - t0,
          src: f.src || f.getAttribute('src') || '',
          id: f.id || '',
          name: f.name || '',
          width: f.offsetWidth,
          height: f.offsetHeight,
        });
      }
    }

    // Best-effort dismissal of any obvious interstitial — Stanford's config
    // had enableMessagesInterstitial:'true'. Pattern-match visible buttons
    // with common dismiss copy. Idempotent — re-clicking the same button
    // after it's gone is a no-op.
    const dismissCandidates = Array.from(document.querySelectorAll(
      'button, a, [role="button"]'))
      .filter(b => {
        if (b.offsetWidth === 0 || b.offsetHeight === 0) return false;
        const t = (b.textContent || '').trim();
        return /^(continue|ok|got it|i agree|accept|dismiss|close|view\\s+messages?)\$/i.test(t);
      });
    if (dismissCandidates.length > 0) {
      dismissCandidates[0].click();
      dismissAttempts += 1;
    }

    // If an iframe appeared and is large, check whether its contentDocument
    // is accessible (same-origin); if so, try findRows() inside.
    for (const f of currentIframes) {
      if (f.offsetWidth < 100 || f.offsetHeight < 100) continue;
      try {
        const idoc = f.contentDocument;
        if (!idoc) continue;
        const innerLinks = Array.from(idoc.querySelectorAll(
          'a[href*="messages/detail/"]'));
        if (innerLinks.length > 0) {
          // Same-origin iframe with message links — we could potentially
          // scrape from here. Mark via diag but don't change the row-pull
          // path in this discovery commit.
          xhrLog.push({ kind: 'iframe-has-links', url: f.src,
                        count: innerLinks.length, at: Date.now() - t0 });
        }
      } catch (_) { /* cross-origin */ }
    }

    await new Promise(r => setTimeout(r, 500));
  }

  // (folder is already resolved at the top of the script — see above.)

  // Pagination detection: look for a "Next" affordance. Epic typically
  // uses either a numbered pager or a Next link/button. We capture both
  // the existence and (if present) the href so the caller can navigate.
  function findNext() {
    const cands = Array.from(document.querySelectorAll(
      'a, button, [role="button"]'));
    for (const el of cands) {
      const txt = (el.textContent || '').trim();
      if (!/^next\\b/i.test(txt)) continue;
      if (el.disabled || /disabled/i.test(el.className || '')) continue;
      const href = (el.tagName === 'A' && el.href) ? el.href : null;
      return { hasNext: true, nextHref: href };
    }
    return { hasNext: false };
  }

  // ── ALWAYS-emitted diagnostics ─────────────────────────────────
  // PHI hygiene: NO text content from rows (subjects, sender names,
  // dates). Only structural and url-shaped data. Class/tag names,
  // counts, href patterns, page title/headings (which are page-chrome
  // strings like "Inbox" / "Sent Messages", not patient data).

  // Iframe enumeration — Stanford's MyHealth wrapper may load the actual
  // message list inside an iframe (cross-origin, blocked from JS access).
  // Reporting iframe src tells us where the real list URL is. Also try
  // contentDocument access — if same-origin, we can scrape from there.
  const iframeEls = Array.from(document.querySelectorAll('iframe'));
  const iframes = iframeEls.map(f => {
    let sameOriginAnchors = -1;
    let sameOriginMessageyAnchors = [];
    let accessError = null;
    try {
      const idoc = f.contentDocument;
      if (idoc) {
        sameOriginAnchors = idoc.querySelectorAll('a').length;
        sameOriginMessageyAnchors = Array.from(
          idoc.querySelectorAll('a[href*="messag"], a[href*="detail"]'))
          .slice(0, 8)
          .map(a => a.getAttribute('href'));
      } else {
        accessError = 'no-contentDocument';
      }
    } catch (e) {
      accessError = (e.message || '').slice(0, 100);
    }
    return {
      src: f.src || f.getAttribute('src') || '',
      id: f.id || '',
      name: f.name || '',
      classes: (f.className || '').slice(0, 80),
      width: f.offsetWidth,
      height: f.offsetHeight,
      visible: f.offsetWidth > 0 && f.offsetHeight > 0,
      sameOriginAnchors,
      sameOriginMessageyAnchors,
      accessError,
    };
  });

  // Body HTML snippet — chrome only since rows aren't here. PHI-safe at
  // this body length; if rows WERE rendered we'd see body length 50KB+.
  // 4000-char window is enough to see iframe declarations, key class
  // names, and any placeholder div awaiting JS-loaded content.
  const bodyHTMLSnippet = (document.body?.innerHTML || '').slice(0, 4000);

  // Body text snippet — first 400 chars of human-visible text. Lets us
  // confirm what the page actually shows (e.g., a "loading..." message,
  // a session-expired notice, the inbox header etc).
  const bodyTextSnippet = (document.body?.textContent || '')
    .replace(/\\s+/g, ' ')
    .trim()
    .slice(0, 400);

  const allAnchors = Array.from(document.querySelectorAll('a'));
  const hrefAttrs = allAnchors
    .map(a => a.getAttribute('href') || '')
    .filter(h => h && h !== '#' && !h.startsWith('javascript:'));

  // Group hrefs by their second-level path segment so we see Stanford's
  // routing style without listing every URL. e.g. {'/signedin/messages':12, '/signedin/health':3}
  const hrefBuckets = {};
  for (const h of hrefAttrs) {
    let bucket;
    try {
      const u = h.startsWith('http') ? new URL(h) : new URL(h, location.origin);
      const seg = u.pathname.split('/').slice(0, 3).join('/');
      bucket = seg || '/';
    } catch { bucket = h.split('?')[0].split('#')[0].slice(0, 40); }
    hrefBuckets[bucket] = (hrefBuckets[bucket] || 0) + 1;
  }

  // Sample of first row's parsing details (if any rows were found) — lets
  // us verify cell extraction worked correctly, PHI-safely (we emit row
  // structure only — text lengths, no content).
  const firstRowParse = rows.length > 0 ? {
    sample: { id: rows[0].id, folder: rows[0].folder,
              isReply: rows[0].isReply, isUnread: rows[0].isUnread,
              subjectLen: (rows[0].subject || '').length,
              otherPartyLen: (rows[0].otherParty || '').length,
              dateLen: (rows[0].date || '').length },
  } : null;

  // On 0 rows: structural snapshot of clickable elements (Epic SPAs
  // sometimes wire row clicks via onclick attrs, not anchors).
  const structuralDiag = rows.length === 0 ? {
    clickableRowSamples: Array.from(document.querySelectorAll(
      'tr[onclick], li[onclick], [data-msg-id], [data-message-id], [data-id]'))
      .slice(0, 5)
      .map(el => ({
        tag: el.tagName,
        classes: (el.className || '').slice(0, 80),
        onclick: (el.getAttribute('onclick') || '').slice(0, 200),
        dataMsgId: el.getAttribute('data-msg-id')
          || el.getAttribute('data-message-id')
          || el.getAttribute('data-id'),
      })),
    classedContainerSample: Array.from(document.querySelectorAll(
      'main *[class], body > div *[class]'))
      .filter(el => el.children.length > 2 && el.children.length < 50)
      .slice(0, 10)
      .map(el => ({
        tag: el.tagName,
        classes: (el.className || '').slice(0, 80),
        childCount: el.children.length,
        firstChildTag: el.children[0]?.tagName || '',
      })),
    // Message-y href patterns (closest candidates for detail-page links)
    messageyHrefs: hrefAttrs
      .filter(h => /messag|msg|detail/i.test(h))
      .slice(0, 12),
  } : null;

  const folderMatch2 = location.pathname.match(/\\/messages\\/(inbox|outbox)/);
  const onExpectedRoute = !!folderMatch2;

  emit({
    // Always — for every page, success or fail
    url: location.href,
    pathname: location.pathname,
    folder,
    rowCount: rows.length,
    rows,
    pagination: findNext(),
    pollMs: Date.now() - t0,
    pollTimedOut: rows.length === 0,
    pageTitle: document.title,
    headings: Array.from(document.querySelectorAll('h1, h2'))
      .slice(0, 3)
      .map(h => (h.textContent || '').trim().slice(0, 60)),
    anchorCount: allAnchors.length,
    anchorsWithHref: hrefAttrs.length,
    tableCount: document.querySelectorAll('table').length,
    hrefBuckets,
    onExpectedRoute,
    bodyLength: (document.body?.textContent || '').length,
    bodyTextSnippet,
    iframes,
    iframeAppearances,
    dismissAttempts,
    primerEvents,
    xhrLog,
    // Direct-API path results — structural-only describeShape; never
    // includes message text/sender/dates so the diag file stays as
    // PHI-light as the DOM-path emit was.
    api: {
      status: apiResult.status || null,
      ok: apiResult.ok || false,
      error: apiResult.error || null,
      rowsExtracted: apiRows ? apiRows.length : 0,
      shape: apiShape,
      sample: apiResult.sample || null,
    },
    rowSource: (apiRows && apiRows.length > 0) ? 'api' : (rows.length > 0 ? 'dom' : null),
    timings: { ...timings, totalMs: Date.now() - t0 },
    firstRowParse,
    error: rows.length === 0 ? 'no-rows-found' : null,
    diagnostics: structuralDiag,
    // Heavy field — kept last for readability; only included on failure
    // to keep the discovery JSON manageable on successful pages.
    bodyHTMLSnippet: rows.length === 0 ? bodyHTMLSnippet : null,
  });
  } catch (e) {
    emit({
      url: location.href,
      pathname: location.pathname,
      folder: (typeof folder === 'string') ? folder : 'unknown',
      rowCount: 0,
      rows: [],
      error: 'js-exception',
      jsError: {
        message: (e && e.message) ? String(e.message).slice(0, 300) : String(e).slice(0, 300),
        stack: (e && e.stack) ? String(e.stack).slice(0, 1500) : '',
      },
    });
  }
})();
''';
  }

  /// Fetch one Stanford message's full body via the JSON API.
  ///
  /// Mirrors the stanfordMessageList architecture:
  ///   - Hook XHR for body capture (diagnostic)
  ///   - POST to a candidate endpoint with {payload: messageId}
  ///   - On 0-row / non-2xx failure, grep the SPA's JS bundles for the
  ///     real endpoint path
  ///
  /// Caller passes the [folder] ('inbox' or 'outbox') and the message
  /// [messageId] (Stanford's numeric string ID from the discovery list).
  ///
  /// Result via callHandler('messageDetail', payload):
  ///   { folder, id, ok, status, data, attempts, jsGrepHits?, error?, timings }
  /// 'data' is the parsed JSON response from the working endpoint —
  /// the body content + thread chain + metadata. PHI-bearing; stays
  /// local on disk only.
  static String stanfordMessageDetail({
    required String folder,
    required String messageId,
  }) {
    // Stanford's data API uses /Mailbox/* paths for both inbox AND
    // outbox detail (the SPA's route /signedin/messages/detail/<folder>/<id>
    // is just URL routing, not endpoint sharding). The most likely
    // endpoint name by analogy with /Page is /Message; we also try
    // common alternates.
    final folderJs = "'${folder.replaceAll(r"\", r"\\").replaceAll("'", r"\'")}'";
    final messageIdJs = "'${messageId.replaceAll(r"\", r"\\").replaceAll("'", r"\'")}'";
    return '''
(async () => {
  const folder = $folderJs;
  const messageId = $messageIdJs;
  const t0 = Date.now();

  function emit(payload) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler('messageDetail', payload);
    }
  }

  // XHR + fetch interception — captures whatever URL Stanford itself
  // uses when navigating to a detail page (if we trigger that flow as
  // a diagnostic). PHI-safe: URL + truncated body only.
  const xhrLog = [];
  try {
    const origFetch = window.fetch;
    window.fetch = function(input, init) {
      const url = (typeof input === 'string') ? input : (input && input.url) || '';
      xhrLog.push({ kind: 'fetch', method: (init && init.method) || 'GET',
                    url: url.slice(0, 200), at: Date.now() - t0 });
      return origFetch.apply(this, arguments);
    };
    const OrigXHR = window.XMLHttpRequest;
    const origOpen = OrigXHR.prototype.open;
    const origSend = OrigXHR.prototype.send;
    OrigXHR.prototype.open = function(method, url) {
      this.__binaUrl = (url || '').slice(0, 200);
      this.__binaMethod = method;
      xhrLog.push({ kind: 'xhr', method, url: this.__binaUrl, at: Date.now() - t0 });
      return origOpen.apply(this, arguments);
    };
    OrigXHR.prototype.send = function(body) {
      const u = this.__binaUrl || '';
      if (/\\/Mailbox\\/|\\/Outbox\\/|\\/Message/.test(u)) {
        let bodyStr = '';
        try {
          bodyStr = body == null ? ''
            : (typeof body === 'string') ? body
            : JSON.stringify(body);
        } catch (_) { bodyStr = '<unserializable>'; }
        xhrLog.push({ kind: 'xhr-body', url: u, method: this.__binaMethod || '',
                      body: bodyStr.slice(0, 400), at: Date.now() - t0 });
      }
      return origSend.apply(this, arguments);
    };
  } catch (_) {}

  async function fetchOnce(endpoint, body, method) {
    method = method || 'POST';
    try {
      const init = {
        method,
        credentials: 'include',
        headers: {
          'Accept': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
      };
      if (method !== 'GET') {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
      const resp = await fetch(endpoint, init);
      const text = await resp.text();
      try { return { ok: resp.ok, status: resp.status, data: JSON.parse(text) }; }
      catch (e) {
        return { ok: false, status: resp.status, error: 'json-parse-failed',
                 sample: text.slice(0, 500) };
      }
    } catch (e) {
      return { ok: false, error: 'fetch-failed', message: (e && e.message) || '' };
    }
  }

  // Endpoint candidates — by analogy with /Page; first match wins.
  // The discriminated-union pattern (payload: false | string) from /Page
  // suggests payload: <messageId> here too.
  const candidates = [
    { endpoint: '/Private/Ajax/V1/Mailbox/Message', body: { payload: messageId } },
    { endpoint: '/Private/Ajax/V1/Mailbox/MessageDetail', body: { payload: messageId } },
    { endpoint: '/Private/Ajax/V1/Mailbox/Detail', body: { payload: messageId } },
    { endpoint: '/Private/Ajax/V1/Mailbox/Get', body: { payload: messageId } },
    // Outbox alternates
    { endpoint: '/Private/Ajax/V1/Outbox/Message', body: { payload: messageId } },
    { endpoint: '/Private/Ajax/V1/Outbox/Detail', body: { payload: messageId } },
  ];

  const attempts = [];
  let lastOk = null;
  for (const c of candidates) {
    const t0a = Date.now();
    const r = await fetchOnce(c.endpoint, c.body);
    attempts.push({
      endpoint: c.endpoint, body: c.body,
      status: r.status || null, ok: r.ok || false,
      hasData: !!(r.data),
      dataTopKeys: r.data && typeof r.data === 'object' ? Object.keys(r.data).slice(0, 8) : null,
      elapsedMs: Date.now() - t0a,
      error: r.error || null,
    });
    if (r.ok && r.data) {
      lastOk = { endpoint: c.endpoint, result: r };
      // Stanford's pattern: response wraps real data in a named field
      // alongside `meta`. If we got a 200 + parseable JSON with anything
      // beyond just `meta`, accept it as a win.
      const dataKeys = Object.keys(r.data);
      const hasContent = dataKeys.some(k => k !== 'meta');
      if (hasContent) break;
    }
  }

  // JS-source grep fallback — if no candidate gave us body content, scan
  // Stanford's JS for a hint at the real endpoint. Same mechanism we
  // used to discover {payload: false} for the list pages.
  let jsGrepHits = null;
  if (!lastOk || !attempts.some(a => a.ok && a.dataTopKeys && a.dataTopKeys.some(k => k !== 'meta'))) {
    try {
      const allScripts = Array.from(document.querySelectorAll('script[src]'))
        .map(s => s.src).filter(u => u && /\\.js(\\?|\$)/i.test(u));
      const needles = ['Mailbox/Message', 'Mailbox/Detail', 'Mailbox/Get',
                       'Outbox/Message', 'Outbox/Detail', 'messageDetail',
                       'GetMessage', 'getMessage'];
      const probed = await Promise.all(allScripts.slice(0, 30).map(async url => {
        try {
          const r = await fetch(url, { mode: 'cors' });
          if (!r.ok) return { url, status: r.status, error: 'http-' + r.status };
          const text = await r.text();
          return { url, status: r.status, length: text.length, text };
        } catch (e) {
          return { url, error: 'fetch-threw:' + ((e && e.message) || '').slice(0, 60) };
        }
      }));
      const hits = [];
      const probedUrls = [];
      for (const p of probed) {
        probedUrls.push({ url: p.url, length: p.length || 0,
                          status: p.status || null, error: p.error || null });
        if (!p.text) continue;
        for (const n of needles) {
          const idx = p.text.indexOf(n);
          if (idx >= 0) {
            hits.push({ url: p.url, needle: n,
                        snippet: p.text.substring(Math.max(0, idx - 400), idx + 800) });
            break;
          }
        }
      }
      jsGrepHits = { hits, probedCount: probed.filter(p => p.text).length,
                     totalScripts: allScripts.length, probedUrls };
    } catch (e) {
      jsGrepHits = { error: (e && e.message) || String(e) };
    }
  }

  emit({
    folder, id: messageId,
    ok: !!lastOk,
    status: lastOk ? lastOk.result.status : null,
    endpoint: lastOk ? lastOk.endpoint : null,
    data: lastOk ? lastOk.result.data : null,
    attempts,
    xhrLog,
    jsGrepHits,
    timings: { totalMs: Date.now() - t0 },
  });
})();
''';
  }

  /// Fetch one Stanford lab/imaging report's full HTML body.
  ///
  /// Unlike messages (which returned JSON via `/Private/Ajax/V1/Mailbox/
  /// Message`), lab results are served as full HTML pages at
  /// `https://mychart.stanfordhealthcare.org/myhealth_sso/app/test-results
  /// /details?eorderid=<id>&lang=en-US` (~137KB per page). Discovered via
  /// the user's DevTools capture: the legacy `/inside.asp?mode=labdetail`
  /// 302-redirects to this modern URL.
  ///
  /// Reverse-engineered via Chrome MCP recon (2026-06-23; see
  /// tools/portal-scout/captures/stanford/labs/). The SSO deep-link page
  /// is just an SPA shell; the real body lives in the JSON returned by
  /// POST /myhealth_sso/api/test-results/GetDetails.
  ///
  /// Two-step flow:
  ///   1. Fetch the SSO shell HTML once per session, regex out the
  ///      `__RequestVerificationToken` hidden field, cache on `window`.
  ///   2. For each eorderid, POST GetDetails with body
  ///      `{ orderKey, organizationID:"", PageNonce }`. The response
  ///      JSON carries `results[0].studyResult.{narrative,impression,
  ///      combinedRTFNarrativeImpression}.contentAsHtml`, plus
  ///      `resultComponents[]` for structured numerical labs.
  ///
  /// `orderKey` is the URL-decoded form of `eorderid`. Cross-origin —
  /// `Access-Control-Allow-Origin: https://myhealth.stanfordhealthcare.org`
  /// and `Allow-Credentials: true`, so `fetch(..., {credentials: 'include'})`
  /// works from the wrapper-origin WebView.
  ///
  /// Result via callHandler('labDetail', payload):
  ///   { eorderid, ok, status, details, error?, attempts, timings }
  /// where `details` is the parsed GetDetails JSON.
  static String stanfordLabDetail({required String eorderid}) {
    final eorderidJs = "'${eorderid.replaceAll(r"\", r"\\").replaceAll("'", r"\'")}'";
    return '''
(async () => {
  const eorderid = $eorderidJs;
  const t0 = Date.now();
  const attempts = [];

  function emit(payload) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler('labDetail', payload);
    }
  }
  function fail(error, extra) {
    emit({ eorderid, ok: false, error, attempts, timings: { totalMs: Date.now() - t0 }, ...(extra || {}) });
  }
  function randomHex(n) {
    const a = new Uint8Array(n / 2);
    crypto.getRandomValues(a);
    return Array.from(a, b => b.toString(16).padStart(2, '0')).join('');
  }

  // Step 1 — ensure verification token. Cache on window for the WebView session.
  const TOKEN_CACHE = '__binaStanfordRVT';
  let token = window[TOKEN_CACHE];
  if (!token) {
    const tT = Date.now();
    const shellUrl = 'https://mychart.stanfordhealthcare.org/myhealth_sso/app/test-results/details'
      + '?lang=en-US&eorderid=' + encodeURIComponent(eorderid);
    try {
      const shellResp = await fetch(shellUrl, { credentials: 'include', headers: { 'Accept': 'text/html' } });
      const shellHtml = await shellResp.text();
      attempts.push({ step: 'shell-fetch', url: shellUrl, status: shellResp.status, htmlLength: shellHtml.length, elapsedMs: Date.now() - tT });
      if (!shellResp.ok) return fail('shell-non-ok', { status: shellResp.status });
      const m = shellHtml.match(/name="__RequestVerificationToken"[^>]*value="([^"]+)"/);
      if (!m) return fail('token-not-found-in-shell');
      token = m[1];
      window[TOKEN_CACHE] = token;
    } catch (e) {
      return fail('shell-fetch-failed', { message: (e && e.message) || String(e) });
    }
  } else {
    attempts.push({ step: 'shell-cached', tokenLen: token.length });
  }

  // Step 2 — POST GetDetails. orderKey = URL-decoded eorderid.
  const orderKey = decodeURIComponent(eorderid);
  const tD = Date.now();
  const apiUrl = 'https://mychart.stanfordhealthcare.org/myhealth_sso/api/test-results/GetDetails';
  const body = JSON.stringify({ orderKey, organizationID: '', PageNonce: randomHex(32) });
  let detailsResp, details;
  try {
    detailsResp = await fetch(apiUrl, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        '__RequestVerificationToken': token,
      },
      body,
    });
  } catch (e) {
    return fail('details-fetch-failed', { message: (e && e.message) || String(e) });
  }
  const detailsText = await detailsResp.text();
  attempts.push({ step: 'getdetails', url: apiUrl, status: detailsResp.status, ok: detailsResp.ok, respLength: detailsText.length, elapsedMs: Date.now() - tD });

  // If the token went stale (Epic rotates them), clear cache so the next call refetches.
  if (detailsResp.status === 401 || detailsResp.status === 403 || detailsResp.status === 419) {
    delete window[TOKEN_CACHE];
    return fail('details-auth-failed', { status: detailsResp.status });
  }
  if (!detailsResp.ok) return fail('details-non-ok', { status: detailsResp.status });

  try {
    details = JSON.parse(detailsText);
  } catch (e) {
    return fail('details-json-parse-failed', { message: (e && e.message) || String(e), respPreview: detailsText.slice(0, 200) });
  }

  emit({
    eorderid, ok: true, status: detailsResp.status, details,
    attempts, timings: { totalMs: Date.now() - t0 },
  });
})();
''';
  }

  /// Generic Stanford "Clinical/<Section>/Load<Action>" list fetcher.
  /// Same auth pattern as [stanfordLabDetail] — reuses the cached
  /// __binaStanfordRVT verification token. POSTs to the section's
  /// endpoint with an empty body and returns the parsed JSON.
  ///
  /// Endpoint discovery: tools/portal-scout/specs/stanford-v1.json,
  /// derived from the v1.12 scout's 17 MB capture. Same shape works
  /// for Allergies/HealthIssues/Immunizations (and likely others).
  ///
  /// Result via callHandler('clinicalList', payload):
  ///   { section, ok, status, list, error?, attempts, timings }
  static String stanfordClinicalLoadList({
    required String section,        // e.g. 'Allergies'
    required String endpointPath,   // e.g. 'Allergies/LoadListData'
    Map<String, String>? extraQuery,
  }) {
    final sectionJs   = "'${section.replaceAll("'", r"\'")}'";
    final endpointJs  = "'${endpointPath.replaceAll("'", r"\'")}'";
    final qsEntries = (extraQuery ?? const {'ComponentNumber': '2', 'lang': 'en-US'})
        .entries
        .map((e) => '${Uri.encodeQueryComponent(e.key)}=${Uri.encodeQueryComponent(e.value)}')
        .join('&');
    final qsJs = "'$qsEntries'";
    return '''
(async () => {
  const section      = $sectionJs;
  const endpointPath = $endpointJs;
  const qs           = $qsJs;
  const t0 = Date.now();
  const attempts = [];

  function emit(payload) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler('clinicalList', payload);
    }
  }
  function fail(error, extra) {
    emit({ section, ok: false, error, attempts, timings: { totalMs: Date.now() - t0 }, ...(extra || {}) });
  }

  // Step 1 — ensure verification token. Cached on window across calls.
  const TOKEN_CACHE = '__binaStanfordRVT';
  let token = window[TOKEN_CACHE];
  if (!token) {
    const tT = Date.now();
    // Any /myhealth_sso/app/* shell page has the __CSRFContainer hidden input.
    const shellUrl = 'https://mychart.stanfordhealthcare.org/myhealth_sso/Clinical/Allergies/Index?lang=en-US';
    try {
      const shellResp = await fetch(shellUrl, { credentials: 'include', headers: { 'Accept': 'text/html' } });
      const shellHtml = await shellResp.text();
      attempts.push({ step: 'shell-fetch', status: shellResp.status, htmlLength: shellHtml.length, elapsedMs: Date.now() - tT });
      if (!shellResp.ok) return fail('shell-non-ok', { status: shellResp.status });
      const m = shellHtml.match(/name="__RequestVerificationToken"[^>]*value="([^"]+)"/);
      if (!m) return fail('token-not-found-in-shell');
      token = m[1];
      window[TOKEN_CACHE] = token;
    } catch (e) {
      return fail('shell-fetch-failed', { message: (e && e.message) || String(e) });
    }
  } else {
    attempts.push({ step: 'shell-cached', tokenLen: token.length });
  }

  // Step 2 — POST the section's LoadListData (or equivalent) endpoint.
  const tL = Date.now();
  const apiUrl = 'https://mychart.stanfordhealthcare.org/myhealth_sso/Clinical/' + endpointPath
    + (qs ? ('?' + qs) : '');
  let listResp;
  try {
    listResp = await fetch(apiUrl, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        '__RequestVerificationToken': token,
      },
      body: '',
    });
  } catch (e) {
    return fail('list-fetch-failed', { message: (e && e.message) || String(e) });
  }
  const listText = await listResp.text();
  attempts.push({ step: 'loadlist', url: apiUrl, status: listResp.status, ok: listResp.ok, respLength: listText.length, elapsedMs: Date.now() - tL });

  if (listResp.status === 401 || listResp.status === 403 || listResp.status === 419) {
    delete window[TOKEN_CACHE];
    return fail('list-auth-failed', { status: listResp.status });
  }
  if (!listResp.ok) return fail('list-non-ok', { status: listResp.status });

  let list;
  try {
    list = JSON.parse(listText);
  } catch (e) {
    return fail('list-json-parse-failed', { message: (e && e.message) || String(e), respPreview: listText.slice(0, 200) });
  }

  emit({ section, ok: true, status: listResp.status, list, attempts, timings: { totalMs: Date.now() - t0 } });
})();
''';
  }

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

  // ──────────────────────────────────────────────────────────────────────
  // PORTAL SCOUT — mobile equivalent of the Chrome+Tampermonkey recon
  // path. Same architecture (page-context fetch/XHR/sessionStorage hooks
  // with cross-frame postMessage bridging), expressed as Dart-injected JS
  // instead of a userscript. Once installed, the page exposes
  //   window.__portalScout.{start, stop, count, records, clear,
  //                          enumerateAllFrames}
  // and Dart drives the autonomous portal walk via evaluateJavascript.
  //
  // The canonical entry point is [bootstrapForUserScript] — register it as
  // an `initialUserScripts` UserScript with `forMainFrameOnly: false` and
  // `injectionTime: AT_DOCUMENT_START` so the bootstrap runs in every
  // frame (wrapper + cross-origin iframes) before page JS executes.
  // [installApiCapture] is kept as a fallback for explicit re-injection.
  //
  // Captures live in the top frame's localStorage (key 'portalscout.records')
  // so they survive same-origin navigation between sections.
  // ──────────────────────────────────────────────────────────────────────

  /// Self-contained bootstrap that runs in EVERY frame (top + cross-origin
  /// iframes) at document-start. Installs:
  ///   - fetch / XHR / Storage.setItem hooks (per-frame)
  ///   - postMessage-RPC enumeration handler (every frame answers requests)
  ///   - on the top frame only: the [window.__portalScout] surface, including
  ///     `enumerateAllFrames()` which broadcasts to all descendants and
  ///     aggregates responses.
  static String bootstrapForUserScript() => r'''
(() => {
  if (window.__binaPortalScoutBootstrapped) return;
  window.__binaPortalScoutBootstrapped = true;

  const LS_STATE   = 'portalscout.active';
  const LS_RECORDS = 'portalscout.records';
  const MSG_TAG    = '__portalScout__';
  const inTop      = (window.top === window);

  // ── Helpers ─────────────────────────────────────────────────────────
  const now      = () => new Date().toISOString();
  const isAsset  = (url) => /\.(css|js|png|jpg|jpeg|svg|gif|woff2?|ttf|ico|map)(\?|$)/i.test(url || '');
  const stringifyBody = (b) => {
    if (b == null) return null;
    if (typeof b === 'string') return b;
    if (b instanceof URLSearchParams) return b.toString();
    if (b instanceof FormData) {
      const o = {}; for (const [k, v] of b.entries()) o[k] = (typeof v === 'string') ? v : '[Blob]';
      return JSON.stringify(o);
    }
    if (b instanceof ArrayBuffer || ArrayBuffer.isView(b)) return '[binary ' + b.byteLength + 'b]';
    try { return JSON.stringify(b); } catch (e) { return '[unserializable]'; }
  };
  const headersToObject = (h) => {
    if (!h) return null;
    if (h instanceof Headers) { const o = {}; for (const [k, v] of h.entries()) o[k] = v; return o; }
    if (Array.isArray(h)) return Object.fromEntries(h);
    if (typeof h === 'object') return { ...h };
    return null;
  };
  const parseRespHeaders = (s) => {
    const o = {};
    (s || '').split('\r\n').forEach(line => {
      const i = line.indexOf(':');
      if (i > 0) o[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
    });
    return o;
  };

  // ── Capture state & store ──────────────────────────────────────────
  function appendLocal(rec) {
    try {
      const arr = JSON.parse(localStorage.getItem(LS_RECORDS) || '[]');
      arr.push(rec);
      localStorage.setItem(LS_RECORDS, JSON.stringify(arr));
    } catch (e) {}
  }
  const push = inTop
    ? appendLocal
    : (rec) => { try { window.top.postMessage({tag: MSG_TAG, type: 'rec', rec}, '*'); } catch (e) {} };

  let subActive = true; // fail-open in subframes (top still gates on receive)
  const active = inTop
    ? () => localStorage.getItem(LS_STATE) === '1'
    : () => subActive;

  // ── Top-frame: receive child records + state-requests; broadcast state ─
  function broadcastState(isActive) {
    try {
      for (let i = 0; i < window.frames.length; i++) {
        try { window.frames[i].postMessage({tag: MSG_TAG, type: 'state', active: isActive}, '*'); } catch (e) {}
      }
    } catch (e) {}
  }

  // ── Enumeration logic (runs locally in every frame) ────────────────
  function classify(path, text) {
    const t = ((text || '') + ' ' + (path || '')).toLowerCase();
    if (/\b(billing|payment|invoice|account|settings|preferences|profile|help|faq|support|logout|sign[- ]?out|privacy|terms|about|legal|advert|decline|manage[- ]access|learn[- ]more)\b/.test(t)) return 'skip';
    // Common meta-links surfaced inside Epic sections that aren't real
    // sub-sections — they go to the same page (or a static empty state)
    // and contribute zero new XHRs. Demote so BFS doesn't waste time.
    if (/^(view all|view instructions|see all|see details|no pcp|view more|show more|expand all)\b/i.test((text || '').trim())) return 'skip';
    if (/^view all \(\d+\)/i.test((text || '').trim())) return 'skip';
    if (/^see details and manage/i.test((text || '').trim())) return 'skip';
    if (/\.(pdf|jpg|jpeg|png|gif|css|js)(\?|$)/.test(path || '')) return 'skip';
    // Per-item drilldowns (specific provider, lab, msg, appointment) are
    // for the per-section item-sampling phase, not the section-level
    // scout. Identify them by per-item query parameters or by a
    // provider-name-shaped link text (Title Case + clinical suffix
    // with optional middle initial).
    if (/[?&](serid|ticketId|eorderid|msgId|csn|encType|orderId|reportId|noteId|docId|encounterId)=/i.test(path || '')) return 'item';
    if (/^[A-Z][a-z]+(?:\s+(?:[A-Z]\.?|[A-Z][a-z'.]+))+,?\s+(MD|DO|NP|PA|RN|RD|PT|OT|PharmD|PHARMD|MA|LCSW|PsyD|DDS|DPT)\.?$/.test(text || '')) return 'item';
    if (/^send (a )?message to /i.test(text || '')) return 'item';
    // Plural-tolerant clinical match — Stanford's top nav reads MESSAGES /
    // VISITS / PROCEDURES, and \bmessage\b fails on "messages" because
    // there's no word boundary between 'message' and 's'. The optional s?
    // before \b fixes it without false positives.
    if (/\b(record|result|message|appointment|visit|note|allergy|allergie|immuniz|vaccin|condition|problem|medication|medicine|med-?list|procedure|order|referral|questionnaire|history|reminder|goal|care[- ]?plan|care[- ]?team|advance[- ]?care|covid|lab|test|document|tracking|research|inbox|outbox|letter|chart|profile-?summary)s?\b/.test(t)) return 'clinical';
    return 'other';
  }
  function visibleText(el) {
    return (el.innerText || el.textContent || el.getAttribute('aria-label') || el.title || '')
      .trim().replace(/\s+/g, ' ').slice(0, 80);
  }
  function destinationHint(el) {
    for (const attr of ['href', 'data-href', 'data-route', 'data-link', 'data-url']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) return {url: v, src: attr};
    }
    const onclickAttr = el.getAttribute && el.getAttribute('onclick');
    if (onclickAttr) {
      const m = onclickAttr.match(/['"](\/[^'"\s]+|#\/?[^'"\s]+|https?:\/\/[^'"\s]+)['"]/);
      if (m) return {url: m[1], src: 'onclick-attr'};
    }
    const onclickFn = el.onclick;
    if (typeof onclickFn === 'function') {
      const src = onclickFn.toString();
      const m = src.match(/['"](\/[^'"\s]+|#\/?[^'"\s]+|https?:\/\/[^'"\s]+)['"]/);
      if (m) return {url: m[1], src: 'onclick-fn'};
    }
    for (const attr of ['routerLink', 'ng-href', 'ng-click', 'to']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) return {url: v, src: attr};
    }
    return null;
  }
  function makeAbsolute(url) {
    try { return new URL(url, location.href).href; } catch (e) { return null; }
  }
  function enumerateLocal() {
    const seen = new Set();
    const out  = [];
    const byKind = {};
    const selectors = [
      {sel: 'a[href]',                kind: 'a'},
      {sel: 'button',                  kind: 'button'},
      {sel: '[role="link"]',           kind: 'role-link'},
      {sel: '[role="menuitem"]',       kind: 'role-menuitem'},
      {sel: '[role="tab"]',            kind: 'role-tab'},
      {sel: '[role="button"]',         kind: 'role-button'},
      {sel: '[data-href]',             kind: 'data-href'},
      {sel: '[data-route]',            kind: 'data-route'},
      {sel: '[ng-click]',              kind: 'ng-click'},
      {sel: '[routerlink], [routerLink]', kind: 'router-link'},
    ];
    for (const {sel, kind} of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        const hint = destinationHint(el);
        const text = visibleText(el);
        const href = hint && hint.url ? makeAbsolute(hint.url) : null;
        if (href && !href.startsWith(location.origin) && !href.startsWith('#')) continue;
        const dedupKey = href || (kind + '|' + text);
        if (seen.has(dedupKey)) continue;
        if (!hint && !text) continue;
        seen.add(dedupKey);
        const path = href ? ((new URL(href)).pathname + (new URL(href)).hash) : '';
        const cls  = classify(path, text);
        out.push({
          href, text, path,
          classification: cls,
          elementKind: kind,
          hintSource: hint ? hint.src : null,
          frameUrl: location.href,
        });
        byKind[kind] = (byKind[kind] || 0) + 1;
      }
    }
    return {byKind, candidates: out};
  }

  // ── postMessage RPC across frames ──────────────────────────────────
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (!d || d.tag !== MSG_TAG) return;
    // Capture-record forwarding (subframes → top)
    if (d.type === 'rec' && d.rec && inTop && active()) appendLocal(d.rec);
    // Subframe state propagation
    if (d.type === 'state' && !inTop) subActive = !!d.active;
    // Subframe replies to top's enumeration request — handled below in the
    // enumerateAllFrames wait loop via dedicated listener on each call.
    if (d.type === 'state-request' && inTop) {
      try { e.source && e.source.postMessage({tag: MSG_TAG, type: 'state', active: active()}, '*'); } catch (er) {}
    }
    // Any frame answers enumeration requests with its local catalogue.
    if (d.type === 'scout-enum-request') {
      const result = enumerateLocal();
      try {
        e.source && e.source.postMessage({
          tag: MSG_TAG, type: 'scout-enum-response',
          reqId: d.reqId,
          frameUrl: location.href,
          byKind: result.byKind,
          candidates: result.candidates,
        }, '*');
      } catch (er) {}
    }
  });

  // Subframe — ask top for current capture state on install.
  if (!inTop) {
    try { window.top.postMessage({tag: MSG_TAG, type: 'state-request'}, '*'); } catch (e) {}
  }

  // ── Hook fetch ────────────────────────────────────────────────────
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url    = (typeof input === 'string') ? input : (input && input.url) || String(input);
    const method = (init && init.method) || (typeof input === 'object' && input.method) || 'GET';
    const reqBody    = (init && init.body) || null;
    const reqHeaders = headersToObject(init && init.headers) || {};
    const t0 = performance.now();
    const resp = await origFetch(input, init);
    const dt = performance.now() - t0;
    if (active() && !isAsset(url)) {
      const clone = resp.clone();
      let respBody = '';
      try { respBody = await clone.text(); } catch (e) { respBody = '[read err]'; }
      push({when: now(), kind: 'fetch', page: location.href, url, method,
            reqHeaders, reqBody: stringifyBody(reqBody),
            status: resp.status, respHeaders: headersToObject(resp.headers),
            respContentType: resp.headers.get('content-type') || '',
            respBody, durationMs: Math.round(dt)});
    }
    return resp;
  };

  // ── Hook XHR ───────────────────────────────────────────────────────
  const OrigXHR = window.XMLHttpRequest;
  function CapturedXHR() {
    const xhr = new OrigXHR();
    const meta = {reqHeaders: {}, reqBody: null, method: 'GET', url: '', t0: 0};
    const origOpen = xhr.open;
    xhr.open = function (m, u) { meta.method = m; meta.url = u; return origOpen.apply(xhr, arguments); };
    const origSet  = xhr.setRequestHeader;
    xhr.setRequestHeader = function (k, v) { meta.reqHeaders[k] = v; return origSet.apply(xhr, arguments); };
    const origSend = xhr.send;
    xhr.send = function (body) {
      meta.reqBody = body; meta.t0 = performance.now();
      if (active() && !isAsset(meta.url)) {
        xhr.addEventListener('loadend', () => {
          const rh = parseRespHeaders(xhr.getAllResponseHeaders());
          push({when: now(), kind: 'xhr', page: location.href, url: meta.url,
                method: meta.method, reqHeaders: meta.reqHeaders, reqBody: stringifyBody(meta.reqBody),
                status: xhr.status, respHeaders: rh,
                respContentType: rh['content-type'] || '',
                respBody: xhr.responseText, durationMs: Math.round(performance.now() - meta.t0)});
        });
      }
      return origSend.apply(xhr, arguments);
    };
    return xhr;
  }
  CapturedXHR.prototype = OrigXHR.prototype;
  Object.setPrototypeOf(CapturedXHR, OrigXHR);
  window.XMLHttpRequest = CapturedXHR;

  // ── Hook Storage.setItem ──────────────────────────────────────────
  const origSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (k, v) {
    if (active() && k !== LS_RECORDS && k !== LS_STATE) {
      const area = (this === sessionStorage ? 'session' : (this === localStorage ? 'local' : 'other'));
      push({when: now(), kind: 'storage', area, op: 'setItem', page: location.href,
            key: k, valueLen: (v || '').length, valuePreview: String(v).slice(0, 200)});
    }
    return origSetItem.apply(this, arguments);
  };

  // ── Top-frame: expose programmatic surface ─────────────────────────
  if (!inTop) {
    window.__portalScout = {installed: true, _subframe: true, _bootstrap: true};
    return;
  }
  window.__portalScout = {
    installed: true,
    _bootstrap: true,
    start: () => { localStorage.setItem(LS_RECORDS, '[]'); localStorage.setItem(LS_STATE, '1'); broadcastState(true);  return 'started'; },
    stop:  () => { localStorage.setItem(LS_STATE, '0'); broadcastState(false); return JSON.parse(localStorage.getItem(LS_RECORDS) || '[]').length; },
    count: () => JSON.parse(localStorage.getItem(LS_RECORDS) || '[]').length,
    records: () => JSON.parse(localStorage.getItem(LS_RECORDS) || '[]'),
    clear: () => { localStorage.setItem(LS_RECORDS, '[]'); return 'cleared'; },
    isActive: () => localStorage.getItem(LS_STATE) === '1',
    // Broadcast enumeration request to every descendant frame; aggregate the
    // responses that arrive within timeoutMs. Returns a JSON-serializable
    // {ok, frames, totalCandidates, byFrame, byKind, candidates} object.
    enumerateAllFrames: async (timeoutMs) => {
      timeoutMs = (typeof timeoutMs === 'number') ? timeoutMs : 1500;
      const reqId = 'enum-' + Math.random().toString(36).slice(2);
      const responses = [];
      function onMsg(e) {
        const d = e.data;
        if (!d || d.tag !== MSG_TAG || d.type !== 'scout-enum-response' || d.reqId !== reqId) return;
        responses.push(d);
      }
      window.addEventListener('message', onMsg);
      function broadcast(w) {
        try { w.postMessage({tag: MSG_TAG, type: 'scout-enum-request', reqId}, '*'); } catch (e) {}
        let n = 0; try { n = w.frames.length; } catch (e) {}
        for (let i = 0; i < n; i++) {
          try { broadcast(w.frames[i]); } catch (e) {}
        }
      }
      broadcast(window);
      await new Promise(r => setTimeout(r, timeoutMs));
      window.removeEventListener('message', onMsg);
      const byFrame = {};
      const byKind  = {};
      const all     = [];
      const seen    = new Set();
      for (const r of responses) {
        byFrame[r.frameUrl] = (r.candidates || []).length;
        for (const k in (r.byKind || {})) byKind[k] = (byKind[k] || 0) + r.byKind[k];
        for (const c of (r.candidates || [])) {
          const key = c.href || (c.elementKind + '|' + c.text);
          if (seen.has(key)) continue;
          seen.add(key);
          all.push(c);
        }
      }
      return {ok: true, frames: responses.length, totalCandidates: all.length, byFrame, byKind, candidates: all};
    },
  };
})();
''';

  /// Page-context capture hook — fetch, XHR, sessionStorage writes; subframes
  /// forward records to top via postMessage. Re-injection on the same page
  /// is a no-op (idempotent install).
  static String installApiCapture() => r'''
(() => {
  const LS_STATE   = 'portalscout.active';
  const LS_RECORDS = 'portalscout.records';
  const MSG_TAG    = '__portalScout__';

  function injectPage(fn, cfg) {
    const s = document.createElement('script');
    s.textContent = '(' + fn.toString() + ')(' + JSON.stringify(cfg) + ');';
    (document.head || document.documentElement).appendChild(s);
    s.remove();
  }

  function pageInstall(cfg) {
    if (window.__portalScout && window.__portalScout.installed) return;

    const LS_STATE   = cfg.lsState;
    const LS_RECORDS = cfg.lsRecords;
    const MSG_TAG    = cfg.msgTag;
    const inTop      = (window.top === window);

    function appendLocal(rec) {
      try {
        const arr = JSON.parse(localStorage.getItem(LS_RECORDS) || '[]');
        arr.push(rec);
        localStorage.setItem(LS_RECORDS, JSON.stringify(arr));
      } catch (e) {}
    }
    const push = inTop
      ? appendLocal
      : (rec) => { try { window.top.postMessage({tag: MSG_TAG, type: 'rec', rec}, '*'); } catch (e) {} };

    // Subframes default ACTIVE — they may install after a top-frame start();
    // top still gates incoming records on its own active() state.
    let subActive = true;
    const active = inTop
      ? () => localStorage.getItem(LS_STATE) === '1'
      : () => subActive;

    if (inTop) {
      window.addEventListener('message', (e) => {
        const d = e.data;
        if (!d || d.tag !== MSG_TAG) return;
        if (d.type === 'rec' && d.rec && active()) appendLocal(d.rec);
        if (d.type === 'state-request') {
          try { e.source && e.source.postMessage({tag: MSG_TAG, type: 'state', active: active()}, '*'); } catch (er) {}
        }
      });
    } else {
      window.addEventListener('message', (e) => {
        const d = e.data;
        if (!d || d.tag !== MSG_TAG) return;
        if (d.type === 'state') subActive = !!d.active;
      });
      try { window.top.postMessage({tag: MSG_TAG, type: 'state-request'}, '*'); } catch (e) {}
    }
    function broadcastState(isActive) {
      try {
        for (let i = 0; i < window.frames.length; i++) {
          try { window.frames[i].postMessage({tag: MSG_TAG, type: 'state', active: isActive}, '*'); } catch (e) {}
        }
      } catch (e) {}
    }

    const now      = () => new Date().toISOString();
    const isAsset  = (url) => /\.(css|js|png|jpg|jpeg|svg|gif|woff2?|ttf|ico|map)(\?|$)/i.test(url);
    const stringifyBody = (b) => {
      if (b == null) return null;
      if (typeof b === 'string') return b;
      if (b instanceof URLSearchParams) return b.toString();
      if (b instanceof FormData) {
        const o = {}; for (const [k, v] of b.entries()) o[k] = (typeof v === 'string') ? v : '[Blob]';
        return JSON.stringify(o);
      }
      if (b instanceof ArrayBuffer || ArrayBuffer.isView(b)) return '[binary ' + b.byteLength + 'b]';
      try { return JSON.stringify(b); } catch (e) { return '[unserializable]'; }
    };
    const headersToObject = (h) => {
      if (!h) return null;
      if (h instanceof Headers) { const o = {}; for (const [k, v] of h.entries()) o[k] = v; return o; }
      if (Array.isArray(h)) return Object.fromEntries(h);
      if (typeof h === 'object') return { ...h };
      return null;
    };
    const parseRespHeaders = (s) => {
      const o = {};
      (s || '').split('\r\n').forEach(line => {
        const i = line.indexOf(':');
        if (i > 0) o[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
      });
      return o;
    };

    const origFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
      const url    = (typeof input === 'string') ? input : (input && input.url) || String(input);
      const method = (init && init.method) || (typeof input === 'object' && input.method) || 'GET';
      const reqBody    = (init && init.body) || null;
      const reqHeaders = headersToObject(init && init.headers) || {};
      const t0 = performance.now();
      const resp = await origFetch(input, init);
      const dt = performance.now() - t0;
      if (active() && !isAsset(url)) {
        const clone = resp.clone();
        let respBody = '';
        try { respBody = await clone.text(); } catch (e) { respBody = '[read err]'; }
        push({when: now(), kind: 'fetch', page: location.href, url, method,
              reqHeaders, reqBody: stringifyBody(reqBody),
              status: resp.status, respHeaders: headersToObject(resp.headers),
              respContentType: resp.headers.get('content-type') || '',
              respBody, durationMs: Math.round(dt)});
      }
      return resp;
    };

    const OrigXHR = window.XMLHttpRequest;
    function CapturedXHR() {
      const xhr = new OrigXHR();
      const meta = {reqHeaders: {}, reqBody: null, method: 'GET', url: '', t0: 0};
      const origOpen = xhr.open;
      xhr.open = function (m, u) { meta.method = m; meta.url = u; return origOpen.apply(xhr, arguments); };
      const origSet  = xhr.setRequestHeader;
      xhr.setRequestHeader = function (k, v) { meta.reqHeaders[k] = v; return origSet.apply(xhr, arguments); };
      const origSend = xhr.send;
      xhr.send = function (body) {
        meta.reqBody = body; meta.t0 = performance.now();
        if (active() && !isAsset(meta.url)) {
          xhr.addEventListener('loadend', () => {
            const rh = parseRespHeaders(xhr.getAllResponseHeaders());
            push({when: now(), kind: 'xhr', page: location.href, url: meta.url,
                  method: meta.method, reqHeaders: meta.reqHeaders, reqBody: stringifyBody(meta.reqBody),
                  status: xhr.status, respHeaders: rh,
                  respContentType: rh['content-type'] || '',
                  respBody: xhr.responseText, durationMs: Math.round(performance.now() - meta.t0)});
          });
        }
        return origSend.apply(xhr, arguments);
      };
      return xhr;
    }
    CapturedXHR.prototype = OrigXHR.prototype;
    Object.setPrototypeOf(CapturedXHR, OrigXHR);
    window.XMLHttpRequest = CapturedXHR;

    const origSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      if (active() && k !== LS_RECORDS && k !== LS_STATE) {
        const area = (this === sessionStorage ? 'session' : (this === localStorage ? 'local' : 'other'));
        push({when: now(), kind: 'storage', area, op: 'setItem', page: location.href,
              key: k, valueLen: (v || '').length, valuePreview: String(v).slice(0, 200)});
      }
      return origSetItem.apply(this, arguments);
    };

    if (!inTop) { window.__portalScout = {installed: true, _subframe: true}; return; }
    window.__portalScout = {
      installed: true,
      start: () => { localStorage.setItem(LS_RECORDS, '[]'); localStorage.setItem(LS_STATE, '1'); broadcastState(true);  return 'started'; },
      stop:  () => { localStorage.setItem(LS_STATE, '0'); broadcastState(false); return JSON.parse(localStorage.getItem(LS_RECORDS) || '[]').length; },
      count: () => JSON.parse(localStorage.getItem(LS_RECORDS) || '[]').length,
      records: () => JSON.parse(localStorage.getItem(LS_RECORDS) || '[]'),
      clear: () => { localStorage.setItem(LS_RECORDS, '[]'); return 'cleared'; },
      isActive: () => localStorage.getItem(LS_STATE) === '1',
    };
  }

  injectPage(pageInstall, {lsState: LS_STATE, lsRecords: LS_RECORDS, msgTag: MSG_TAG});
  return 'installed';
})();
''';

  /// Enumerate clickable elements from the current page and classify each
  /// as clinical-data | home | other | skip. Handles SPAs that don't use
  /// real <a href> tags — also walks <button>, [role=link], [role=menuitem],
  /// and [tabindex] elements, mining destination hints from click handler
  /// source where available (e.g. `location.href = '/foo'`, `router.push('/foo')`,
  /// data-href attributes, hash routes).
  ///
  /// Returns JSON-as-string with shape:
  ///   {ok, total, byKind: {a,button,...}, links: [{href|null, text, path,
  ///     classification, elementKind, hintSource}]}
  /// Entries with href=null are click-only targets that the driver has to
  /// dispatch via DOM .click() rather than direct navigation.
  static String scoutEnumerateLinks() => r'''
(() => {
  function classify(path, text) {
    const t = ((text || '') + ' ' + (path || '')).toLowerCase();
    if (/\b(billing|payment|invoice|account|settings|preferences|profile|help|faq|support|logout|sign[- ]?out|privacy|terms|about|legal|advert|decline|manage[- ]access|learn[- ]more)\b/.test(t)) return 'skip';
    // Common meta-links surfaced inside Epic sections that aren't real
    // sub-sections — they go to the same page (or a static empty state)
    // and contribute zero new XHRs. Demote so BFS doesn't waste time.
    if (/^(view all|view instructions|see all|see details|no pcp|view more|show more|expand all)\b/i.test((text || '').trim())) return 'skip';
    if (/^view all \(\d+\)/i.test((text || '').trim())) return 'skip';
    if (/^see details and manage/i.test((text || '').trim())) return 'skip';
    if (/\.(pdf|jpg|jpeg|png|gif|css|js)(\?|$)/.test(path || '')) return 'skip';
    // Per-item drilldowns (specific provider, lab, msg, appointment) are
    // for the per-section item-sampling phase, not the section-level
    // scout. Identify them by per-item query parameters or by a
    // provider-name-shaped link text (Title Case + clinical suffix
    // with optional middle initial).
    if (/[?&](serid|ticketId|eorderid|msgId|csn|encType|orderId|reportId|noteId|docId|encounterId)=/i.test(path || '')) return 'item';
    if (/^[A-Z][a-z]+(?:\s+(?:[A-Z]\.?|[A-Z][a-z'.]+))+,?\s+(MD|DO|NP|PA|RN|RD|PT|OT|PharmD|PHARMD|MA|LCSW|PsyD|DDS|DPT)\.?$/.test(text || '')) return 'item';
    if (/^send (a )?message to /i.test(text || '')) return 'item';
    // Plural-tolerant clinical match — Stanford's top nav reads MESSAGES /
    // VISITS / PROCEDURES, and \bmessage\b fails on "messages" because
    // there's no word boundary between 'message' and 's'. The optional s?
    // before \b fixes it without false positives.
    if (/\b(record|result|message|appointment|visit|note|allergy|allergie|immuniz|vaccin|condition|problem|medication|medicine|med-?list|procedure|order|referral|questionnaire|history|reminder|goal|care[- ]?plan|care[- ]?team|advance[- ]?care|covid|lab|test|document|tracking|research|inbox|outbox|letter|chart|profile-?summary)s?\b/.test(t)) return 'clinical';
    return 'other';
  }
  function visibleText(el) {
    return (el.innerText || el.textContent || el.getAttribute('aria-label') || el.title || '')
      .trim().replace(/\s+/g, ' ').slice(0, 80);
  }
  function destinationHint(el) {
    // Inline href / data-href / data-route / data-link
    for (const attr of ['href', 'data-href', 'data-route', 'data-link', 'data-url']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) return {url: v, src: attr};
    }
    // onclick attribute (raw HTML attribute)
    const onclickAttr = el.getAttribute && el.getAttribute('onclick');
    if (onclickAttr) {
      const m = onclickAttr.match(/['"](\/[^'"\s]+|#\/?[^'"\s]+|https?:\/\/[^'"\s]+)['"]/);
      if (m) return {url: m[1], src: 'onclick-attr'};
    }
    // onclick property (JS-assigned)
    const onclickFn = el.onclick;
    if (typeof onclickFn === 'function') {
      const src = onclickFn.toString();
      const m = src.match(/['"](\/[^'"\s]+|#\/?[^'"\s]+|https?:\/\/[^'"\s]+)['"]/);
      if (m) return {url: m[1], src: 'onclick-fn'};
    }
    // Angular/React routerLink attribute
    for (const attr of ['routerLink', 'ng-href', 'ng-click', 'to']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) return {url: v, src: attr};
    }
    return null;
  }
  function makeAbsolute(url) {
    try { return new URL(url, location.href).href; } catch (e) { return null; }
  }

  const seen = new Set();
  const out  = [];
  const byKind = {};
  const selectors = [
    {sel: 'a[href]',                kind: 'a'},
    {sel: 'button',                  kind: 'button'},
    {sel: '[role="link"]',           kind: 'role-link'},
    {sel: '[role="menuitem"]',       kind: 'role-menuitem'},
    {sel: '[role="tab"]',            kind: 'role-tab'},
    {sel: '[role="button"]',         kind: 'role-button'},
    {sel: '[data-href]',             kind: 'data-href'},
    {sel: '[data-route]',            kind: 'data-route'},
    {sel: '[ng-click]',              kind: 'ng-click'},
    {sel: '[routerlink], [routerLink]', kind: 'router-link'},
  ];
  for (const {sel, kind} of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      const hint = destinationHint(el);
      const text = visibleText(el);
      const href = hint && hint.url ? makeAbsolute(hint.url) : null;
      // Same-origin only (or relative-resolved within origin). External skipped.
      if (href && !href.startsWith(location.origin) && !href.startsWith('#')) continue;
      // Dedup by href when we have one; otherwise by (kind, text) — text alone
      // is too loose. Always include if we have a hint.
      const dedupKey = href || (kind + '|' + text);
      if (seen.has(dedupKey)) continue;
      // Tab/button without any destination hint AND empty text: useless — skip.
      if (!hint && !text) continue;
      seen.add(dedupKey);
      const path = href ? (new URL(href)).pathname + (new URL(href)).hash : '';
      const cls  = classify(path, text);
      out.push({
        href, text, path,
        classification: cls,
        elementKind: kind,
        hintSource: hint ? hint.src : null,
      });
      byKind[kind] = (byKind[kind] || 0) + 1;
    }
  }
  return JSON.stringify({ok: true, total: out.length, byKind, links: out});
})();
''';

  /// Snapshot the currently-loaded page — URL, title, dominant heading, and
  /// the row-like structural patterns. Used per-section during the scout.
  static String scoutSnapshotCurrent() => r'''
(() => {
  function rowPatterns() {
    const out = [];
    for (const t of document.querySelectorAll('table')) {
      const rows = (t.tBodies && t.tBodies[0] && t.tBodies[0].rows.length) || 0;
      if (rows > 1) {
        const headers = Array.from(t.querySelectorAll('thead th')).map(th => th.innerText.trim().slice(0, 30));
        out.push({type: 'table', rowCount: rows, headers});
      }
    }
    for (const u of document.querySelectorAll('ul, ol')) {
      const items = u.querySelectorAll(':scope > li');
      if (items.length > 2) out.push({type: u.tagName.toLowerCase(), itemCount: items.length});
    }
    const cards = document.querySelectorAll('[class*="card"], [class*="row"], [class*="result"], [data-testid*="row"], [data-testid*="card"]');
    if (cards.length > 2) out.push({type: 'cards', count: cards.length});
    return out;
  }
  return JSON.stringify({
    url:         location.href,
    title:       document.title,
    pageHeading: (document.querySelector('h1, h2') && document.querySelector('h1, h2').innerText || '').trim().slice(0, 80),
    rowPatterns: rowPatterns(),
    bodyTextHead: (document.body && document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 400),
  });
})();
''';

  /// Pull the top frame's captured records out and (optionally) clear the
  /// store so the next section gets a fresh slice.
  static String scoutGetCaptures({bool clear = false}) => '''
(() => {
  if (!window.__portalScout) return '[]';
  const recs = window.__portalScout.records();
  ${clear ? 'window.__portalScout.clear();' : ''}
  return JSON.stringify(recs);
})();
''';
}
