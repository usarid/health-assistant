import 'dart:async';
import 'dart:convert';
import 'dart:math' show Random;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import '../scrape/scrape_jobs.dart';
import '../scrape/stanford_config.dart';
import '../storage/credentials_store.dart';
import '../storage/local_writer.dart';

/// Scrape screen — batch loop over Stanford visits.
///
///   - Loads the Stanford CSN list from assets/stanford-v3-visits.json
///     (filtered to visits with shareable notes — typically ~106 entries).
///   - Loops over each CSN: host-driven controller.loadUrl → await
///     onLoadStop → inject scraper → await saveNote callback → persist.
///   - Persists each note (or multi-note bundle) to its own JSON
///     immediately (crash-safe), updates a manifest, and writes a
///     consolidated JSON at the end.
///   - Surfaces live progress: index, captured (real), empty, errors.
///
/// No keepalive: C-022 confirmed Stanford fingerprints injected keepalive
/// fetches as a bot signature and revokes the session. Natural per-visit
/// navigation cadence (1 visit per ~10-15s) keeps the session warm
/// without any synthetic pings.
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
  // CSN currently being scraped — surfaces in every noteDiag line so the
  // post-run JSONL ties emits back to a specific visit without anyone
  // needing to correlate timestamps.
  String? _currentCsn;


  // Diagnostics: off by default (clean UX). User toggles via overflow menu
  // when they need to capture a screenshot for support. Resets on app
  // restart — that's fine; this is a "turn it on, reproduce, screenshot,
  // send" tool, not a persistent setting.
  bool _showDiagnostics = false;
  // Holds the most recent diagnostic line so the diag area survives
  // setState rebuilds that don't relate to it.
  String _diagLine = '';


  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('BinaHealth'),
        backgroundColor: Colors.teal,
        foregroundColor: Colors.white,
        actions: [
          PopupMenuButton<String>(
            onSelected: _onMenuSelected,
            itemBuilder: (_) => [
              const PopupMenuItem(value: 'forget-login', child: Text('Forget saved login')),
              PopupMenuItem(
                value: 'toggle-diagnostics',
                child: Text(_showDiagnostics ? 'Hide diagnostics' : 'Show diagnostics'),
              ),
              const PopupMenuItem(value: 'retry-failures', child: Text('Retry failed visits')),
            ],
          ),
        ],
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
                      _buildProgressLine(),
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
                if (_showDiagnostics && _diagLine.isNotEmpty)
                  Container(
                    margin: const EdgeInsets.only(top: 6),
                    padding: const EdgeInsets.symmetric(vertical: 4, horizontal: 8),
                    decoration: BoxDecoration(
                      color: Colors.black.withValues(alpha: 0.06),
                      borderRadius: BorderRadius.circular(4),
                    ),
                    child: Text(
                      'diag: $_diagLine',
                      style: const TextStyle(fontSize: 10, color: Colors.black87, fontFamily: 'monospace'),
                    ),
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
                c.addJavaScriptHandler(
                  handlerName: 'capturedCredentials',
                  callback: _onCapturedCredentialsHandler,
                );
                c.addJavaScriptHandler(
                  handlerName: 'loginDiag',
                  callback: _onLoginDiagHandler,
                );
                c.addJavaScriptHandler(
                  handlerName: 'noteDiag',
                  callback: _onNoteDiagHandler,
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
                // If this looks like a login page (not signed in yet), inject
                // the autofill + capture hook with any stored credentials.
                // Stanford's login is an Angular SPA — the form fields render
                // a beat AFTER onLoadStop, so the injected JS sets up a
                // MutationObserver to wire when fields appear.
                if (!onSignedIn && !_batchRunning) {
                  await _wireLoginPageIfPresent();
                }
                // Also probe again on the /signedin/ first-landing — some
                // Epic flows go signedin → re-login if the session was stale.
                if (onSignedIn && !_batchRunning) {
                  // Defer slightly; if the session is good this is a no-op.
                  Future.delayed(const Duration(milliseconds: 800), _wireLoginPageIfPresent);
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

  /// Status-line builder. Splits the errors bucket into truly-empty
  /// (Stanford-confirmed no-notes visits — not worth showing as "errors"
  /// to the user; they're not a problem to fix) and real errors (which
  /// might be transient and worth retrying).
  static const _emptyReasons = {'no-notes-available', 'no-notes-tab'};
  String _buildProgressLine() {
    final real = _captured.where((c) => c.visibleTextLength > 100).length;
    final empty = _errors.where((e) => _emptyReasons.contains(e.reason)).length;
    final realErrors = _errors.length - empty;
    final parts = [
      '$_batchIndex/$_batchTotal',
      'captured ${_captured.length} ($real real)',
      if (empty > 0) 'empty $empty',
      if (realErrors > 0) 'errors $realErrors',
    ];
    return parts.join(' • ');
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

  /// Dev/diagnostic handler — JS injection emits wiring + capture trace.
  /// Always logs to console (debugPrint). Surfaces to the UI's diag strip
  /// only when the user has flipped Show diagnostics in the overflow menu
  /// — for capturing screenshots of failures to send back for support.
  Future<dynamic> _onLoginDiagHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false};
    final m = Map<String, dynamic>.from(args.first as Map);
    debugPrint('[bina loginDiag] $m');
    if (_showDiagnostics) {
      final stage = m['stage']?.toString() ?? '?';
      String summary;
      if (stage == 'wired') {
        summary = 'wired email=${m["hasEmail"]} pw=${m["hasPassword"]} '
            'signIn=${m["hasSignIn"]} form=${m["hasForm"]}';
      } else if (stage == 'capture-fired') {
        summary = 'capture-fired via ${m["via"]} '
            '(email-len=${m["emailLen"]} pw-len=${m["passwordLen"]})';
      } else {
        summary = '$stage: $m';
      }
      _updateDiag(summary);
    }
    return {'ok': true};
  }

  /// Helper: only does the setState if the user has diagnostics on. Use
  /// for cred handler progress and any other internal handler telemetry.
  void _updateDiag(String line) {
    debugPrint('[bina diag] $line');
    if (!_showDiagnostics) return;
    if (mounted) setState(() => _diagLine = line);
  }

  /// Diagnostic stream from the multi-note scrape loop. Each call corresponds
  /// to one stage (mode-detected, list-entered, pre-click, post-click,
  /// post-back, list-done, iter-aborted). Always console-logged AND
  /// appended to a per-batch JSONL file (so post-run inspection doesn't
  /// require live screenshots). Surfaced to the UI's diag strip only when
  /// [_showDiagnostics] is on.
  Future<dynamic> _onNoteDiagHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false};
    final m = Map<String, dynamic>.from(args.first as Map);
    final stage = m['stage']?.toString() ?? '?';
    debugPrint('[bina noteDiag] $stage $m');
    if (_batchStartedAt != null) {
      final stamped = Map<String, dynamic>.from(m);
      stamped['csn'] = _currentCsn;
      stamped['batchIndex'] = _batchIndex;
      // Fire-and-forget — never block JS callback on file IO
      LocalWriter.appendDiagLine(_batchStartedAt!, stamped);
    }
    if (!_showDiagnostics) return {'ok': true};
    // Compact one-liner — order key fields by stage so the most relevant
    // bits show up first.
    String summary;
    if (stage == 'pre-click' || stage == 'post-click') {
      summary = '$stage i=${m["i"]} url=${m["urlPath"] ?? ""} '
          'sec=${m["sectionLen"]}${stage == "post-click" ? " changed=${m["sectionChanged"]} cap=${m["capturedLen"]}" : ""}';
    } else if (stage == 'post-back') {
      summary = 'post-back i=${m["i"]} via=${m["method"]} '
          'url=${m["urlPath"] ?? ""} btns=${m["buttonsNow"]}';
    } else if (stage == 'list-entered') {
      summary = 'list-entered (${m["buttonCount"]} buttons)';
    } else if (stage == 'mode-detected') {
      summary = 'mode=${m["mode"]} after ${m["elapsedMs"]}ms';
    } else if (stage == 'iter-start') {
      summary = 'iter-start i=${m["i"]} btns=${m["btnsAvailable"]}';
    } else if (stage == 'list-done') {
      summary = 'list-done captured=${m["capturedCount"]} empty=${m["emptyCount"]}';
    } else {
      summary = '$stage: $m';
    }
    if (mounted) setState(() => _diagLine = summary);
    return {'ok': true};
  }

  Future<void> _onMenuSelected(String value) async {
    if (value == 'forget-login') {
      final exists = await CredentialsStore.has('stanford');
      if (!exists) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('No saved Stanford login on this device.'),
          duration: Duration(seconds: 3),
        ));
        return;
      }
      await CredentialsStore.clear('stanford');
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('Saved Stanford login cleared from this device.'),
        duration: Duration(seconds: 3),
      ));
    } else if (value == 'toggle-diagnostics') {
      setState(() {
        _showDiagnostics = !_showDiagnostics;
        if (!_showDiagnostics) _diagLine = '';
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(_showDiagnostics
            ? 'Diagnostics on. Trace appears below the status line.'
            : 'Diagnostics off.'),
        duration: const Duration(seconds: 2),
      ));
    } else if (value == 'retry-failures') {
      // Aggregates failed + partially-captured CSNs across ALL prior
      // batches — so retry catches both never-worked visits AND multi-note
      // visits that captured some sub-notes but missed others.
      final failed = await LocalWriter.findIncompleteCsns();
      if (!mounted) return;
      if (failed.isEmpty) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('No incomplete visits across prior batches — '
              'run "Scrape all" first.'),
          duration: Duration(seconds: 3),
        ));
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('Retrying ${failed.length} incomplete visits…'),
        duration: const Duration(seconds: 3),
      ));
      await _scrapeAll(overrideCsns: failed);
    }
  }

  /// Called from injected JS when the Sign In button is tapped. Captures
  /// the typed-or-autofilled credentials and, IF they're different from
  /// (or absent in) what we have stored, asks the user via Dialog.
  Future<dynamic> _onCapturedCredentialsHandler(List<dynamic> args) async {
    _updateDiag('cred handler: entered');
    if (args.isEmpty || args.first is! Map) {
      _updateDiag('cred handler: bad args');
      return {'ok': false};
    }
    final m = Map<String, dynamic>.from(args.first as Map);
    final portal = (m['portal'] ?? 'stanford').toString();
    final email = (m['email'] ?? '').toString();
    final password = (m['password'] ?? '').toString();
    final wasAutofilled = m['wasAutofilled'] == true;
    _updateDiag('cred handler: portal=$portal email-len=${email.length} '
        'pw-len=${password.length} autofilled=$wasAutofilled');
    if (email.isEmpty || password.isEmpty) return {'ok': false};

    // Autofilled-and-unchanged → user signing in with the saved creds.
    // Skip the prompt silently.
    if (wasAutofilled) {
      try {
        final existing = await CredentialsStore.read(portal);
        if (existing != null && existing.email == email && existing.password == password) {
          _updateDiag('cred handler: already saved, noop');
          return {'ok': true, 'action': 'noop'};
        }
      } catch (e) {
        _updateDiag('cred handler: Keychain read err: ${e.toString().substring(0, 40)}');
      }
    }

    if (!mounted) return {'ok': false};

    // Modal Dialog (not SnackBar — SnackBars on iOS WebView pages can be
    // dismissed by intervening navigation; Dialogs sit on top of the
    // navigator stack).
    final shouldSave = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (dialogCtx) => AlertDialog(
        title: const Text('Save Stanford login?'),
        content: const Text(
          'Store the email + password on this device (iOS Keychain) so '
          'next time you can skip typing them.\n\n'
          'They never leave this phone. Tap "Forget saved login" in the '
          'menu later to remove them.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogCtx).pop(false),
            child: const Text('Not now'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialogCtx).pop(true),
            child: const Text('Save'),
          ),
        ],
      ),
    );

    if (shouldSave == true) {
      try {
        await CredentialsStore.save(portal: portal, email: email, password: password);
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Saved. Will autofill next time.'),
            duration: Duration(seconds: 3),
          ));
        }
      } catch (e) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text('Could not save: $e'),
            duration: const Duration(seconds: 5),
          ));
        }
      }
    }
    return {'ok': true};
  }

  /// Inject the autofill + capture script if the current page looks like
  /// a login form. The JS itself short-circuits to 'no-login-fields' when
  /// no inputs are found, so calling it on non-login pages is safe.
  Future<void> _wireLoginPageIfPresent() async {
    final ctrl = _ctrl;
    if (ctrl == null) return;
    const portal = 'stanford';
    final cred = await CredentialsStore.read(portal);
    final js = ScrapeJobs.loginAutofillAndCapture(
      autofillEmail: cred?.email,
      autofillPassword: cred?.password,
    );
    try {
      await ctrl.evaluateJavascript(source: js);
    } catch (_) {
      // Page may have already navigated by the time JS runs — fine.
    }
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
  /// Scrape every visit in [overrideCsns] if provided; otherwise load the
  /// full visit list from the bundled asset. Used by both the primary
  /// "Scrape all" FAB and the "Retry failed visits" menu action.
  Future<void> _scrapeAll({List<String>? overrideCsns}) async {
    if (_batchRunning) return;
    final csns = overrideCsns ?? await _loadCsnList();
    if (csns.isEmpty) {
      setState(() => _status = overrideCsns != null
          ? 'No failed visits to retry.'
          : 'Could not load CSN list from assets/stanford-v3-visits.json');
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
      _status = 'Scraping ${csns.length} visits…';
    });

    final rng = Random();
    try {
      for (int i = 0; i < csns.length; i++) {
        if (_abortRequested) {
          setState(() => _status = 'Aborted at $i/${csns.length}');
          break;
        }
        final csn = csns[i];
        _currentCsn = csn;
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
          final rawNotes = res['notes'];
          if (err != null && err.isNotEmpty) {
            _errors.add(ScrapeError(csn: csn, index: i, reason: err, at: DateTime.now()));
          } else if (rawNotes is List && rawNotes.isNotEmpty) {
            // Multi-note (list-view) visit — array of {label, html, htmlLength}
            final subs = <SubNote>[];
            for (final n in rawNotes) {
              if (n is! Map) continue;
              final label = (n['label'] ?? '').toString();
              final subHtml = (n['html'] ?? '').toString();
              final subLen = (n['htmlLength'] is int)
                  ? n['htmlLength'] as int
                  : subHtml.length;
              final plainSub = subHtml.replaceAll(RegExp(r'<[^>]+>'), '').trim();
              subs.add(SubNote(
                label: label,
                html: subHtml,
                htmlLength: subLen,
                visibleTextLength: plainSub.length,
              ));
            }
            final aggHtml = subs.fold<int>(0, (a, s) => a + s.htmlLength);
            final aggText = subs.fold<int>(0, (a, s) => a + s.visibleTextLength);
            _captured.add(CapturedNote(
              csn: csn,
              html: '',
              htmlLength: aggHtml,
              visibleTextLength: aggText,
              capturedAt: DateTime.now(),
              subNotes: subs,
            ));
            await LocalWriter.writeMultiNote(csn, subs);
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
      final real = _captured.where((c) => c.visibleTextLength > 100).length;
      final empty = _errors.where((e) => _emptyReasons.contains(e.reason)).length;
      final realErrors = _errors.length - empty;
      final tail = StringBuffer()
        ..write('${_captured.length} captured ($real real)');
      if (empty > 0) tail.write(', $empty empty');
      if (realErrors > 0) tail.write(', $realErrors errors');
      setState(() => _status = 'DONE in ${dur.inSeconds}s — $tail → $path');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// One iteration of the loop. Returns null if navigation never settled;
  /// otherwise the saveNote handler's payload — single-note shape
  /// `{html, error?}` or multi-note shape `{html: '', notes: [...]}`.
  ///
  /// Pacing notes (2026-06-13 finding): Stanford rate-limits rapid-fire
  /// navigation. ~2s/visit triggered session revocation after ~20s. We
  /// pace conservatively: 4s settle + 3-7s jittered between visits.
  /// Multi-note visits take longer (each VIEW NOTE click inside the JS
  /// adds ~5-10s); the per-visit timeout below covers them.
  ///
  /// No retry: every error in the 2026-06-13 9-failure run was structural
  /// (list-view, fixed by the multi-note JS branch), not transient. If a
  /// genuinely slow-render case appears later, narrow retry can come back.
  Future<Map<String, dynamic>?> _scrapeOneVisit(String csn) async {
    final url = StanfordConfig.visitDetailUrlPattern
        .replaceAll('%CSN%', Uri.encodeComponent(csn));

    // Settle was 4s historically (worked but conservative). The JS itself
    // polls for either VIEW NOTE buttons or .pgSection content, so 2s is
    // enough lead for the SPA to mount the Clinical Notes tab before we
    // inject. Saves ~2s on every visit (~200s across a 100-visit run).
    const settleSeconds = 2;
    // Mode-detection poll inside JS: was 15s. Successful visits resolve
    // in <2s; the only thing 15s bought was waiting on truly-empty pages
    // to finally confirm there's no content. 5s is plenty — if neither
    // buttons nor inline text show up by then, the page is empty. Saves
    // ~10s on each mode-timeout visit.
    const pollMs = 5000;

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

    await Future.delayed(const Duration(seconds: settleSeconds));

    // Detect session death BEFORE wasting an inject attempt.
    final landedUri = await _ctrl!.getUrl();
    final landedStr = landedUri?.toString() ?? '';
    if (!landedStr.contains('after-visit-summary')) {
      return {'error': 'session-dead-landed-at:${_truncForLog(landedStr)}', 'sessionDead': true};
    }

    _scrapeCompleter = Completer<Map<String, dynamic>>();
    try {
      await _ctrl!.evaluateJavascript(source: ScrapeJobs.stanfordSingleNote(pollMs: pollMs));
    } catch (e) {
      return {'error': 'inject-failed: $e'};
    }
    // Multi-note visits iterate VIEW NOTE buttons inside the JS — each
    // adds ~5-10s of polling. Allow up to 3 min total for a 20-note
    // hospital-stay visit; covers everything we've seen in v3 visits.
    try {
      return await _scrapeCompleter!.future.timeout(const Duration(minutes: 3));
    } on TimeoutException {
      return {'error': 'scrape-timeout'};
    }
  }

  static String _truncForLog(String s) {
    // Keep the path but drop everything after csn= (which is identifying).
    final i = s.indexOf('csn=');
    return i >= 0 ? '${s.substring(0, i)}csn=…' : (s.length > 80 ? '${s.substring(0, 80)}…' : s);
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
