import 'stanford_config.dart';

/// JS scripts the host injects into the WebView per scrape job.
class ScrapeJobs {
  /// Per-visit Stanford scrape. Runs on a
  /// /signedin/appointments/after-visit-summary/csn=X&encType=3 page.
  ///
  /// Two things happen:
  ///   1. Fire-and-forget keepalive pings — every visit's scrape
  ///      contributes to keeping both Stanford sessions warm. At ~10s per
  ///      visit this is plenty of cadence.
  ///   2. Click the Clinical Notes tab, poll for the .pgSection container,
  ///      extract its outerHTML, call back to Dart via the saveNote handler.
  ///
  /// Calls window.flutter_inappwebview.callHandler('saveNote', { csn, html, error? }).
  static String stanfordSingleNote() {
    return '''
(async () => {
  function send(payload) {
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      window.flutter_inappwebview.callHandler('saveNote', payload);
    } else {
      console.warn('[scraper] no flutter_inappwebview handler');
    }
  }

  // (1) Keepalive intentionally DISABLED for this experiment (2026-06-13).
  //     Working hypothesis: Stanford's anti-abuse flags injected keepalive
  //     fetches differently than its own-page-code ones, causing session
  //     revocation after a single rapid visit. If the batch completes
  //     without keepalive, we know keepalive was the trigger. (The
  //     Dart-side Timer is also short-circuited in scrape_screen.dart for
  //     this run.)

  // (2) Scrape the Clinical Notes tab
  const m = location.href.match(/csn=([^&]+)/);
  const csn = m ? decodeURIComponent(m[1]) : 'unknown';

  const candidates = Array.from(document.querySelectorAll('li, a, button'))
    .filter(el => /Clinical Notes/i.test(el.textContent || ''));
  const tab = candidates.find(el => (el.textContent || '').trim() === 'Clinical Notes')
    || candidates[0];

  if (!tab) {
    send({ csn, html: '', error: 'no-notes-tab' });
    return;
  }

  tab.click();

  // Stanford renders the body inside a .pgSection div. Real content is
  // ~9-200 KB; skeleton-only is ~750 chars. Threshold of 200 chars catches
  // any successful render. 15s upper bound per visit.
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

  /// Standalone keepalive — fired by the Dart-side Timer.periodic every 30s
  /// as a safety net if a per-visit scrape hangs and stops contributing to
  /// the per-visit keepalive in stanfordSingleNote().
  static String keepalive() {
    final urls = StanfordConfig.keepaliveUrls.map((u) => "'$u'").join(', ');
    return '''
for (const u of [$urls]) {
  try { fetch(u, { method: 'GET', credentials: 'include', mode: 'cors' }); }
  catch (_) {}
}
true;
''';
  }
}
