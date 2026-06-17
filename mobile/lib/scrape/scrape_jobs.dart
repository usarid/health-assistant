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
    if (cursor) {
      // Stanford returns `currentPageBeginMessageId` + `nextPageBeginMessageId`
      // in the response. The request field that ADVANCES the cursor is
      // unknown — try the most plausible. Success requires the response
      // cursor to differ from the input cursor (handled below).
      return [
        { currentPageBeginMessageId: cursor },
        { beginMessageId: cursor },
        { nextPageBeginMessageId: cursor },
        { pageBeginMessageId: cursor },
        { startMessageId: cursor },
      ];
    }
    if (folder === 'inbox') {
      return [
        {},
        { folder: 'Inbox' },
        { folder: 'inbox' },
        { folderName: 'Inbox' },
        { folderType: 'inbox' },
        { mailboxFolder: 'Inbox' },
        { mailboxFolderId: 1 },
        { mailboxFolderId: 0 },
        { view: 'Inbox' },
        { includeRead: true, includeUnread: true },
        { archived: false, deleted: false },
      ];
    }
    return [{}];
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
      // Try common field names — Epic uses MixedCase
      const id = m.Id || m.ID || m.MessageId || m.MessageID || m.id || '';
      const subject = m.Subject || m.subject || m.Title || m.title || '';
      const otherParty = m.From || m.from || m.Sender || m.sender
                      || m.To || m.to || m.Recipient || m.recipient
                      || (m.Provider && (m.Provider.Name || m.Provider.DisplayName))
                      || '';
      const date = m.Date || m.SentDate || m.ReceivedDate
                || m.sent || m.received || m.timestamp || '';
      const isUnread = !!(m.IsUnread || m.Unread || m.unread || m.isUnread);
      const isReply = /^re\\b/i.test(String(subject));
      return {
        folder,
        id: String(id),
        subject: String(subject),
        otherParty: typeof otherParty === 'string' ? otherParty : JSON.stringify(otherParty),
        date: String(date),
        isUnread,
        isReply,
      };
    }).filter(r => r.id);  // drop entries where we couldn't find an ID
  }

  // Timing breakdown for the whole script — emitted in the final
  // diag so we can pinpoint where each ms goes per page.
  const timings = {};
  function mark(label, ms) { timings[label] = ms; }

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
