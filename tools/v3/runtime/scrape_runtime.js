/**
 * v3 scrape runtime — generic Epic-portal scraper driven by a JSON config.
 *
 * Reads a portal config (e.g. tools/v3/configs/ucsf.json) and executes the
 * declared jobs in dependency order. Returns structured results that the
 * Python converters in tools/v2/ already know how to consume.
 *
 * Architecture notes (after 2026-06-06 UCSF live test):
 *   - Filter spec is structured JSON ({and, or, not, path_truthy, path_equals})
 *     evaluated by a small in-runtime interpreter. Earlier prototype used
 *     `new Function()` to eval string expressions; that breaks under CSP when
 *     a fetch has occurred earlier in the same async block (real-world
 *     blocker found at UCSF). The structured form is CSP-safe.
 *   - Discovery supports an optional paginator that clicks a "Load more"
 *     button until exhausted (or a stop-when-seen item appears, for
 *     incremental scrapes that stop at the previous high-watermark).
 *
 * Usage (browser console, after logging into the portal):
 *
 *   const cfg = await (await fetch('/v3/configs/ucsf.json')).json();  // or paste inline
 *   const runner = new ScrapeRuntime(cfg);
 *   const results = await runner.run(['visits', 'notes']);
 *   // results.visits = array of {item, response, _provenance}
 *   // results.notes  = array of {item, response, _provenance}
 *
 * Usage (mobile WebView): same pattern; the host app injects this script after
 * the user authenticates, calls run() with the appropriate job list, and
 * exfiltrates the results to the BinaHealth backend.
 *
 * Important: scraped content must NEVER traverse the AI conversation channel
 * (P-PHI-STAYS-LOCAL in docs/CONCLUSIONS_LOG.md). Results flow browser →
 * local file or browser → BinaHealth backend, not browser → AI.
 */

(function (global) {
  'use strict';

  const RUNTIME_VERSION = 'v3.1.0';

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

  // ── Path lookup (null-tolerant) ──────────────────────────────────────
  function getPath(obj, path) {
    if (path == null) return undefined;
    const parts = String(path).split('.');
    let cur = obj;
    for (const p of parts) {
      if (cur == null) return undefined;
      cur = cur[p];
    }
    return cur;
  }

  // ── Filter spec evaluator (CSP-safe; no new Function / no eval) ─────
  // Accepted spec shapes:
  //   null | true      → accept all
  //   false            → reject all
  //   "a.b.c"          → path must be truthy
  //   [spec, spec, …]  → implicit AND
  //   { and: [...] }
  //   { or:  [...] }
  //   { not: spec }
  //   { path_truthy: "a.b.c" }
  //   { path_equals: ["a.b.c", value] }
  function evalFilter(spec, item) {
    if (spec == null || spec === true) return true;
    if (spec === false) return false;
    if (typeof spec === 'string') return !!getPath(item, spec);
    if (Array.isArray(spec)) return spec.every(s => evalFilter(s, item));
    if (typeof spec === 'object') {
      if ('and' in spec) return (spec.and || []).every(s => evalFilter(s, item));
      if ('or' in spec) return (spec.or || []).some(s => evalFilter(s, item));
      if ('not' in spec) return !evalFilter(spec.not, item);
      if ('path_truthy' in spec) return !!getPath(item, spec.path_truthy);
      if ('path_equals' in spec) {
        const [p, v] = spec.path_equals;
        return getPath(item, p) === v;
      }
    }
    console.warn('[v3 runtime] unknown filter spec', spec);
    return false;
  }

  // ── Paginator (for portals where the work-list paginates via UI) ─────
  function findButtonByText(text) {
    const needle = String(text).toLowerCase();
    return Array.from(document.querySelectorAll('a, button'))
      .find(el => el.textContent && el.textContent.trim().toLowerCase().includes(needle));
  }

  async function runPaginator(pagCfg, getRD, opts) {
    if (!pagCfg) return;
    const maxIters = pagCfg.max_iterations || 50;
    const settleMs = pagCfg.settle_ms || 200;
    const waitGrowMs = pagCfg.wait_for_growth_ms || 6000;
    if (pagCfg.type === 'click_until_gone') {
      for (let i = 0; i < maxIters; i++) {
        // Optional early stop for incremental scrapes
        if (pagCfg.stop_when_seen) {
          const rd = getRD();
          const hit = rd.some(item => getPath(item, pagCfg.stop_when_seen.path) === pagCfg.stop_when_seen.value);
          if (hit) {
            opts.log(`[paginator] stop_when_seen hit after ${i} clicks (${rd.length} items)`);
            return;
          }
        }
        const btn = findButtonByText(pagCfg.button_text);
        if (!btn) {
          opts.log(`[paginator] button-gone after ${i} clicks (${getRD().length} items)`);
          return;
        }
        const before = getRD().length;
        btn.click();
        const start = Date.now();
        while (Date.now() - start < waitGrowMs) {
          await sleep(300);
          if (getRD().length > before) break;
        }
        await sleep(settleMs);
      }
      opts.log(`[paginator] max-iterations cap reached (${maxIters})`);
    } else {
      opts.log(`[paginator] unknown type: ${pagCfg.type}`);
    }
  }

  // ── Discovery ────────────────────────────────────────────────────────
  async function discoverEpicRenderedData(discCfg, opts) {
    const inst = (global.Epic
      && global.Epic.PatientAccess
      && global.Epic.PatientAccess.Components
      && global.Epic.PatientAccess.Components.__Instances
      && global.Epic.PatientAccess.Components.__Instances[discCfg.instance]);
    if (!inst || !inst.RenderedData) {
      throw new Error(`No RenderedData at instance ${discCfg.instance}`);
    }
    if (discCfg.paginator) {
      await runPaginator(discCfg.paginator, () => inst.RenderedData, opts);
    }
    return inst.RenderedData.slice();
  }

  // Some Epic components don't expose their work-list as a single
  // RenderedData array — they keep it under `.Data.<path>`, often split
  // across multiple arrays per component. Stanford's UpcomingVisits
  // component (instance 5) keeps its entries in Data.NextNDaysVisits +
  // Data.LaterVisitsList + Data.InProgressVisits. This mode reads every
  // configured path and concatenates them. No paginator support here —
  // upcoming-style lists are bounded.
  function discoverEpicComponentData(discCfg) {
    const inst = (global.Epic
      && global.Epic.PatientAccess
      && global.Epic.PatientAccess.Components
      && global.Epic.PatientAccess.Components.__Instances
      && global.Epic.PatientAccess.Components.__Instances[discCfg.instance]);
    if (!inst) throw new Error(`No Epic instance ${discCfg.instance}`);
    const root = inst.Data;
    if (!root) throw new Error(`Instance ${discCfg.instance} has no .Data`);
    const out = [];
    for (const path of (discCfg.data_paths || [])) {
      const v = getPath(root, path);
      if (Array.isArray(v)) out.push(...v);
      else if (v != null) out.push(v);
    }
    return out;
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
    const v = getPath(item, clean);
    if (v === undefined) {
      if (optional) return '';
      throw new Error(`Item path missing or undefined: ${path}`);
    }
    return v;
  }

  function resolveTemplate(tpl, ctx) {
    if (typeof tpl === 'string') {
      const whole = tpl.match(/^\{(.+)\}$/);
      if (whole) return resolveToken(whole[1], ctx);
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

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ── Job execution ────────────────────────────────────────────────────
  async function runJob(jobName, portalCfg, token, pageNonce, priorResults, opts) {
    const jobCfg = portalCfg.jobs[jobName];
    if (!jobCfg) throw new Error(`Unknown job: ${jobName}`);

    let workItems;
    if (jobCfg.discovery.mode === 'epic_rendered_data') {
      workItems = await discoverEpicRenderedData(jobCfg.discovery, opts);
    } else if (jobCfg.discovery.mode === 'epic_component_data') {
      workItems = discoverEpicComponentData(jobCfg.discovery);
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

    if (jobCfg.filter) {
      const before = workItems.length;
      workItems = workItems.filter(item => evalFilter(jobCfg.filter, item));
      opts.log(`[${jobName}] filtered to ${workItems.length} (was ${before})`);
    }

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
