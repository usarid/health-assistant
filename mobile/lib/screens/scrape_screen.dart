import 'dart:async';
import 'dart:convert';
import 'dart:math' show Random;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import '../scrape/scrape_jobs.dart';
import '../scrape/stanford_config.dart';
import '../storage/local_writer.dart';

/// Iteration 2 scrape screen.
///
/// Beyond iteration 1's single-visit demo, this:
///   - Loads the Stanford CSN list from assets/stanford-v3-visits.json
///     (filtered to visits with shareable notes — typically ~106 entries).
///   - Loops over each CSN: host-driven controller.loadUrl → await
///     onLoadStop → inject scraper → await saveNote callback → persist.
///   - Runs keepalive belt-and-suspenders:
///       (a) per-visit scrape JS fires keepalive fetches at scrape start
///           (~10s cadence)
///       (b) Dart Timer.periodic injects a keepalive JS every 30s as a
///           safety net if a per-visit scrape stalls
///   - Persists each note to its own JSON immediately (crash-safe), updates
///     a manifest, and writes a consolidated JSON at the end.
///   - Surfaces live progress: X/N captured, Y real, Z errors • K pings.
class ScrapeScreen extends StatefulWidget {
  const ScrapeScreen({super.key});

  @override
  State<ScrapeScreen> createState() => _ScrapeScreenState();
}

class _ScrapeScreenState extends State<ScrapeScreen> {
  InAppWebViewController? _ctrl;

  String _status = 'Log into Stanford MyHealth';
  String _currentUrl = '';
  bool _onSignedInPage = false;
  bool _batchRunning = false;
  bool _abortRequested = false;

  // Single-visit test CSN (kept from iteration 1 so we can spot-check)
  static const String _testCsn =
      'WP-242cylB3JEw7-2F-2FQUxFt6Xmsg-3D-3D-24uWtvS9-2FNhwMvYRaT4g0QO20SzIPlEtu5R6S4k0Qya-2Fc-3D';

  // Completers that bridge JS lifecycle events into Dart's async/await
  Completer<void>? _navCompleter;
  Completer<Map<String, dynamic>>? _scrapeCompleter;

  // Batch state
  int _batchTotal = 0;
  int _batchIndex = 0;
  final List<CapturedNote> _captured = [];
  final List<ScrapeError> _errors = [];
  DateTime? _batchStartedAt;

  // Keepalive (Dart-side safety net)
  Timer? _keepaliveTimer;
  int _keepalivePings = 0;

  @override
  void dispose() {
    _keepaliveTimer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('BinaHealth'),
        backgroundColor: Colors.teal,
        foregroundColor: Colors.white,
      ),
      floatingActionButton: _buildFab(),
      floatingActionButtonLocation: FloatingActionButtonLocation.endFloat,
      body: Column(
        children: [
          Container(
            padding: const EdgeInsets.all(12),
            color: _batchRunning ? Colors.blue.shade50 : Colors.amber.shade100,
            width: double.infinity,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _status,
                  style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
                ),
                if (_batchRunning)
                  Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Text(
                      '$_batchIndex/$_batchTotal • captured ${_captured.length} '
                      '(${_captured.where((c) => c.visibleTextLength > 100).length} real) '
                      '• errors ${_errors.length} • keepalive $_keepalivePings',
                      style: const TextStyle(fontSize: 11, color: Colors.black87, fontFamily: 'monospace'),
                    ),
                  ),
                if (_currentUrl.isNotEmpty && !_batchRunning)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(_currentUrl,
                        style: const TextStyle(fontSize: 11, color: Colors.black54),
                        overflow: TextOverflow.ellipsis),
                  ),
              ],
            ),
          ),
          Expanded(
            child: InAppWebView(
              initialUrlRequest: URLRequest(url: WebUri(StanfordConfig.loginUrl)),
              initialSettings: InAppWebViewSettings(
                javaScriptEnabled: true,
                userAgent: StanfordConfig.mobileUserAgent,
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
                final onSignedIn = urlStr.contains(StanfordConfig.signedInMarker);
                setState(() {
                  _currentUrl = urlStr;
                  _onSignedInPage = onSignedIn;
                  if (onSignedIn && !_batchRunning && !_status.startsWith('Scrape')) {
                    _status = 'Logged in. Choose Test or Scrape All.';
                  }
                });
                // Resolve a pending navigation if this is the URL we asked for
                if (_navCompleter != null && _navCompleter!.isCompleted == false) {
                  _navCompleter!.complete();
                }
              },
            ),
          ),
        ],
      ),
    );
  }

  Widget? _buildFab() {
    if (_batchRunning) {
      return FloatingActionButton.extended(
        onPressed: () { setState(() { _abortRequested = true; _status = 'Abort requested — finishing current visit…'; }); },
        icon: const Icon(Icons.stop),
        label: const Text('Abort batch'),
        backgroundColor: Colors.red.shade600,
        foregroundColor: Colors.white,
      );
    }
    if (!_onSignedInPage) return null;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        FloatingActionButton.extended(
          heroTag: 'test',
          onPressed: () => _scrapeOne(_testCsn),
          icon: const Icon(Icons.science),
          label: const Text('Test one'),
          backgroundColor: Colors.grey.shade700,
          foregroundColor: Colors.white,
        ),
        const SizedBox(height: 12),
        FloatingActionButton.extended(
          heroTag: 'batch',
          onPressed: _scrapeAll,
          icon: const Icon(Icons.cloud_download),
          label: const Text('Scrape all'),
          backgroundColor: Colors.teal,
          foregroundColor: Colors.white,
        ),
      ],
    );
  }

  // ── Handler the WebView calls back into ────────────────────────────
  Future<dynamic> _onSaveNoteHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false, 'reason': 'bad-args'};
    final m = Map<String, dynamic>.from(args.first as Map);
    if (_scrapeCompleter != null && !_scrapeCompleter!.isCompleted) {
      _scrapeCompleter!.complete(m);
    }
    return {'ok': true};
  }

  // ── Single test scrape (preserves iteration-1 flow) ────────────────
  Future<void> _scrapeOne(String csn) async {
    if (_ctrl == null) return;
    setState(() => _status = 'Test scrape: navigating…');
    final res = await _scrapeOneVisit(csn);
    if (res == null) {
      setState(() => _status = 'Test scrape: navigation failed');
      return;
    }
    final html = (res['html'] ?? '').toString();
    final err = res['error']?.toString();
    if (err != null && err.isNotEmpty) {
      setState(() => _status = 'Test scrape failed: $err');
      return;
    }
    final path = await LocalWriter.writeNote(csn, html);
    setState(() => _status = 'Test scrape: ${html.length} chars → $path');
  }

  // ── The actual loop ────────────────────────────────────────────────
  Future<void> _scrapeAll() async {
    if (_batchRunning) return;
    final csns = await _loadCsnList();
    if (csns.isEmpty) {
      setState(() => _status = 'Could not load CSN list from assets/stanford-v3-visits.json');
      return;
    }

    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _batchTotal = csns.length;
      _batchIndex = 0;
      _captured.clear();
      _errors.clear();
      _batchStartedAt = DateTime.now();
      _keepalivePings = 0;
      _status = 'Scraping ${csns.length} visits — keepalive every 30s';
    });

    _startKeepalive();

    final rng = Random();
    try {
      for (int i = 0; i < csns.length; i++) {
        if (_abortRequested) {
          setState(() => _status = 'Aborted at $i/${csns.length}');
          break;
        }
        final csn = csns[i];
        setState(() => _batchIndex = i + 1);
        await LocalWriter.writeBatchManifest(BatchManifest(
          startedAt: _batchStartedAt!,
          totalCount: csns.length,
          currentIndex: i,
          capturedCount: _captured.length,
          errorCount: _errors.length,
          currentCsn: csn,
        ));

        final res = await _scrapeOneVisit(csn);
        if (res == null) {
          _errors.add(ScrapeError(csn: csn, index: i, reason: 'nav-failed', at: DateTime.now()));
        } else if (res['sessionDead'] == true) {
          // Session revoked — every subsequent visit will fail with the same
          // error. Stop the batch cleanly rather than burn through 100 more
          // doomed iterations.
          _errors.add(ScrapeError(csn: csn, index: i, reason: res['error']?.toString() ?? 'session-dead', at: DateTime.now()));
          setState(() => _status = 'Stopped at $i/${csns.length} — session revoked. Log in again and retry.');
          break;
        } else {
          final html = (res['html'] ?? '').toString();
          final err = res['error']?.toString();
          if (err != null && err.isNotEmpty) {
            _errors.add(ScrapeError(csn: csn, index: i, reason: err, at: DateTime.now()));
          } else {
            final plain = html.replaceAll(RegExp(r'<[^>]+>'), '').trim();
            _captured.add(CapturedNote(
              csn: csn,
              html: html,
              htmlLength: html.length,
              visibleTextLength: plain.length,
              capturedAt: DateTime.now(),
            ));
            await LocalWriter.writeNote(csn, html);
          }
        }

        // Between-visit pacing. Stanford rate-limits rapid-fire navigation;
        // 3-7s jittered keeps the cadence below their threshold.
        if (i + 1 < csns.length && !_abortRequested) {
          final pauseMs = 3000 + rng.nextInt(4000);
          await Future.delayed(Duration(milliseconds: pauseMs));
        }
      }

      final finishedAt = DateTime.now();
      final path = await LocalWriter.writeConsolidated(
        captured: _captured,
        errors: _errors,
        startedAt: _batchStartedAt!,
        finishedAt: finishedAt,
      );
      final dur = finishedAt.difference(_batchStartedAt!);
      setState(() => _status =
          'DONE in ${dur.inSeconds}s — ${_captured.length} captured '
          '(${_captured.where((c) => c.visibleTextLength > 100).length} real), '
          '${_errors.length} errors → $path');
    } finally {
      _stopKeepalive();
      setState(() => _batchRunning = false);
    }
  }

  /// One iteration of the loop. Returns null if navigation never settled;
  /// otherwise the saveNote handler's payload (containing 'html' or 'error').
  ///
  /// Pacing notes (2026-06-13 finding): Stanford rate-limits rapid-fire
  /// navigation. ~2s/visit triggered session revocation after ~20s. We
  /// pace conservatively: 4s settle + 3-7s jittered between visits.
  /// Estimated run time at this cadence: ~15s/visit × 106 = ~26 min.
  Future<Map<String, dynamic>?> _scrapeOneVisit(String csn) async {
    final url = StanfordConfig.visitDetailUrlPattern
        .replaceAll('%CSN%', Uri.encodeComponent(csn));

    _navCompleter = Completer<void>();
    try {
      await _ctrl!.loadUrl(urlRequest: URLRequest(url: WebUri(url)));
    } catch (e) {
      return null;
    }
    try {
      await _navCompleter!.future.timeout(const Duration(seconds: 20));
    } on TimeoutException {
      return {'error': 'nav-timeout'};
    }

    // Settle delay — Stanford's JS framework needs time to render the Past
    // Visit Details page with its tabs after the HTML loads. 4s is the
    // tested-safe minimum from the Chrome MCP runs.
    await Future.delayed(const Duration(seconds: 4));

    // Detect session death BEFORE wasting an inject attempt. If we got
    // redirected to login or the home page, our URL no longer contains
    // 'after-visit-summary'. The whole batch should bail in this case.
    final landedUri = await _ctrl!.getUrl();
    final landedStr = landedUri?.toString() ?? '';
    if (!landedStr.contains('after-visit-summary')) {
      return {'error': 'session-dead-landed-at:${_truncForLog(landedStr)}', 'sessionDead': true};
    }

    _scrapeCompleter = Completer<Map<String, dynamic>>();
    try {
      await _ctrl!.evaluateJavascript(source: ScrapeJobs.stanfordSingleNote());
    } catch (e) {
      return {'error': 'inject-failed: $e'};
    }
    try {
      return await _scrapeCompleter!.future.timeout(const Duration(seconds: 25));
    } on TimeoutException {
      return {'error': 'scrape-timeout'};
    }
  }

  static String _truncForLog(String s) {
    // Keep the path but drop everything after csn= (which is identifying).
    final i = s.indexOf('csn=');
    return i >= 0 ? '${s.substring(0, i)}csn=…' : (s.length > 80 ? '${s.substring(0, 80)}…' : s);
  }

  // ── Keepalive (Dart-side safety net) ───────────────────────────────
  // EXPERIMENT 2026-06-13: keepalive disabled entirely. Working hypothesis:
  // injected keepalive fetches are themselves what Stanford's anti-abuse
  // flags. If this batch completes without keepalive, we re-enable with a
  // different mechanism (e.g., let the page's own JS handle it via clicks).
  void _startKeepalive() {
    _keepaliveTimer?.cancel();
    _keepalivePings = 0;
    // No-op — see comment above.
  }

  void _stopKeepalive() {
    _keepaliveTimer?.cancel();
    _keepaliveTimer = null;
  }

  // ── CSN list source (iteration 3 will replace with portal-driven discovery) ──
  Future<List<String>> _loadCsnList() async {
    try {
      final raw = await rootBundle.loadString('assets/stanford-v3-visits.json');
      final data = jsonDecode(raw) as Map<String, dynamic>;
      final visits = (data['visits'] as List?) ?? const [];
      final csns = <String>[];
      for (final v in visits) {
        if (v is! Map) continue;
        final item = v['item'] as Map?;
        final resp = v['response'] as Map?;
        // Only visits with shareable notes
        final notesInfo = resp?['notesInfo'] as Map?;
        final shareable = notesInfo?['isAtLeastOneNoteShareable'] == true;
        final reportId = (notesInfo?['notesReport'] as Map?)?['reportID'];
        if (!shareable || reportId == null) continue;
        final csn = (item?['Csn'] ?? resp?['csn'])?.toString();
        if (csn != null && csn.isNotEmpty) csns.add(csn);
      }
      return csns;
    } catch (_) {
      return const [];
    }
  }
}
