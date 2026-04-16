/**
 * MyChart Network Interceptor
 * ============================
 * Paste this FIRST, then navigate around MyChart normally.
 * It hooks into fetch() and XMLHttpRequest to capture all API responses.
 *
 * Usage:
 *   1. Reload MyChart (go to mskmychart.mskcc.org/MyChart/Home)
 *   2. Open DevTools Console
 *   3. Paste this script
 *   4. Click through: Visits, Messages, Test Results, Medications, etc.
 *   5. When done, type: downloadCapture()
 *
 * The script silently records every JSON API response in the background.
 */

(function() {
  'use strict';

  if (window.__networkCapture) {
    console.log('%c[Interceptor] Already running! Navigate around, then call downloadCapture()', 'color: #9C27B0; font-weight: bold');
    return;
  }

  const captured = [];
  window.__networkCapture = captured;

  const log = (msg) => console.log(`%c[Interceptor] ${msg}`, 'color: #9C27B0; font-weight: bold');

  // === Hook fetch() ===
  const origFetch = window.fetch;
  window.fetch = async function(...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    const resp = await origFetch.apply(this, args);

    // Clone the response so we can read it without consuming the stream
    const clone = resp.clone();
    try {
      const contentType = clone.headers.get('content-type') || '';
      if (contentType.includes('json') || contentType.includes('fhir')) {
        const body = await clone.json();
        const entry = {
          type: 'fetch',
          url: url,
          status: resp.status,
          contentType: contentType,
          timestamp: new Date().toISOString(),
          data: body,
        };
        captured.push(entry);
        log(`fetch: ${url.substring(0, 100)} (${resp.status}) — ${JSON.stringify(body).length} bytes`);
      }
    } catch(e) {
      // Not JSON, skip
    }

    return resp;
  };

  // === Hook XMLHttpRequest ===
  const origXHROpen = XMLHttpRequest.prototype.open;
  const origXHRSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__capturedUrl = url;
    this.__capturedMethod = method;
    return origXHROpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function(...args) {
    this.addEventListener('load', function() {
      try {
        const contentType = this.getResponseHeader('content-type') || '';
        if (contentType.includes('json') || contentType.includes('fhir')) {
          const body = JSON.parse(this.responseText);
          const entry = {
            type: 'xhr',
            method: this.__capturedMethod,
            url: this.__capturedUrl,
            status: this.status,
            contentType: contentType,
            timestamp: new Date().toISOString(),
            data: body,
          };
          captured.push(entry);
          log(`XHR: ${(this.__capturedUrl || '').substring(0, 100)} (${this.status})`);
        }
      } catch(e) {
        // Not JSON, skip
      }
    });
    return origXHRSend.apply(this, args);
  };

  // === Download function ===
  window.downloadCapture = function() {
    const summary = {};
    captured.forEach(c => {
      const key = c.url?.split('?')[0] || 'unknown';
      summary[key] = (summary[key] || 0) + 1;
    });

    console.log('%c\n=== CAPTURED API CALLS ===', 'color: #9C27B0; font-weight: bold; font-size: 14px');
    console.log(`Total: ${captured.length} responses`);
    for (const [url, count] of Object.entries(summary).sort((a,b) => b[1] - a[1])) {
      console.log(`  ${count}x  ${url}`);
    }

    const output = {
      capturedAt: new Date().toISOString(),
      source: 'MSKCC MyChart (Network Intercept)',
      totalCalls: captured.length,
      calls: captured,
    };

    const blob = new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `mskcc_network_capture_${new Date().toISOString().slice(0,10)}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    log(`✓ Downloaded ${captured.length} captured API responses`);
  };

  // === Status function ===
  window.captureStatus = function() {
    log(`Captured ${captured.length} API responses so far`);
    const urls = [...new Set(captured.map(c => c.url?.split('?')[0]))];
    urls.forEach(u => log(`  ${u}`));
  };

  log('Network interceptor installed!');
  log('Now navigate around MyChart — click Visits, Messages, Test Results, etc.');
  log('Commands:');
  log('  captureStatus()   — see what\'s been captured so far');
  log('  downloadCapture() — download all captured data as JSON');

})();
