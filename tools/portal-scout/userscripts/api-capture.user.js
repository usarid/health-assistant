// ==UserScript==
// @name         Portal API capture (Bina Health)
// @namespace    https://github.com/usarid/BinaHealth
// @version      0.1.0
// @description  Recon helper: intercepts every fetch / XHR on portal pages and
//               records URL, method, headers, request body, response body, plus
//               every sessionStorage write. Toggle on, drive the UI, toggle off,
//               click "Download captures" to save a JSON file to your Downloads
//               folder for offline analysis. Captures stay local; the AI is not
//               in the loop at capture time.
// @match        https://myhealth.stanfordhealthcare.org/*
// @match        https://mychart.stanfordhealthcare.org/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  const STATE_KEY = 'portalscout.capture-active';
  const STORE_KEY = 'portalscout.captures';

  function active()      { return GM_getValue(STATE_KEY, false); }
  function setActive(v)  { GM_setValue(STATE_KEY, !!v); }
  function getCaptures() { return GM_getValue(STORE_KEY, []); }
  function setCaptures(a){ GM_setValue(STORE_KEY, a); }
  function pushCapture(rec) {
    const all = getCaptures();
    all.push(rec);
    setCaptures(all);
  }
  function now() { return new Date().toISOString(); }

  // ── Install interceptors at document-start, before page JS runs ────
  // (They self-gate on active(); when inactive, they're zero-cost passthroughs.)

  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = (typeof input === 'string') ? input : (input?.url || String(input));
    const method = (init?.method) || (typeof input === 'object' && input?.method) || 'GET';
    const reqBody = init?.body ?? null;
    const reqHeaders = headersToObject(init?.headers) || {};
    const t0 = performance.now();
    const resp = await origFetch.apply(this, arguments);
    const dt = performance.now() - t0;
    if (active() && shouldCapture(url)) {
      const clone = resp.clone();
      let respBody = null, respCT = clone.headers.get('content-type') || '';
      try {
        respBody = await clone.text();
      } catch (e) { respBody = `[error reading body: ${e.message}]`; }
      pushCapture({
        when: now(), kind: 'fetch', page: location.href, url,
        method, reqHeaders, reqBody: stringifyBody(reqBody),
        status: resp.status, respHeaders: headersToObject(resp.headers),
        respContentType: respCT, respBody, durationMs: Math.round(dt),
      });
    }
    return resp;
  };

  const OrigXHR = window.XMLHttpRequest;
  function CapturedXHR() {
    const xhr = new OrigXHR();
    const meta = { reqHeaders: {}, reqBody: null, method: 'GET', url: '', t0: 0 };
    const origOpen = xhr.open;
    xhr.open = function (method, url) {
      meta.method = method; meta.url = url;
      return origOpen.apply(xhr, arguments);
    };
    const origSetHdr = xhr.setRequestHeader;
    xhr.setRequestHeader = function (k, v) {
      meta.reqHeaders[k] = v;
      return origSetHdr.apply(xhr, arguments);
    };
    const origSend = xhr.send;
    xhr.send = function (body) {
      meta.reqBody = body; meta.t0 = performance.now();
      if (active() && shouldCapture(meta.url)) {
        xhr.addEventListener('loadend', () => {
          const respHeaders = parseAllResponseHeaders(xhr.getAllResponseHeaders());
          pushCapture({
            when: now(), kind: 'xhr', page: location.href, url: meta.url,
            method: meta.method, reqHeaders: meta.reqHeaders, reqBody: stringifyBody(meta.reqBody),
            status: xhr.status, respHeaders, respContentType: respHeaders['content-type'] || '',
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

  // sessionStorage writes — both setItem AND direct property assignment.
  // Wrap setItem on the prototype:
  const origSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (k, v) {
    if (active()) {
      const area = (this === sessionStorage ? 'session' : (this === localStorage ? 'local' : 'other'));
      pushCapture({ when: now(), kind: 'storage', area, op: 'setItem', page: location.href, key: k, valueLen: (v||'').length, valuePreview: String(v).slice(0, 200) });
    }
    return origSetItem.apply(this, arguments);
  };
  // For direct property assignment, we can't proxy sessionStorage itself, but we
  // can periodically snapshot its keys+values while capture is active.
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
        pushCapture({ when: now(), kind: 'storage', area: 'session', op: 'snapshot-delta', page: location.href, key: k, valueLen: (v||'').length, valuePreview: String(v).slice(0, 200) });
      }
    }
    lastSnap = cur;
  }, 500);

  // ── Helpers ───────────────────────────────────────────────────────
  function shouldCapture(url) {
    // Skip static assets; we only care about XHR/Fetch APIs.
    return !/\.(css|js|png|jpg|jpeg|svg|gif|woff2?|ttf|ico|map)(\?|$)/i.test(url);
  }
  function headersToObject(h) {
    if (!h) return null;
    if (h instanceof Headers) {
      const o = {}; for (const [k, v] of h.entries()) o[k] = v; return o;
    }
    if (Array.isArray(h)) return Object.fromEntries(h);
    if (typeof h === 'object') return { ...h };
    return null;
  }
  function parseAllResponseHeaders(s) {
    const o = {};
    (s || '').split('\r\n').forEach(line => {
      const i = line.indexOf(':');
      if (i > 0) o[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
    });
    return o;
  }
  function stringifyBody(b) {
    if (b == null) return null;
    if (typeof b === 'string') return b;
    if (b instanceof URLSearchParams) return b.toString();
    if (b instanceof FormData) {
      const o = {}; for (const [k, v] of b.entries()) o[k] = (typeof v === 'string') ? v : '[Blob]'; return JSON.stringify(o);
    }
    if (b instanceof ArrayBuffer || ArrayBuffer.isView(b)) return `[binary ${b.byteLength}b]`;
    try { return JSON.stringify(b); } catch (e) { return `[unserializable: ${e.message}]`; }
  }

  // ── Menu commands ─────────────────────────────────────────────────
  GM_registerMenuCommand('▶ Start API capture', () => {
    setCaptures([]);
    setActive(true);
    alert('Capture started. Drive the UI, then click "Stop API capture".');
  });
  GM_registerMenuCommand('■ Stop API capture',  () => {
    setActive(false);
    const n = getCaptures().length;
    alert(`Capture stopped. ${n} records captured.`);
  });
  GM_registerMenuCommand('⤓ Download captures', () => {
    const all = getCaptures();
    const blob = new Blob([JSON.stringify(all, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `portal-captures-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
  });
  GM_registerMenuCommand('✖ Clear captures', () => {
    setCaptures([]); setActive(false);
    alert('Captures cleared.');
  });
})();
