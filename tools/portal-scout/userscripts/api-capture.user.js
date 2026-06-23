// ==UserScript==
// @name         Portal API capture (Bina Health)
// @namespace    https://github.com/usarid/BinaHealth
// @version      0.2.0
// @description  Recon helper: intercepts every fetch / XHR on portal pages and
//               records URL, method, headers, request body, response body, plus
//               sessionStorage writes. Two control surfaces:
//                 - Tampermonkey menu (manual): Start / Stop / Download / Clear
//                 - Programmatic (window.__portalScout): so the Chrome MCP can
//                   drive the whole recon end-to-end with one javascript_tool call.
//               Captures stay local; the AI is not in the loop at capture time.
// @match        https://myhealth.stanfordhealthcare.org/*
// @match        https://mychart.stanfordhealthcare.org/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  // ── Page-context injection ──────────────────────────────────────────
  // Tampermonkey runs userscripts in an isolated sandbox: window.fetch in
  // the userscript context is NOT the same as window.fetch in the page.
  // To intercept real traffic we must install hooks in the PAGE context.
  // The injected IIFE writes captures to localStorage under a known key;
  // the userscript reads them and re-exposes via the menu.

  const LS_STATE   = 'portalscout.active';
  const LS_RECORDS = 'portalscout.records';     // JSON array of capture records

  function injectPageScript(fn, args = {}) {
    const s = document.createElement('script');
    s.textContent = '(' + fn.toString() + ')(' + JSON.stringify(args) + ');';
    (document.head || document.documentElement).appendChild(s);
    s.remove();
  }

  // The function below runs in PAGE context. It must be self-contained (no
  // closure over userscript-scope variables). Capture toggle and storage
  // both go through page-context localStorage, so the page can read/write.
  function pageInstall(cfg) {
    const LS_STATE   = cfg.lsState;
    const LS_RECORDS = cfg.lsRecords;

    if (window.__portalScout && window.__portalScout.installed) return;

    const push = (rec) => {
      try {
        const arr = JSON.parse(localStorage.getItem(LS_RECORDS) || '[]');
        arr.push(rec);
        localStorage.setItem(LS_RECORDS, JSON.stringify(arr));
      } catch (e) { /* swallow — quota etc. */ }
    };
    const active = () => localStorage.getItem(LS_STATE) === '1';
    const now    = () => new Date().toISOString();
    const isAsset = (url) =>
      /\.(css|js|png|jpg|jpeg|svg|gif|woff2?|ttf|ico|map)(\?|$)/i.test(url);

    const stringifyBody = (b) => {
      if (b == null) return null;
      if (typeof b === 'string') return b;
      if (b instanceof URLSearchParams) return b.toString();
      if (b instanceof FormData) {
        const o = {}; for (const [k, v] of b.entries()) o[k] = (typeof v === 'string') ? v : '[Blob]';
        return JSON.stringify(o);
      }
      if (b instanceof ArrayBuffer || ArrayBuffer.isView(b)) return `[binary ${b.byteLength}b]`;
      try { return JSON.stringify(b); } catch (e) { return `[unserializable: ${e.message}]`; }
    };
    const headersToObject = (h) => {
      if (!h) return null;
      if (h instanceof Headers) { const o = {}; for (const [k, v] of h.entries()) o[k] = v; return o; }
      if (Array.isArray(h)) return Object.fromEntries(h);
      if (typeof h === 'object') return { ...h };
      return null;
    };
    const parseAllRespHeaders = (s) => {
      const o = {};
      (s || '').split('\r\n').forEach(line => {
        const i = line.indexOf(':');
        if (i > 0) o[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
      });
      return o;
    };

    // ── fetch hook ────────────────────────────────────────────────────
    const origFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
      const url    = (typeof input === 'string') ? input : (input?.url || String(input));
      const method = (init?.method) || (typeof input === 'object' && input?.method) || 'GET';
      const reqBody    = init?.body ?? null;
      const reqHeaders = headersToObject(init?.headers) || {};
      const t0 = performance.now();
      const resp = await origFetch(input, init);
      const dt = performance.now() - t0;
      if (active() && !isAsset(url)) {
        const clone = resp.clone();
        let respBody = '';
        try { respBody = await clone.text(); } catch (e) { respBody = `[read err: ${e.message}]`; }
        push({
          when: now(), kind: 'fetch', page: location.href, url,
          method, reqHeaders, reqBody: stringifyBody(reqBody),
          status: resp.status,
          respHeaders: headersToObject(resp.headers),
          respContentType: resp.headers.get('content-type') || '',
          respBody, durationMs: Math.round(dt),
        });
      }
      return resp;
    };

    // ── XHR hook ──────────────────────────────────────────────────────
    const OrigXHR = window.XMLHttpRequest;
    function CapturedXHR() {
      const xhr = new OrigXHR();
      const meta = { reqHeaders: {}, reqBody: null, method: 'GET', url: '', t0: 0 };
      const origOpen = xhr.open;
      xhr.open = function (m, u) { meta.method = m; meta.url = u; return origOpen.apply(xhr, arguments); };
      const origSet = xhr.setRequestHeader;
      xhr.setRequestHeader = function (k, v) { meta.reqHeaders[k] = v; return origSet.apply(xhr, arguments); };
      const origSend = xhr.send;
      xhr.send = function (body) {
        meta.reqBody = body; meta.t0 = performance.now();
        if (active() && !isAsset(meta.url)) {
          xhr.addEventListener('loadend', () => {
            const rh = parseAllRespHeaders(xhr.getAllResponseHeaders());
            push({
              when: now(), kind: 'xhr', page: location.href, url: meta.url,
              method: meta.method, reqHeaders: meta.reqHeaders, reqBody: stringifyBody(meta.reqBody),
              status: xhr.status, respHeaders: rh,
              respContentType: rh['content-type'] || '',
              respBody: xhr.responseText, durationMs: Math.round(performance.now() - meta.t0),
            });
          });
        }
        return origSend.apply(xhr, arguments);
      };
      return xhr;
    }
    CapturedXHR.prototype = OrigXHR.prototype;
    Object.setPrototypeOf(CapturedXHR, OrigXHR);
    window.XMLHttpRequest = CapturedXHR;

    // ── Storage hook (setItem on prototype catches the explicit call form) ─
    const origSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      if (active() && k !== LS_RECORDS && k !== LS_STATE) {
        const area = (this === sessionStorage ? 'session' : (this === localStorage ? 'local' : 'other'));
        push({ when: now(), kind: 'storage', area, op: 'setItem', page: location.href,
               key: k, valueLen: (v || '').length, valuePreview: String(v).slice(0, 200) });
      }
      return origSetItem.apply(this, arguments);
    };
    // Direct property assignment (sessionStorage.foo = bar) bypasses setItem;
    // catch via periodic delta-snapshot while active.
    let lastSnap = {};
    setInterval(() => {
      if (!active()) return;
      const cur = {};
      for (let i = 0; i < sessionStorage.length; i++) {
        const k = sessionStorage.key(i);
        cur[k] = sessionStorage.getItem(k);
      }
      for (const [k, v] of Object.entries(cur)) {
        if (lastSnap[k] !== v) {
          push({ when: now(), kind: 'storage', area: 'session', op: 'snapshot-delta',
                 page: location.href, key: k, valueLen: (v || '').length,
                 valuePreview: String(v).slice(0, 200) });
        }
      }
      lastSnap = cur;
    }, 500);

    // ── Programmatic surface — drivable from Chrome MCP javascript_tool ─
    window.__portalScout = {
      installed: true,
      start: () => { localStorage.setItem(LS_RECORDS, '[]'); localStorage.setItem(LS_STATE, '1'); return 'started'; },
      stop:  () => { localStorage.setItem(LS_STATE, '0'); return JSON.parse(localStorage.getItem(LS_RECORDS) || '[]').length; },
      count: () => JSON.parse(localStorage.getItem(LS_RECORDS) || '[]').length,
      isActive: () => localStorage.getItem(LS_STATE) === '1',
      records:  () => JSON.parse(localStorage.getItem(LS_RECORDS) || '[]'),
      // Trigger a JSON download to the user's Downloads folder.
      download: (filename) => {
        const all = JSON.parse(localStorage.getItem(LS_RECORDS) || '[]');
        const blob = new Blob([JSON.stringify(all, null, 2)], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename || `portal-captures-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
        return a.download;
      },
      clear: () => { localStorage.removeItem(LS_RECORDS); localStorage.removeItem(LS_STATE); return 'cleared'; },
    };
  }

  injectPageScript(pageInstall, { lsState: LS_STATE, lsRecords: LS_RECORDS });

  // ── Tampermonkey menu commands (manual control surface) ─────────────
  // Bridge into page-context window.__portalScout via location-level read.
  function pageRecordCount() {
    try { return JSON.parse(localStorage.getItem(LS_RECORDS) || '[]').length; } catch { return 0; }
  }
  GM_registerMenuCommand('▶ Start API capture', () => {
    localStorage.setItem(LS_RECORDS, '[]');
    localStorage.setItem(LS_STATE, '1');
    alert('Capture started. Drive the UI, then click "Stop API capture".');
  });
  GM_registerMenuCommand('■ Stop API capture',  () => {
    localStorage.setItem(LS_STATE, '0');
    alert(`Capture stopped. ${pageRecordCount()} records captured.`);
  });
  GM_registerMenuCommand('⤓ Download captures', () => {
    // Invoke the page-context download via a one-shot script injection.
    injectPageScript(function () { window.__portalScout && window.__portalScout.download(); });
  });
  GM_registerMenuCommand('✖ Clear captures', () => {
    localStorage.removeItem(LS_RECORDS);
    localStorage.removeItem(LS_STATE);
    alert('Captures cleared.');
  });
})();
