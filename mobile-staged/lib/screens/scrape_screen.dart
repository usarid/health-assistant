import 'package:flutter/material.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import '../scrape/scrape_jobs.dart';
import '../storage/local_writer.dart';

/// Day-one scrape screen: a single WebView whose lifecycle the host owns.
///
/// Flow:
///   - WebView opens Stanford MyHealth login.
///   - User logs in (handles MFA in the WebView, full credential control).
///   - When the URL lands on /signedin/..., we enable the "Scrape this visit"
///     action in the AppBar.
///   - Tap → host drives loadUrl(after-visit-summary URL for hardcoded test CSN).
///     This top-level navigation is what gets Sec-Fetch-Dest: document, the one
///     thing page-JS scraping CAN'T fake (proven 2026-06-08).
///   - onLoadStop → host injects scraper JS via evaluateJavascript.
///   - JS clicks the Clinical Notes tab, polls .pgSection, captures outerHTML,
///     calls back via window.flutter_inappwebview.callHandler('saveNote', ...).
///   - Dart handler writes JSON to the app's documents directory.
class ScrapeScreen extends StatefulWidget {
  const ScrapeScreen({super.key});

  @override
  State<ScrapeScreen> createState() => _ScrapeScreenState();
}

class _ScrapeScreenState extends State<ScrapeScreen> {
  InAppWebViewController? _ctrl;
  String _status = 'Tap the address bar to log into Stanford MyHealth';
  String _currentUrl = '';
  bool _onSignedInPage = false;

  // Day-one hardcoded test CSN — the 3/27/2026 Telemedicine visit with
  // Susan Ziolkowski, MD. We know it has a real Clinical Notes tab with
  // content (validated via Chrome MCP 2026-06-08). Replace with a dynamically
  // discovered CSN in iteration 2.
  static const String _testCsn =
      'WP-242cylB3JEw7-2F-2FQUxFt6Xmsg-3D-3D-24uWtvS9-2FNhwMvYRaT4g0QO20SzIPlEtu5R6S4k0Qya-2Fc-3D';

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('BinaHealth — day-1 Stanford scrape'),
        actions: [
          if (_onSignedInPage)
            TextButton(
              onPressed: _scrapeOneVisit,
              child: const Text(
                'Scrape this visit',
                style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
              ),
            ),
        ],
      ),
      body: Column(
        children: [
          Container(
            padding: const EdgeInsets.all(12),
            color: Colors.amber.shade100,
            width: double.infinity,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _status,
                  style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
                ),
                if (_currentUrl.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(
                      _currentUrl,
                      style: const TextStyle(fontSize: 11, color: Colors.black54),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
              ],
            ),
          ),
          Expanded(
            child: InAppWebView(
              initialUrlRequest: URLRequest(
                url: WebUri('https://myhealth.stanfordhealthcare.org/#/'),
              ),
              initialSettings: InAppWebViewSettings(
                javaScriptEnabled: true,
                // Mimic mobile Safari UA — Stanford may serve a different UI to
                // a default macOS WKWebView UA, and we want our prototype to see
                // what the real iPhone app will see.
                userAgent:
                    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) '
                    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 '
                    'Mobile/15E148 Safari/604.1',
              ),
              onWebViewCreated: (c) {
                _ctrl = c;
                c.addJavaScriptHandler(
                  handlerName: 'saveNote',
                  callback: _onSaveNoteHandler,
                );
              },
              onLoadStop: (c, url) async {
                final urlStr = url?.toString() ?? '';
                final onSignedIn = urlStr.contains('/signedin/');
                setState(() {
                  _currentUrl = urlStr;
                  _onSignedInPage = onSignedIn;
                  if (onSignedIn && !_status.contains('Scraped')) {
                    _status = 'Logged in. Tap "Scrape this visit" to test.';
                  }
                });
              },
            ),
          ),
        ],
      ),
    );
  }

  Future<dynamic> _onSaveNoteHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) {
      setState(() => _status = 'Handler called with bad args');
      return {'ok': false, 'reason': 'bad-args'};
    }
    final m = Map<String, dynamic>.from(args.first as Map);
    final csn = (m['csn'] ?? 'unknown').toString();
    final html = (m['html'] ?? '').toString();
    final error = m['error']?.toString();

    if (error != null && error.isNotEmpty) {
      setState(() => _status = 'Scrape failed: $error (csn ${csn.substring(0, 8)}…)');
      return {'ok': false, 'reason': error};
    }
    if (html.isEmpty) {
      setState(() => _status = 'Empty HTML returned for ${csn.substring(0, 8)}…');
      return {'ok': false, 'reason': 'empty'};
    }

    final path = await LocalWriter.writeNote(csn, html);
    setState(() {
      _status = 'Scraped ${html.length} chars → $path';
    });
    return {'ok': true, 'path': path};
  }

  Future<void> _scrapeOneVisit() async {
    final ctrl = _ctrl;
    if (ctrl == null) return;

    setState(() => _status = 'Navigating to test visit (top-level)…');

    final url = 'https://myhealth.stanfordhealthcare.org/signedin/appointments/'
        'after-visit-summary/csn=$_testCsn&encType=3';

    // The critical line. controller.loadUrl IS the host-driven top-level
    // navigation that produces Sec-Fetch-Dest: document — the thing JS in
    // a page cannot produce, and the one the portal requires.
    await ctrl.loadUrl(urlRequest: URLRequest(url: WebUri(url)));

    // Give the page a moment past onLoadStop. Stanford renders the AVS tab
    // first; we'll click Clinical Notes in the injected JS.
    await Future.delayed(const Duration(seconds: 3));

    setState(() => _status = 'Injecting scraper…');

    await ctrl.evaluateJavascript(source: ScrapeJobs.stanfordSingleNote);
  }
}
