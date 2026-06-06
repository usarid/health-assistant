/**
 * v3 portal-discovery helper — paste in DevTools console (or inject via the
 * Chrome MCP) while logged into a new Epic-based MyChart portal (e.g.
 * Stanford MyHealth) to capture the portal-specific values needed for a v3
 * config.
 *
 * Usage:
 *   1. Log into the portal in your browser.
 *   2. Open DevTools console.
 *   3. Paste this entire file.
 *   4. Navigate to Visits and expand at least one visit with a clinical note
 *      (so the relevant API calls fire).
 *   5. Run `discoverPortal()` in the console — this finalizes capture and
 *      shows a "Save discovery" button.
 *   6. Click the Save button and save the JSON to a local file.
 *
 * Per P-PHI-STAYS-LOCAL: request body VALUES are scrubbed to type sketches
 * before being returned — CSNs, tokens, IDs become "<string length=N>" etc.
 * The structural information (field names, paths, methods) is what we need
 * to write the config; the values were never useful and were just incidental.
 *
 * What this captures:
 *   - The Epic SPA component instances and the size of each RenderedData array
 *     (so we can identify which instance holds the visits list).
 *   - Every JSON API call that fired while this script was active, with
 *     method / path / request-body field shape / response shape.
 *   - The portal's anti-forgery token presence + the URL base path.
 *
 * What this does NOT do:
 *   - Send any data anywhere. Everything stays in your browser. The
 *     "Save discovery" button uses showSaveFilePicker — you choose where the
 *     file lands; nothing leaves the tab automatically.
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
          // Both request body and response are scrubbed: we record the
          // shape (field names + value types), never the values. Per
          // P-PHI-STAYS-LOCAL.
          requestBodyShape: shapeOf(bodyParsed, 4),
          responseStatus: respStatus,
          responseShape: shapeOf(respJson, 3),
        });
      }
    }
  };

  function shapeOf(v, depth) {
    if (v == null) return typeof v;
    if (typeof v === 'boolean') return v;  // booleans aren't sensitive
    if (typeof v === 'number') return '<number>';
    if (typeof v === 'string') return `<string length=${v.length}>`;
    if (depth < 0) return typeof v;
    if (Array.isArray(v)) {
      return v.length === 0
        ? 'array[]'
        : `array[${v.length}] of ${shapeOf(v[0], depth - 1)}`;
    }
    if (typeof v === 'object') {
      const keys = Object.keys(v).slice(0, 16);
      const inner = {};
      for (const k of keys) inner[k] = shapeOf(v[k], depth - 1);
      if (Object.keys(v).length > 16) inner['…'] = `(${Object.keys(v).length - 16} more)`;
      return inner;
    }
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
    global.__v3_discover_result = result;

    // Inject a "Save discovery" button — uses showSaveFilePicker (requires
    // a user-gesture click) so the JSON lands in a file the user picks.
    // Per P-PHI-STAYS-LOCAL: nothing about the file leaves the tab on its
    // own; the user (or the Chrome MCP click-as-user-gesture) initiates it.
    document.getElementById('v3-discover-save-btn')?.remove();
    const btn = document.createElement('button');
    btn.id = 'v3-discover-save-btn';
    btn.textContent = 'Save discovery';
    Object.assign(btn.style, {
      position: 'fixed', top: '8px', left: '8px', zIndex: '999999',
      padding: '12px 18px', fontSize: '14px', background: '#4CAF50', color: 'white',
      border: 'none', borderRadius: '4px', cursor: 'pointer', fontWeight: 'bold',
    });
    btn.onclick = async () => {
      btn.textContent = 'Saving…';
      try {
        const host = (location.host || 'portal').replace(/[^A-Za-z0-9.-]/g, '_');
        const name = `${host}-discovery.json`;
        const handle = await window.showSaveFilePicker({
          suggestedName: name,
          types: [{ description: 'JSON', accept: { 'application/json': ['.json'] } }],
        });
        const writable = await handle.createWritable();
        await writable.write(json);
        await writable.close();
        btn.textContent = '✓ Saved';
        global.__v3_save_status = 'saved';
      } catch (e) {
        btn.textContent = 'Error: ' + String(e).substring(0, 40);
        global.__v3_save_status = 'error: ' + String(e).substring(0, 100);
      }
    };
    document.body.appendChild(btn);

    console.log('%c[v3 discover] result ready — click the green "Save discovery" button to save to a file',
      'color:#4CAF50;font-weight:bold');
    return { ready: true, sizeKB: (json.length / 1024).toFixed(1), buttonId: 'v3-discover-save-btn' };
  };

  console.log('%c[v3 discover] active. Navigate Visits → expand a note → run discoverPortal()',
    'color:#2196F3;font-weight:bold');
})(typeof window !== 'undefined' ? window : globalThis);
