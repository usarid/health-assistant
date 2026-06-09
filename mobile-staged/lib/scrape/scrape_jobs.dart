/// JS scripts the host injects into the WebView per scrape job.
///
/// In iteration 2 these will be loaded from the per-portal JSON configs in
/// tools/v3/configs/ (Stanford: click Clinical Notes tab → poll .pgSection →
/// callback; UCSF: different page structure → different JS). For day-one a
/// single hardcoded Stanford script is sufficient to prove the architecture.
class ScrapeJobs {
  /// Runs on a Stanford /signedin/appointments/after-visit-summary/csn=X&encType=3
  /// page. Finds the Clinical Notes tab, clicks it, polls for the .pgSection
  /// content to render, then calls back to Dart with the captured HTML.
  ///
  /// Calls window.flutter_inappwebview.callHandler('saveNote', { csn, html, error? }).
  static const String stanfordSingleNote = r'''
(async () => {
  function send(payload) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler('saveNote', payload);
    } else {
      console.warn('[scraper] no flutter_inappwebview handler — running outside the host?');
    }
  }

  const m = location.href.match(/csn=([^&]+)/);
  const csn = m ? decodeURIComponent(m[1]) : 'unknown';

  // Find the Clinical Notes tab on the Stanford after-visit-summary page.
  const candidates = Array.from(document.querySelectorAll('li, a, button'))
    .filter(el => /Clinical Notes/i.test(el.textContent || ''));
  const tab = candidates.find(el => (el.textContent || '').trim() === 'Clinical Notes')
    || candidates[0];

  if (!tab) {
    send({ csn, html: '', error: 'no-notes-tab' });
    return;
  }

  tab.click();

  // Poll for the rendered note container. Stanford renders the body inside
  // a .pgSection div (validated via Chrome MCP 2026-06-08). Real content is
  // ~9-200 KB; skeleton-only is ~750 chars. Threshold of 200 chars catches
  // any successful render.
  const startedAt = Date.now();
  while (Date.now() - startedAt < 15000) {
    await new Promise(r => setTimeout(r, 400));
    const section = document.querySelector('.pgSection');
    if (section && section.textContent.length > 200) {
      send({ csn, html: section.outerHTML });
      return;
    }
  }

  send({ csn, html: '', error: 'timeout-waiting-for-pgSection' });
})();
''';
}
