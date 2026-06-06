/**
 * v3 portal-discovery helper — paste in DevTools console while logged into
 * a new Epic-based MyChart portal (e.g. Stanford MyHealth) to capture the
 * portal-specific values needed for a v3 config.
 *
 * Usage:
 *   1. Log into the portal in your browser.
 *   2. Open DevTools console.
 *   3. Paste this entire file.
 *   4. Navigate to Visits and expand at least one visit with a clinical note
 *      (so the relevant API calls fire).
 *   5. Run `discoverPortal()` in the console.
 *   6. Copy the JSON output and paste it back to the assistant.
 *
 * What this captures:
 *   - The Epic SPA component instances and the size of each RenderedData array
 *     (so we can identify which instance holds the visits list).
 *   - Every JSON API call that fired while this script was active, with
 *     method / path / sample request body / sample response shape.
 *   - The portal's anti-forgery token presence + the URL base path.
 *
 * What this does NOT do:
 *   - Send any data anywhere. Everything stays in your browser. The output is
 *     a string for you to copy at your discretion.
 */

(function (global) {
  'use strict';

  if (global.__v3_discover_active) {
    console.log('[v3 discover] already running. Run discoverPortal() to finish.');
    return;
  }
  global.__v3_discover_active = true;
  const captured = [];

  // ── Hook fetch ────────────────────────────────────────────────────────
  const origFetch = global.fetch;
  global.fetch = async function (...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    const opts = args[1] || {};
    const startedAt = new Date().toISOString();
    let respJson = null;
    let respStatus = null;
    try {
      const resp = await origFetch.apply(this, args);
      respStatus = resp.status;
      const ct = resp.headers.get('content-type') || '';
      if (ct.includes('json')) {
        try { respJson = await resp.clone().json(); } catch (_) {}
      }
      return resp;
    } catch (e) {
      throw e;
    } finally {
      // Best-effort capture — only record JSON-likely calls
      if (url.includes('/api/') || url.includes('/MyChart/') || url.includes('/MyHealth/')) {
        let bodyParsed = null;
        try {
          if (opts.body) bodyParsed = JSON.parse(opts.body);
        } catch (_) {}
        captured.push({
          startedAt,
          method: (opts.method || 'GET').toUpperCase(),
          url,
          requestBody: bodyParsed,
          responseStatus: respStatus,
          // Trim the response shape — we only want the structure, not all the content
          responseShape: shapeOf(respJson, 3),
        });
      }
    }
  };

  function shapeOf(v, depth) {
    if (depth < 0 || v == null) return typeof v;
    if (Array.isArray(v)) {
      return v.length === 0
        ? 'array[]'
        : `array[${v.length}] of ${shapeOf(v[0], depth - 1)}`;
    }
    if (typeof v === 'object') {
      const keys = Object.keys(v).slice(0, 12);
      const inner = {};
      for (const k of keys) inner[k] = shapeOf(v[k], depth - 1);
      if (Object.keys(v).length > 12) inner['…'] = `(${Object.keys(v).length - 12} more)`;
      return inner;
    }
    if (typeof v === 'string') return v.length > 60 ? 'string(long)' : 'string';
    return typeof v;
  }

  // ── Probe Epic SPA instances ──────────────────────────────────────────
  function probeInstances() {
    const root = global.Epic
      && global.Epic.PatientAccess
      && global.Epic.PatientAccess.Components
      && global.Epic.PatientAccess.Components.__Instances;
    if (!root) return { error: 'Epic.PatientAccess.Components.__Instances not found' };
    const out = [];
    for (let i = 0; i < root.length; i++) {
      const inst = root[i];
      if (!inst) continue;
      const ctorName = inst.constructor && inst.constructor.name;
      const rd = inst.RenderedData;
      let rdSummary;
      if (Array.isArray(rd)) {
        const sample = rd[0] || {};
        rdSummary = {
          length: rd.length,
          sampleKeys: Object.keys(sample).slice(0, 12),
          hasCsn: !!sample.Csn,
          hasDate: !!sample.Date || !!sample.Instant,
          hasIsLocal: 'IsLocal' in sample,
        };
      } else if (rd != null) {
        rdSummary = `non-array: ${typeof rd}`;
      }
      if (rdSummary) {
        out.push({ index: i, constructor: ctorName, RenderedData: rdSummary });
      }
    }
    return out;
  }

  // ── Probe auth surface ────────────────────────────────────────────────
  function probeAuth() {
    const tokenEl = document.querySelector('input[name="__RequestVerificationToken"]');
    return {
      token_selector_works: !!tokenEl,
      token_value_length: tokenEl ? tokenEl.value.length : 0,
      base_path_candidates: Array.from(new Set(
        Array.from(document.querySelectorAll('a[href]'))
          .map(a => {
            try { return new URL(a.href, location.origin).pathname.split('/')[1] || ''; }
            catch (_) { return ''; }
          })
          .filter(Boolean)
      )).slice(0, 8).map(s => '/' + s),
      host: location.host,
      pathname: location.pathname,
    };
  }

  // ── Finalize ──────────────────────────────────────────────────────────
  global.discoverPortal = function () {
    const instances = probeInstances();
    const auth = probeAuth();

    // Deduplicate captured calls by (method, path-without-query)
    const seen = new Set();
    const uniqueCalls = [];
    for (const c of captured) {
      let pathOnly = c.url;
      try { pathOnly = new URL(c.url, location.origin).pathname; } catch (_) {}
      const key = c.method + ' ' + pathOnly;
      if (seen.has(key)) continue;
      seen.add(key);
      uniqueCalls.push({ ...c, pathOnly });
    }

    const result = {
      __schema: 'v3-discovery/0',
      capturedAt: new Date().toISOString(),
      host: location.host,
      instances,
      auth,
      apiCalls: uniqueCalls,
    };

    const json = JSON.stringify(result, null, 2);
    console.log('%c[v3 discover] result ready — copy from below or call window.__v3_discover_result',
      'color:#2196F3;font-weight:bold');
    global.__v3_discover_result = result;
    console.log(json);
    return json;
  };

  console.log('%c[v3 discover] active. Navigate Visits → expand a note → run discoverPortal()',
    'color:#2196F3;font-weight:bold');
})(typeof window !== 'undefined' ? window : globalThis);
