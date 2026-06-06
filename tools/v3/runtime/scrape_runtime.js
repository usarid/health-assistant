/**
 * v3 scrape runtime — generic Epic-portal scraper driven by a JSON config.
 *
 * Reads a portal config (e.g. tools/v3/configs/ucsf.json) and executes the
 * declared jobs in dependency order. Returns structured results that the
 * Python converters in tools/v2/ already know how to consume.
 *
 * Usage (browser console, after logging into the portal):
 *
 *   const cfg = await (await fetch('/v3/configs/ucsf.json')).json();  // or paste inline
 *   const runner = new ScrapeRuntime(cfg);
 *   const results = await runner.run(['visits', 'notes']);
 *   // results.visits = array of {item, response, _provenance}
 *   // results.notes  = array of {item, response, _provenance}
 *   copy(JSON.stringify(results));  // copy to clipboard for ingest
 *
 * Usage (mobile WebView): same pattern; the host app injects this script after
 * the user authenticates, calls run() with the appropriate job list, and
 * exfiltrates the results to the BinaHealth backend.
 */

(function (global) {
  'use strict';

  const RUNTIME_VERSION = 'v3.0.0';

  // ── Auth ─────────────────────────────────────────────────────────────
  function extractToken(authCfg) {
    const el = document.querySelector(authCfg.token_selector);
    return el ? el.value : '';
  }

  function extractPageNonce(authCfg) {
    if (!authCfg.page_nonce_regex) return null;
    const re = new RegExp(authCfg.page_nonce_regex);
    const m = document.documentElement.innerHTML.match(re);
    return m ? m[1] || m[0] : null;
  }

  // ── Discovery ────────────────────────────────────────────────────────
  function discoverEpicRenderedData(discCfg) {
    const inst = (global.Epic
      && global.Epic.PatientAccess
      && global.Epic.PatientAccess.Components
      && global.Epic.PatientAccess.Components.__Instances
      && global.Epic.PatientAccess.Components.__Instances[discCfg.instance]);
    if (!inst || !inst.RenderedData) {
      throw new Error(`No RenderedData at instance ${discCfg.instance}`);
    }
    return inst.RenderedData.slice();
  }

  function discoverDomHrefScan(discCfg) {
    const out = [];
    const seen = new Set();
    document.querySelectorAll(discCfg.selector).forEach(a => {
      const href = a.getAttribute('href') || '';
      const item = {};
      for (const param of discCfg.params || []) {
        const m = href.match(new RegExp('[?&]' + param + '=([^&]+)'));
        if (m) item[param] = decodeURIComponent(m[1]);
      }
      if (Object.keys(item).length === 0) return;
      const key = JSON.stringify(item);
      if (seen.has(key)) return;
      seen.add(key);
      item._href = href;
      item._linkText = a.textContent.trim().substring(0, 300);
      out.push(item);
    });
    return out;
  }

  // ── Filter evaluation ────────────────────────────────────────────────
  function evalFilter(expr, item) {
    if (!expr) return true;
    try {
      // eslint-disable-next-line no-new-func
      return new Function('item', `return (${expr});`)(item);
    } catch (e) {
      console.warn('[v3 runtime] filter eval failed:', e, expr);
      return false;
    }
  }

  // ── Template resolution ──────────────────────────────────────────────
  function genNonce() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  function lookupItemPath(item, path) {
    // dotted path; supports '?' suffix on final segment for missing-ok
    const optional = path.endsWith('?');
    const clean = optional ? path.slice(0, -1) : path;
    const parts = clean.split('.');
    let cur = item;
    for (const p of parts) {
      if (cur == null) {
        if (optional) return '';
        throw new Error(`Item path missing: ${path}`);
      }
      cur = cur[p];
    }
    if (cur === undefined) {
      if (optional) return '';
      throw new Error(`Item path resolved to undefined: ${path}`);
    }
    return cur;
  }

  function resolveTemplate(tpl, ctx) {
    if (typeof tpl === 'string') {
      // Whole-string template ("{item.X}") returns the raw value (preserves type)
      const whole = tpl.match(/^\{(.+)\}$/);
      if (whole) {
        return resolveToken(whole[1], ctx);
      }
      // Interpolated string
      return tpl.replace(/\{([^}]+)\}/g, (_, tok) => String(resolveToken(tok, ctx)));
    }
    if (Array.isArray(tpl)) return tpl.map(v => resolveTemplate(v, ctx));
    if (tpl && typeof tpl === 'object') {
      const out = {};
      for (const k of Object.keys(tpl)) {
        if (k.startsWith('_')) continue;  // skip _comment etc.
        out[k] = resolveTemplate(tpl[k], ctx);
      }
      return out;
    }
    return tpl;
  }

  function resolveToken(tok, ctx) {
    if (tok === 'auto_nonce') return genNonce();
    if (tok === 'auto_seq') return ctx.bumpSeq();
    if (tok === 'auto_iso_now') return new Date().toISOString();
    if (tok.startsWith('item.')) return lookupItemPath(ctx.item, tok.slice(5));
    return tok;  // literal
  }

  // ── Request ──────────────────────────────────────────────────────────
  async function callEndpoint({ portalCfg, token, pageNonce, endpoint, item, ctx }) {
    const url = portalCfg.base_path + endpoint.path;
    const body = resolveTemplate(endpoint.body_template, { item, bumpSeq: ctx.bumpSeq });
    if (pageNonce) body.PageNonce = pageNonce;

    const headers = Object.assign({
      'accept': 'application/json, text/plain, */*',
      'content-type': 'application/json',
      'x-requested-with': 'XMLHttpRequest',
      [portalCfg.auth.token_header]: token,
    }, endpoint.headers || {});

    const resp = await fetch(url, {
      method: endpoint.method || 'POST',
      credentials: 'include',
      headers,
      body: JSON.stringify(body),
    });
    let parsed = null;
    if (resp.ok) {
      try { parsed = await resp.json(); } catch (e) { parsed = await resp.text(); }
    }
    return { status: resp.status, ok: resp.ok, body: parsed };
  }

  // ── Sleep + concurrency ──────────────────────────────────────────────
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ── Job execution ────────────────────────────────────────────────────
  async function runJob(jobName, portalCfg, token, pageNonce, priorResults, opts) {
    const jobCfg = portalCfg.jobs[jobName];
    if (!jobCfg) throw new Error(`Unknown job: ${jobName}`);

    // 1. Discover the work-list
    let workItems;
    if (jobCfg.discovery.mode === 'epic_rendered_data') {
      workItems = discoverEpicRenderedData(jobCfg.discovery);
    } else if (jobCfg.discovery.mode === 'dom_href_scan') {
      workItems = discoverDomHrefScan(jobCfg.discovery);
    } else if (jobCfg.discovery.mode === 'from_dependency') {
      const dep = jobCfg.depends_on;
      if (!dep || !priorResults[dep]) throw new Error(`Job ${jobName} depends_on ${dep} but it has no results`);
      workItems = priorResults[dep].slice();
    } else {
      throw new Error(`Unknown discovery mode: ${jobCfg.discovery.mode}`);
    }
    opts.log(`[${jobName}] discovered ${workItems.length} items`);

    // 2. Filter
    if (jobCfg.filter) {
      const before = workItems.length;
      workItems = workItems.filter(item => evalFilter(jobCfg.filter, item));
      opts.log(`[${jobName}] filtered to ${workItems.length} (was ${before})`);
    }

    // 3. Iterate, call endpoint per item
    const results = [];
    let seq = 0;
    const ctx = { bumpSeq: () => ++seq };
    const rate = jobCfg.rate_limit_ms || 0;
    for (let i = 0; i < workItems.length; i++) {
      ctx.item = workItems[i];
      const t0 = Date.now();
      let response;
      try {
        response = await callEndpoint({
          portalCfg, token, pageNonce, endpoint: jobCfg.endpoint, item: workItems[i], ctx,
        });
      } catch (e) {
        response = { ok: false, status: 0, error: String(e) };
      }
      results.push({
        item: workItems[i],
        response: response.body,
        _http: { status: response.status, ok: response.ok },
        _provenance: {
          scraped_at: new Date().toISOString(),
          portal_id: portalCfg.portal_id,
          config_version: portalCfg.config_version,
          runtime_version: RUNTIME_VERSION,
          job: jobName,
          endpoint: jobCfg.endpoint.path,
        },
      });
      if ((i + 1) % 10 === 0) {
        opts.log(`[${jobName}] ${i + 1}/${workItems.length} done`);
      }
      const elapsed = Date.now() - t0;
      if (rate > elapsed) await sleep(rate - elapsed);
    }
    return results;
  }

  // ── Public API ───────────────────────────────────────────────────────
  class ScrapeRuntime {
    constructor(portalCfg, options = {}) {
      this.portalCfg = portalCfg;
      this.options = Object.assign({
        log: (...a) => console.log('%c[v3]', 'color:#2196F3;font-weight:bold', ...a),
      }, options);
    }

    async run(jobList) {
      const token = extractToken(this.portalCfg.auth);
      if (!token) throw new Error('No anti-forgery token found in DOM');
      const pageNonce = extractPageNonce(this.portalCfg.auth);
      this.options.log(`runtime ${RUNTIME_VERSION} starting on ${this.portalCfg.portal_id}`);
      this.options.log(`token=found  pageNonce=${pageNonce ? 'found' : 'not used'}`);

      const results = {};
      for (const job of jobList) {
        this.options.log(`---- job: ${job} ----`);
        results[job] = await runJob(job, this.portalCfg, token, pageNonce, results, this.options);
        this.options.log(`[${job}] complete: ${results[job].length} items returned`);
      }
      return results;
    }
  }

  global.ScrapeRuntime = ScrapeRuntime;
  global.SCRAPE_RUNTIME_VERSION = RUNTIME_VERSION;
})(typeof window !== 'undefined' ? window : globalThis);
