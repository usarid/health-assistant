import 'dart:async';
import 'dart:collection' show Queue, UnmodifiableListView;
import 'dart:convert';
import 'dart:math' show Random;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show rootBundle, Clipboard;
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import '../portal/portal_registry.dart';
import '../scrape/scrape_jobs.dart';
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

  /// Currently-active portal (values only — URLs, hostnames, CSRF field
  /// names). Meaty scraping logic lives in scrape_jobs.dart / the
  /// pipeline methods on this class; anything portal-specific in there
  /// still refers to Stanford hostnames inline (R-2 territory).
  PortalConfig get _portal => PortalRegistry.instance.active;

  String _status = 'Log into ${PortalRegistry.instance.active.name}';
  String _currentUrl = '';
  bool _onSignedInPage = false;
  bool _batchRunning = false;
  bool _abortRequested = false;

  // Completers that bridge JS lifecycle events into Dart's async/await
  Completer<void>? _navCompleter;
  Completer<Map<String, dynamic>>? _scrapeCompleter;
  Completer<Map<String, dynamic>>? _messageListCompleter;
  Completer<Map<String, dynamic>>? _messageDetailCompleter;
  Completer<Map<String, dynamic>>? _labDetailCompleter;
  Completer<Map<String, dynamic>>? _clinicalListCompleter;

  // Batch state
  int _batchTotal = 0;
  int _batchIndex = 0;
  final List<CapturedNote> _captured = [];
  final List<ScrapeError> _errors = [];
  DateTime? _batchStartedAt;

  // Portal-scout state
  // Default OFF post-discovery (2026-06-25): the v1.12 scout produced the
  // Stanford spec we needed; routine sign-ins shouldn't spend 3 min
  // re-scouting. Toggle on via menu when re-mapping a new portal or
  // checking for portal changes.
  bool _autoScoutOnSignIn = false;
  bool _scoutRanThisSession = false; // guards against re-fire on session re-login

  // When true (default), lab/message body fetches skip items that are
  // already on disk. Toggled OFF by the "Refetch everything (ignore
  // cache)" menu item — useful after Stanford issues an addendum /
  // correction, or when we need a full re-pull for debugging.
  bool _incrementalScrape = true;

  // After-auth scrape ORCHESTRATOR — runs every known per-section
  // scraper in sequence as soon as the user is signed in. The user's
  // explicit principle: routine ingest should never require a menu tap,
  // only authenticate-and-wait. See [feedback-auto-run-after-auth].
  bool _autoOrchestrateOnSignIn = true;
  bool _orchestratorRanThisSession = false;
  bool _orchestratorRunning = false;
  Timer? _orchestratorAutoFireTimer;
  Timer? _orchestratorAutoRepollTimer;
  // Auto-fire is gated on a per-tick DOM probe (signed-in indicators
  // present; no password input visible). The 8s countdown gives the
  // user buffer to interrupt; after every defer we re-poll every 10s
  // so a slow MFA completion doesn't strand the user with "nothing
  // happening" when they DO finish signing in.
  Timer? _scoutAutoFireTimer;
  Timer? _scoutAutoRepollTimer;
  static const Duration _scoutAutoFireDelay  = Duration(seconds: 8);
  static const Duration _scoutAutoRepollWait = Duration(seconds: 10);
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
  void dispose() {
    _scoutAutoFireTimer?.cancel();
    _scoutAutoRepollTimer?.cancel();
    _orchestratorAutoFireTimer?.cancel();
    _orchestratorAutoRepollTimer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('BinaHealth · ${_portal.name}'),
        backgroundColor: Colors.teal,
        foregroundColor: Colors.white,
        actions: [
          PopupMenuButton<String>(
            onSelected: _onMenuSelected,
            // Cap the menu height so it scrolls (via PopupMenu's internal
            // SingleChildScrollView) instead of being clipped when the item
            // list is taller than the space below the app bar.
            constraints: const BoxConstraints(
              minWidth: 260, maxWidth: 360, maxHeight: 520,
            ),
            itemBuilder: (_) => [
              // Everyday recovery — top so always reachable.
              PopupMenuItem(value: 'go-to-login', child: Text('Go to ${_portal.name} login')),
              const PopupMenuItem(value: 'paste-into-focused', child: Text('Paste into focused field')),
              if (PortalRegistry.instance.all.length > 1)
                const PopupMenuItem(value: 'switch-portal', child: Text('Switch portal…')),
              const PopupMenuItem(value: 'probe-endpoints', child: Text('Probe UCSF endpoints')),
              const PopupMenuDivider(),

              // Scraping.
              const PopupMenuItem(value: 'run-full-scrape', child: Text('Run full scrape now')),
              const PopupMenuItem(value: 'refetch-everything', child: Text('Refetch everything (ignore cache)')),
              const PopupMenuItem(value: 'retry-failures', child: Text('Retry failed visits')),
              const PopupMenuDivider(),

              // Toggles.
              PopupMenuItem(
                value: 'toggle-auto-orchestrate',
                child: Text(_autoOrchestrateOnSignIn
                    ? 'Disable auto-scrape on sign-in'
                    : 'Enable auto-scrape on sign-in'),
              ),
              PopupMenuItem(
                value: 'toggle-auto-scout',
                child: Text(_autoScoutOnSignIn
                    ? 'Disable auto-scout on sign-in'
                    : 'Enable auto-scout on sign-in'),
              ),
              PopupMenuItem(
                value: 'toggle-diagnostics',
                child: Text(_showDiagnostics ? 'Hide diagnostics' : 'Show diagnostics'),
              ),
              const PopupMenuDivider(),

              // Advanced.
              const PopupMenuItem(value: 'run-portal-scout', child: Text('Run portal scout now')),
              const PopupMenuItem(value: 'forget-login', child: Text('Forget saved login')),
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
              initialUrlRequest: URLRequest(url: WebUri(_portal.urls.login)),
              initialSettings: InAppWebViewSettings(
                javaScriptEnabled: true,
                userAgent: _portal.userAgent,
              ),
              // Portal scout bootstrap — runs at document-start in every
              // frame (top wrapper + cross-origin iframes alike) so the
              // api-capture hooks and the enumeration RPC handler are wired
              // before any page script can fire. Without forMainFrameOnly:
              // false the cross-origin iframe content (mychart.shc Epic
              // page) is invisible to the scout — exactly what tripped v1.1.
              initialUserScripts: UnmodifiableListView<UserScript>([
                UserScript(
                  source: ScrapeJobs.bootstrapForUserScript(),
                  injectionTime: UserScriptInjectionTime.AT_DOCUMENT_START,
                  forMainFrameOnly: false,
                ),
              ]),
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
                c.addJavaScriptHandler(
                  handlerName: 'messageList',
                  callback: _onMessageListHandler,
                );
                c.addJavaScriptHandler(
                  handlerName: 'messageDetail',
                  callback: _onMessageDetailHandler,
                );
                c.addJavaScriptHandler(
                  handlerName: 'labDetail',
                  callback: _onLabDetailHandler,
                );
                c.addJavaScriptHandler(
                  handlerName: 'clinicalList',
                  callback: _onClinicalListHandler,
                );
                c.addJavaScriptHandler(
                  handlerName: 'probeCapture',
                  callback: _onProbeCaptureHandler,
                );
                c.addJavaScriptHandler(
                  handlerName: 'labsList',
                  callback: _onLabsListHandler,
                );
              },
              onLoadStop: (c, url) async {
                final urlStr = url?.toString() ?? '';
                final onSignedIn = urlStr.contains(_portal.urls.signedInMarker);
                setState(() {
                  _currentUrl = urlStr;
                  _onSignedInPage = onSignedIn;
                  if (onSignedIn && !_batchRunning && !_status.startsWith('Scrape') && !_status.startsWith('Scout') && !_status.startsWith('Auto-scout')) {
                    _status = 'Logged in.';
                  }
                });
                // Resolve a pending navigation if this is the URL we asked for
                if (_navCompleter != null && _navCompleter!.isCompleted == false) {
                  _navCompleter!.complete();
                }
                // Auto-scout fires once per session, AFTER the URL has
                // been stable on /signedin/home for _scoutAutoFireDelay
                // seconds. We restart the countdown on every onLoadStop —
                // any nav (MFA redirect, route bounce, manual nav) resets
                // it, so the scout only fires when the user is genuinely
                // parked on home. v1.4 (fire 2s after first /signedin/
                // touch) interrupted MFA entry; this stability gate avoids
                // that whole failure class.
                // Cancel any running timers — onLoadStop is the canonical
                // "things might have changed" signal. We re-arm below if
                // auto-scout is enabled and we look like we're on a
                // signed-in URL. The DOM probe at fire time decides
                // whether the page is REALLY signed in.
                _scoutAutoFireTimer?.cancel();      _scoutAutoFireTimer = null;
                _scoutAutoRepollTimer?.cancel();    _scoutAutoRepollTimer = null;
                if (onSignedIn && _autoScoutOnSignIn && !_scoutRanThisSession && !_batchRunning) {
                  _armScoutAutoFireCountdown();
                }
                // After-auth full-scrape orchestrator — same stability
                // gate + pre-fire DOM probe pattern as the scout (no
                // interrupting MFA/credentials entry), but fires the
                // whole ingest pipeline instead.
                _orchestratorAutoFireTimer?.cancel();    _orchestratorAutoFireTimer = null;
                _orchestratorAutoRepollTimer?.cancel();  _orchestratorAutoRepollTimer = null;
                if (onSignedIn && _autoOrchestrateOnSignIn && !_orchestratorRanThisSession
                    && !_batchRunning && !_orchestratorRunning) {
                  _armOrchestratorCountdown();
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
    // No FAB when not running — auto-scout handles the canonical path
    // and the overflow menu carries the manual triggers (Test one /
    // Scrape all / Discover messages / Run portal scout now / etc.).
    return null;
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

  /// JS handler for one scraped message-list page (inbox or outbox).
  /// Completes [_messageListCompleter] with the payload so the discovery
  /// loop can collect rows and decide pagination.
  Future<dynamic> _onMessageListHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false};
    final m = Map<String, dynamic>.from(args.first as Map);
    if (_messageListCompleter != null && !_messageListCompleter!.isCompleted) {
      _messageListCompleter!.complete(m);
    }
    return {'ok': true};
  }

  /// JS handler for one fetched message body. Completes
  /// [_messageDetailCompleter] with the full payload (endpoint,
  /// attempts, the message data itself, plus diagnostics).
  Future<dynamic> _onMessageDetailHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false};
    final m = Map<String, dynamic>.from(args.first as Map);
    if (_messageDetailCompleter != null && !_messageDetailCompleter!.isCompleted) {
      _messageDetailCompleter!.complete(m);
    }
    return {'ok': true};
  }

  /// JS handler for one fetched lab/imaging result via GetDetails (Phase 4-2).
  /// Completes [_labDetailCompleter] with {eorderid, ok, status, details, attempts, timings}.
  Future<dynamic> _onLabDetailHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false};
    final m = Map<String, dynamic>.from(args.first as Map);
    if (_labDetailCompleter != null && !_labDetailCompleter!.isCompleted) {
      _labDetailCompleter!.complete(m);
    }
    return {'ok': true};
  }

  /// JS handler for one fetched clinical-list (Allergies / HealthIssues /
  /// Immunizations / …). Completes [_clinicalListCompleter] with
  /// {section, ok, status, list, attempts, timings}.
  Future<dynamic> _onClinicalListHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false};
    final m = Map<String, dynamic>.from(args.first as Map);
    if (_clinicalListCompleter != null && !_clinicalListCompleter!.isCompleted) {
      _clinicalListCompleter!.complete(m);
    }
    return {'ok': true};
  }

  Completer<Map<String, dynamic>>? _probeCaptureCompleter;
  Future<dynamic> _onProbeCaptureHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false};
    final m = Map<String, dynamic>.from(args.first as Map);
    if (_probeCaptureCompleter != null && !_probeCaptureCompleter!.isCompleted) {
      _probeCaptureCompleter!.complete(m);
    }
    return {'ok': true};
  }

  Completer<Map<String, dynamic>>? _labsListCompleter;
  Future<dynamic> _onLabsListHandler(List<dynamic> args) async {
    if (args.isEmpty || args.first is! Map) return {'ok': false};
    final m = Map<String, dynamic>.from(args.first as Map);
    if (_labsListCompleter != null && !_labsListCompleter!.isCompleted) {
      _labsListCompleter!.complete(m);
    }
    return {'ok': true};
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
      LocalWriter.appendDiagLine(portalId: _portal.id, batchStartedAt: _batchStartedAt!, event: stamped);
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

  /// Start the visible countdown that ends in a verify-then-fire call.
  /// Cancellable via onLoadStop (rearm) or the menu toggle (disable).
  void _armScoutAutoFireCountdown() {
    _scoutAutoFireTimer?.cancel();
    final fireAt = DateTime.now().add(_scoutAutoFireDelay);
    setState(() => _status =
        'Auto-scout in ${_scoutAutoFireDelay.inSeconds}s (use menu to defer)');
    _scoutAutoFireTimer = Timer.periodic(const Duration(seconds: 1), (t) {
      final remaining = fireAt.difference(DateTime.now()).inSeconds;
      if (remaining <= 0) {
        t.cancel();
        _scoutAutoFireTimer = null;
        if (!mounted || _batchRunning || _ctrl == null) return;
        _verifyAndFireScout();
      } else if (mounted && !_batchRunning) {
        setState(() =>
            _status = 'Auto-scout in ${remaining}s (use menu to defer)');
      }
    });
  }

  /// Verify the live page DOM is signed-in BEFORE kicking off the scout.
  /// If not (still sees password input or no Logout link), defer + schedule
  /// a re-probe in _scoutAutoRepollWait so a slow MFA completion eventually
  /// triggers the scout without needing another onLoadStop.
  Future<void> _verifyAndFireScout() async {
    if (_ctrl == null) return;
    try {
      final probeRaw = await _ctrl!.evaluateJavascript(source: r'''
        JSON.stringify({
          url: location.href,
          hasPasswordInput: !!document.querySelector('input[type="password"]:not([disabled])'),
          // URL is the primary signed-in signal (checked Dart-side
          // against _portal.urls.signedInMarker). No DOM logout probe —
          // that was Stanford-flavored guesswork that broke on UCSF's
          // avatar-menu-hidden logout. hasPasswordInput above is the
          // one remaining DOM defense: catches SPA route-bounces where
          // the URL says signed-in but the login form is momentarily
          // still rendered.
          bodyHead: (document.body && document.body.innerText || '')
            .replace(/\s+/g, ' ').slice(0, 200),
        })
      ''');
      final probe = jsonDecode(probeRaw?.toString() ?? '{}');
      final hasPw     = probe['hasPasswordInput'] == true;
      final liveUrl   = (probe['url'] ?? '').toString();
      final onSigned  = liveUrl.contains(_portal.urls.signedInMarker);
      if (onSigned && !hasPw) {
        _scoutAutoRepollTimer?.cancel();
        _scoutAutoRepollTimer = null;
        _scoutRanThisSession = true;
        await _runPortalScout();
        return;
      }
      final pathOnly = Uri.tryParse(liveUrl)?.path ?? liveUrl;
      setState(() => _status = 'Auto-scout deferred (pw=$hasPw '
          'url=$pathOnly) — will re-check in ${_scoutAutoRepollWait.inSeconds}s');
      // Re-poll loop: keep checking the page state at a slower cadence
      // until the user is genuinely signed in. Cancelled by next
      // onLoadStop (which will rearm via the full countdown), by the
      // menu toggle, or by dispose().
      _scoutAutoRepollTimer?.cancel();
      _scoutAutoRepollTimer = Timer(_scoutAutoRepollWait, () {
        if (!mounted || _batchRunning || !_autoScoutOnSignIn || _scoutRanThisSession) return;
        _verifyAndFireScout();
      });
    } catch (e) {
      setState(() => _status = 'Auto-scout verify failed: $e');
    }
  }

  /// Start the visible countdown before kicking off the full scrape
  /// pipeline. Mirrors _armScoutAutoFireCountdown so the two auto-trigger
  /// paths behave the same — cancellable on every onLoadStop (URL change
  /// rearms), deferrable via menu toggle, and never fires while we're
  /// still on a sign-in screen (the verify probe handles that).
  void _armOrchestratorCountdown() {
    _orchestratorAutoFireTimer?.cancel();
    final fireAt = DateTime.now().add(_scoutAutoFireDelay);
    setState(() => _status =
        'Auto-scrape in ${_scoutAutoFireDelay.inSeconds}s (use menu to defer)');
    _orchestratorAutoFireTimer = Timer.periodic(const Duration(seconds: 1), (t) {
      final remaining = fireAt.difference(DateTime.now()).inSeconds;
      if (remaining <= 0) {
        t.cancel();
        _orchestratorAutoFireTimer = null;
        if (!mounted || _batchRunning || _orchestratorRunning || _ctrl == null) return;
        _verifyAndFireOrchestrator();
      } else if (mounted && !_batchRunning && !_orchestratorRunning) {
        setState(() =>
            _status = 'Auto-scrape in ${remaining}s (use menu to defer)');
      }
    });
  }

  /// Pre-fire DOM probe — same logic as the scout's. Don't fire the
  /// orchestrator (which kicks off long-running sub-fetchers and would
  /// disrupt the user) until we can see a real signed-in DOM.
  Future<void> _verifyAndFireOrchestrator() async {
    if (_ctrl == null) return;
    try {
      final probeRaw = await _ctrl!.evaluateJavascript(source: r'''
        JSON.stringify({
          url: location.href,
          hasPasswordInput: !!document.querySelector('input[type="password"]:not([disabled])'),
          // URL is the primary signed-in signal (checked Dart-side
          // against _portal.urls.signedInMarker). No DOM logout probe —
          // that was Stanford-flavored guesswork that broke on UCSF's
          // avatar-menu-hidden logout. hasPasswordInput above is the
          // one remaining DOM defense: catches SPA route-bounces where
          // the URL says signed-in but the login form is momentarily
          // still rendered.
        })
      ''');
      final probe = jsonDecode(probeRaw?.toString() ?? '{}');
      final hasPw     = probe['hasPasswordInput'] == true;
      final liveUrl   = (probe['url'] ?? '').toString();
      final onSigned  = liveUrl.contains(_portal.urls.signedInMarker);
      if (onSigned && !hasPw) {
        _orchestratorAutoRepollTimer?.cancel();
        _orchestratorAutoRepollTimer = null;
        _orchestratorRanThisSession = true;
        await _runFullScrapePipeline();
        return;
      }
      final pathOnly = Uri.tryParse(liveUrl)?.path ?? liveUrl;
      setState(() => _status = 'Auto-scrape deferred (pw=$hasPw '
          'url=$pathOnly) — will re-check in ${_scoutAutoRepollWait.inSeconds}s');
      _orchestratorAutoRepollTimer?.cancel();
      _orchestratorAutoRepollTimer = Timer(_scoutAutoRepollWait, () {
        if (!mounted || _batchRunning || _orchestratorRunning
            || !_autoOrchestrateOnSignIn || _orchestratorRanThisSession) {
          return;
        }
        _verifyAndFireOrchestrator();
      });
    } catch (e) {
      setState(() => _status = 'Auto-scrape verify failed: $e');
    }
  }

  /// The orchestrator: runs every known per-section scraper in sequence.
  /// Each sub-fetcher writes its results to Documents/ as it has always
  /// done; this method just sequences them. The user's only manual
  /// control is the red Abort FAB (which sets _abortRequested; we check
  /// between tasks). Per-task gating via _batchRunning still works —
  /// each sub-fetcher sets it on entry / clears on finally — and we sit
  /// outside that, with our own _orchestratorRunning flag preventing
  /// re-entry.
  Future<void> _runFullScrapePipeline() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning || _orchestratorRunning) return;
    // Build the pipeline per portal. R-5a portal-scoped LocalWriter so
    // file collisions are no longer a concern; the gate is now about
    // which sub-fetchers have been PROVEN against each portal's actual
    // API surface (via R-5b portal-scout).
    //
    // Clinical triad works uniformly on any Epic MyChart — same
    // `/Clinical/<Section>/LoadListData` shape, only base_path differs
    // (already portal-parameterized in R-2). Verified for UCSF via
    // scout 2026-08-13: /UCSFMyChart/Clinical/Allergies/LoadListData
    // returned 200 with the expected JSON shape.
    //
    // The rest (labs, messages, orion, medications-embedded-JS) are
    // Stanford-flavored: labs use a pre-populated eorderid list from a
    // Stanford-only bundled asset; messages use /Private/Ajax/V1/Mailbox/*
    // (UCSF's live surface is /api/conversations/*); orion is a Stanford-
    // specific subsystem UCSF doesn't expose; medications is an embedded-
    // JS-object literal (UCSF has a proper JSON /LoadExternal endpoint
    // instead). Each of those needs its own portal-aware implementation
    // — tracked in follow-up work.
    setState(() {
      _orchestratorRunning = true;
      _abortRequested = false;
      _status = 'Auto-scrape: starting…';
    });
    final portalTag = _portal.name;
    final tasks = <(String, Future<void> Function())>[
      // Portal-agnostic (proven for stanford + ucsf).
      ('Clinical triad (Allergies / Imm / Conds)',    _fetchClinicalTriad),
    ];
    if (_portal.id == 'ucsf') {
      // UCSF's labs use live GetList discovery + per-key GetDetails
      // (no bundled asset like Stanford's stanford-lab-orders.json).
      tasks.insert(0, ('Lab bodies ($portalTag) — live discovery', _fetchLabsFullBatch));
    }
    if (_portal.id == 'stanford') {
      // Stanford-only until each is ported per portal:
      tasks.insertAll(0, [
        ('Lab bodies ($portalTag)',                     _fetchAllLabBodies),
        // Message-body fetch reads a discovery file from disk; discovery
        // HAS to run first on a fresh install.
        ('Message discovery ($portalTag)',              _discoverMessages),
        ('Message bodies ($portalTag)',                 _fetchAllMessageBodies),
      ]);
      tasks.addAll([
        ('Orion endpoints (Procedures / Appointments)', _fetchOrionEndpoints),
        ('Medications ($portalTag)',                    _fetchMedications),
      ]);
    }
    int ok = 0, failed = 0;
    try {
      for (var i = 0; i < tasks.length; i++) {
        if (_abortRequested) {
          setState(() => _status = 'Auto-scrape aborted at task ${i+1}/${tasks.length}');
          break;
        }
        final (name, fn) = tasks[i];
        setState(() => _status = 'Auto-scrape ${i+1}/${tasks.length}: $name');
        try {
          await fn();
          ok++;
        } catch (e) {
          failed++;
          // Best-effort: log to status, then continue to next task.
          setState(() => _status = 'Auto-scrape task "$name" failed: $e');
          await Future.delayed(const Duration(seconds: 2));
        }
      }
      if (!_abortRequested) {
        setState(() => _status = 'Auto-scrape complete: $ok/${tasks.length} OK'
            '${failed > 0 ? " ($failed failed)" : ""}.');
      }
    } finally {
      setState(() => _orchestratorRunning = false);
    }
  }

  /// Fetch Stanford Medications via content-negotiated JSON at the
  /// /Clinical/Medications URL. Same endpoint that serves HTML to
  /// browser navigations returns JSON when Accept: */* is sent (Stanford's
  /// React app takes this branch). Cookie-based CSRF; no token header
  /// needed. Reuses the 'clinicalList' JS handler bridge.
  Future<void> _fetchMedications() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;
    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _batchTotal = 1;
      _batchIndex = 1;
      _status = 'Medications: fetching…';
    });
    try {
      _clinicalListCompleter = Completer<Map<String, dynamic>>();
      try {
        await _ctrl!.evaluateJavascript(source: ScrapeJobs.stanfordMedicationsFetch());
      } catch (e) {
        setState(() => _status = 'Medications: inject failed: $e');
        return;
      }
      Map<String, dynamic> result;
      try {
        result = await _clinicalListCompleter!.future
            .timeout(const Duration(seconds: 30));
      } on TimeoutException {
        setState(() => _status = 'Medications: timeout (30s)');
        return;
      }
      if (result['ok'] == true && result['list'] != null) {
        final path = await LocalWriter.writeClinicalList(portalId: _portal.id, section: 'Medications', listJson: result['list']);
        setState(() => _status = 'Medications: OK → $path');
      } else {
        // Persist failure envelope for diagnostics.
        await LocalWriter.writeClinicalList(portalId: _portal.id, section: 'Medications-failed', listJson: result);
        setState(() => _status = 'Medications: failed (${result['error'] ?? 'unknown'})');
      }
    } catch (e) {
      setState(() => _status = 'Medications: exception: $e');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// Fetch Procedures (surgery/allSurgeries) + Appointments
  /// (futureappointments) — both /orion/public/ajax/v1/* JSON endpoints,
  /// cookie-only auth (no CSRF token like the /Clinical/* endpoints
  /// require). Reuses the `clinicalList` JS handler so the bridge
  /// completer is the same as the clinical triad.
  Future<void> _fetchOrionEndpoints() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;
    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _batchTotal = 2;
      _batchIndex = 0;
      _status = 'Orion endpoints: starting…';
    });
    // (section label, urlPath, method, jsonBody)
    final endpoints = <(String, String, String, String?)>[
      ('Procedures',    '/orion/public/ajax/v1/surgery/allSurgeries',
                        'POST', '{"numOfDays":730}'),
      ('Appointments',  '/orion/public/ajax/v1/appointments/futureappointments',
                        'GET',  null),
    ];
    try {
      for (var i = 0; i < endpoints.length; i++) {
        if (_abortRequested) break;
        final (section, path, method, body) = endpoints[i];
        setState(() {
          _batchIndex = i + 1;
          _status = 'Orion ${i+1}/${endpoints.length}: $section…';
        });
        _clinicalListCompleter = Completer<Map<String, dynamic>>();
        try {
          await _ctrl!.evaluateJavascript(
            source: ScrapeJobs.epicOrionFetch(
              portal: _portal, section: section, urlPath: path, method: method, jsonBody: body),
          );
        } catch (e) {
          continue;
        }
        Map<String, dynamic> result;
        try {
          result = await _clinicalListCompleter!.future
              .timeout(const Duration(seconds: 30));
        } on TimeoutException {
          continue;
        }
        if (result['ok'] == true && result['list'] != null) {
          await LocalWriter.writeClinicalList(portalId: _portal.id, section: section, listJson: result['list']);
        }
      }
      setState(() => _status = 'Orion endpoints done.');
    } catch (e) {
      setState(() => _status = 'Orion endpoints failed: $e');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// Fetch the AllergyIntolerance / Immunization / Condition triad — all
  /// three use the same POST /myhealth_sso/Clinical/<Section>/Load…
  /// pattern with an empty body. Token caching matches the lab fetcher
  /// One-shot: hit a batch of Epic MyChart endpoints on the active
  /// portal and dump raw responses to disk. Used to inspect real
  /// response shapes before writing per-endpoint parsers. Writes
  /// clinical/<portal>-probe-<ts>.json under Documents.
  Future<void> _probeUcsfEndpoints() async {
    if (_ctrl == null) return;
    if (_batchRunning) return;
    setState(() {
      _batchRunning = true;
      _status = 'Probing ${_portal.name} endpoints…';
    });
    try {
      _probeCaptureCompleter = Completer<Map<String, dynamic>>();
      final probes = <Map<String, String>>[
        // Clinical triad — SAME response shape as Stanford (verified 2026-08-13).
        // The `sectionFile` key steers the Dart writer to save these as proper
        // per-section files that convert_mobile_clinical_to_fhir.py picks up.
        {'name': 'allergiesLoadListData',     'method': 'POST', 'path': '/Clinical/Allergies/LoadListData?ComponentNumber=2', 'body': '', 'sectionFile': 'allergies'},
        {'name': 'immunizationsLoadList',     'method': 'POST', 'path': '/Clinical/Immunizations/LoadImmunizationsList?ComponentNumber=2', 'body': '', 'sectionFile': 'immunizations'},
        {'name': 'healthIssuesLoadListData',  'method': 'POST', 'path': '/Clinical/HealthIssues/LoadListData?ComponentNumber=2', 'body': '', 'sectionFile': 'healthissues'},

        // Medications — LoadExternal returns a list of orgs each with a
        // PrescriptionList. Close to Stanford's shape but nested differently.
        {'name': 'medicationsLoadExternal',   'method': 'POST', 'path': '/Clinical/Medications/LoadExternal',     'body': '', 'sectionFile': 'medications'},

        // Visits — LoadPast returns 357 KB of past visits.
        {'name': 'visitsLoadPast',            'method': 'POST', 'path': '/Visits/VisitsList/LoadPast?loadpast=1&ComponentNumber=7&oldestRenderedDate=2020-01-01T00%3A00%3A00.000Z', 'body': '', 'sectionFile': 'visits-past'},
        {'name': 'visitsLoadUpcoming',        'method': 'POST', 'path': '/Visits/VisitsList/LoadUpcoming?timeZone=America%2FLos_Angeles&ComponentNumber=5', 'body': '', 'sectionFile': 'visits-upcoming'},

        // Letters + Care Team — new capabilities UCSF exposes.
        {'name': 'lettersGetLettersList',     'method': 'POST', 'path': '/api/letters/GetLettersList',            'body': '', 'sectionFile': 'letters'},
        {'name': 'careTeamLoad',              'method': 'POST', 'path': '/Clinical/CareTeam/Load?hfrId=&sources=&actions=&isPrimaryStandalone=true&ComponentNumber=2', 'body': '', 'sectionFile': 'careteam'},

        // Labs — GetList 500s without body. Try a few common param shapes.
        // First hit that returns 200 wins for parser design.
        {'name': 'testResultsGetList_empty',   'method': 'POST', 'path': '/api/test-results/GetList', 'body': '{}'},
        {'name': 'testResultsGetList_paged',   'method': 'POST', 'path': '/api/test-results/GetList', 'body': '{"startIndex":0,"count":50}'},
        {'name': 'testResultsGetList_extern',  'method': 'POST', 'path': '/api/test-results/GetList', 'body': '{"includeExternal":true}'},
        {'name': 'testResultsGetCommunityInfo','method':'POST', 'path': '/api/test-results/GetCommunityInfo',     'body': ''},

        // Conversations (messages) — GetConversationList 500s without body.
        // From GetFoldersList we know folders have `tag` values (1..6);
        // typical Epic uses {folder: tag}. Try tag=1 first (usually inbox).
        {'name': 'conversationsGetFoldersList','method':'POST', 'path': '/api/conversations/GetFoldersList',      'body': ''},
        {'name': 'conversationsGetOrganizations','method':'POST','path':'/api/conversations/GetOrganizations',    'body': ''},
        {'name': 'conversationsList_tag1',     'method':'POST', 'path': '/api/conversations/GetConversationList', 'body': '{"folder":1}'},
        {'name': 'conversationsList_folder1',  'method':'POST', 'path': '/api/conversations/GetConversationList', 'body': '{"folderTag":1}'},
        {'name': 'conversationsList_empty',    'method':'POST', 'path': '/api/conversations/GetConversationList', 'body': '{}'},
      ];
      await _ctrl!.evaluateJavascript(
        source: ScrapeJobs.epicProbeEndpoints(portal: _portal, probes: probes));
      final result = await _probeCaptureCompleter!.future.timeout(const Duration(seconds: 120));
      // Full-blob dump (debug + follow-up analysis).
      final rawPath = await LocalWriter.writeClinicalList(
        portalId: _portal.id,
        section: 'probe',
        listJson: result,
      );
      // Per-section files (only for the ones that succeeded + have a
      // sectionFile name). Steers ingest-side scripts at the right data.
      int wroteFiles = 0;
      final stats = result['results'] as Map? ?? {};
      for (final probe in probes) {
        final sectionFile = probe['sectionFile'];
        if (sectionFile == null) continue;
        final r = stats[probe['name']];
        if (r is! Map || r['ok'] != true) continue;
        final sampleStr = r['sample'] as String?;
        if (sampleStr == null || sampleStr.isEmpty) continue;
        try {
          final parsed = jsonDecode(sampleStr);
          await LocalWriter.writeClinicalList(
            portalId: _portal.id,
            section: sectionFile,
            listJson: parsed,
          );
          wroteFiles++;
        } catch (_) { /* JSON parse failed (truncated?); skip */ }
      }
      if (!mounted) return;
      final okCount = stats.values.whereType<Map>().where((v) => v['ok'] == true).length;
      setState(() => _status = 'Probe done: $okCount/${stats.length} ok. Wrote $wroteFiles per-section files. Raw: $rawPath');
    } on TimeoutException {
      if (!mounted) return;
      setState(() => _status = 'Probe timed out (>90s)');
    } catch (e) {
      if (!mounted) return;
      setState(() => _status = 'Probe failed: $e');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// UCSF (and any Epic MyChart without a pre-populated lab-orders
  /// asset) labs fetch: hit /api/test-results/GetList to discover the
  /// keys live, then loop calling epicLabDetail per key. Each
  /// GetDetails response lands via the existing labDetail handler
  /// wiring (LocalWriter.writeLabBody portal-scopes the filename).
  ///
  /// Runs uncapped — no bundled asset, no reason to stop short.
  /// Incremental cache (LocalWriter.hasLabBody) skips per-key on-disk.
  Future<void> _fetchLabsFullBatch() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;

    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _status = 'Discovering ${_portal.name} labs list…';
    });

    // Step 1: discover the flat list of result keys.
    List<String> keys;
    Map<String, dynamic>? rawRes;
    try {
      _labsListCompleter = Completer<Map<String, dynamic>>();
      await _ctrl!.evaluateJavascript(
          source: ScrapeJobs.epicLabsList(portal: _portal));
      rawRes = await _labsListCompleter!.future
          .timeout(const Duration(seconds: 30));
    } on TimeoutException {
      rawRes = {'ok': false, 'error': 'timeout-30s'};
    } catch (e) {
      rawRes = {'ok': false, 'error': 'inject-failed:$e'};
    }

    // Always write the labs-list result to disk so we can debug failures
    // even after the pipeline moves on. Writes to
    // Documents/clinical/<portal>-labs-list-<ts>.json.
    await LocalWriter.writeClinicalList(
      portalId: _portal.id,
      section: 'labs-list',
      listJson: rawRes,
    );

    if (rawRes!['ok'] != true) {
      if (!mounted) return;
      setState(() => _status = 'Labs list failed: ${rawRes!['error']} — see clinical/${_portal.id}-labs-list-*.json');
      setState(() => _batchRunning = false);
      return;
    }
    final raw = (rawRes['keys'] as List?) ?? const [];
    keys = raw.whereType<String>().toList();

    if (keys.isEmpty) {
      if (!mounted) return;
      setState(() {
        _status = 'Labs list returned zero keys — see clinical/${_portal.id}-labs-list-*.json';
        _batchRunning = false;
      });
      return;
    }

    // Step 2: iterate keys, calling GetDetails per one. Same wiring
    // as _fetchAllLabBodies but sourced live rather than from a
    // bundled asset.
    setState(() {
      _batchTotal = keys.length;
      _batchIndex = 0;
      _batchStartedAt = DateTime.now();
      _status = 'Fetching ${keys.length} ${_portal.name} lab details…';
    });

    final captured = <Map<String, dynamic>>[];
    final errors = <Map<String, dynamic>>[];
    int skipped = 0;

    try {
      for (int i = 0; i < keys.length; i++) {
        if (_abortRequested) {
          setState(() => _status = 'Aborted at $i/${keys.length}');
          break;
        }
        final eorderid = keys[i];
        if (_incrementalScrape &&
            await LocalWriter.hasLabBody(portalId: _portal.id, eorderid: eorderid)) {
          skipped++;
          continue;
        }
        setState(() {
          _batchIndex = i + 1;
          _status = 'Lab $_batchIndex/$_batchTotal — '
              '${captured.length} captured, ${errors.length} errors, $skipped skipped';
        });

        _labDetailCompleter = Completer<Map<String, dynamic>>();
        try {
          await _ctrl!.evaluateJavascript(
              source: ScrapeJobs.epicLabDetail(
                  portal: _portal, eorderid: eorderid));
        } catch (e) {
          errors.add({'eorderid': eorderid, 'reason': 'inject-failed:$e'});
          continue;
        }
        Map<String, dynamic> result;
        try {
          result = await _labDetailCompleter!.future
              .timeout(const Duration(seconds: 20));
        } on TimeoutException {
          errors.add({'eorderid': eorderid, 'reason': 'fetch-timeout'});
          continue;
        }

        if (result['ok'] != true) {
          errors.add({
            'eorderid': eorderid,
            'reason': result['error']?.toString() ?? 'unknown',
          });
          continue;
        }

        try {
          await LocalWriter.writeLabBody(
              portalId: _portal.id, eorderid: eorderid, details: result['details']);
          captured.add({'eorderid': eorderid});
        } catch (e) {
          errors.add({'eorderid': eorderid, 'reason': 'write-failed:$e'});
        }
      }
      await LocalWriter.writeLabBatchConsolidated(
        portalId: _portal.id,
        captured: captured,
        errors: errors,
        startedAt: _batchStartedAt!,
        finishedAt: DateTime.now(),
      );
      setState(() => _status = 'Labs done: ${captured.length} captured, '
          '${errors.length} errors, $skipped skipped');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// (Phase 4-2). Each section's JSON lands under
  /// Documents/clinical/stanford-<section>-<ts>.json.
  Future<void> _fetchClinicalTriad() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;
    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _batchTotal = 3;
      _batchIndex = 0;
      _status = 'Clinical triad: starting…';
    });
    final sections = [
      ('Allergies',     'Allergies/LoadListData'),
      ('Immunizations', 'Immunizations/LoadImmunizationsList'),
      ('HealthIssues',  'HealthIssues/LoadListData'),
      // Medications was probed and dropped 2026-08-10. Endpoint returns
      // HTML not JSON (React SPA that doesn't fire data XHRs in a 15s
      // window). See docs/COVERAGE_MATRIX.md changelog v6 for the full
      // verdict + Epic SMART-on-FHIR path recommendation.
    ];
    final results = <String, Map<String, dynamic>>{};
    try {
      for (var i = 0; i < sections.length; i++) {
        if (_abortRequested) break;
        final (section, endpointPath) = sections[i];
        setState(() {
          _batchIndex = i + 1;
          _status = 'Clinical triad ${i+1}/3: $section…';
        });
        _clinicalListCompleter = Completer<Map<String, dynamic>>();
        try {
          await _ctrl!.evaluateJavascript(
            source: ScrapeJobs.epicClinicalLoadList(
              portal: _portal, section: section, endpointPath: endpointPath),
          );
        } catch (e) {
          results[section] = {'ok': false, 'error': 'inject-failed:$e'};
          continue;
        }
        Map<String, dynamic> result;
        try {
          result = await _clinicalListCompleter!.future
              .timeout(const Duration(seconds: 30));
        } on TimeoutException {
          results[section] = {'ok': false, 'error': 'timeout-30s'};
          continue;
        }
        results[section] = result;
        if (result['ok'] == true && result['list'] != null) {
          await LocalWriter.writeClinicalList(portalId: _portal.id, section: section, listJson: result['list']);
        } else {
          // Persist the failure envelope (error name, HTTP status, attempts)
          // so we can debug from disk without re-running. Landed at
          // Documents/clinical/stanford-<section>-failed-<ts>.json.
          await LocalWriter.writeClinicalList(portalId: _portal.id, section: '$section-failed', listJson: result);
        }
      }
      final okCount = results.values.where((r) => r['ok'] == true).length;
      setState(() => _status = 'Clinical triad done: $okCount/3 OK');
    } catch (e) {
      setState(() => _status = 'Clinical triad failed: $e');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// Autonomous portal sweep — installs the page-context API capture,
  /// enumerates clinical-data links from the current (home) page,
  /// navigates through each in turn, snapshots the section + drains the
  /// captured XHRs, then writes one consolidated spec to disk.
  ///
  /// Once finished, the spec drives the host-side spec synthesizer
  /// (`tools/portal-scout/...`) which generates per-resource scraper code.
  /// For now we just collect; the per-section fetcher generation is the
  /// follow-up. Aborting via the FAB stops between sections cleanly.
  Future<void> _runPortalScout() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;
    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _batchStartedAt = DateTime.now();
      _batchIndex = 0;
      _batchTotal = 0;
      _status = 'Scout: installing capture hook…';
    });
    try {
      // No explicit nav at scout start — verifyAndFireScout's DOM probe
      // already confirmed the current page is signed-in. Adding a hard
      // navigation here knocked Stanford's session out (diag 2026-06-25:
      // probe saw /signedin/home, 4s later enumerate saw /#/ — Stanford
      // bounces hard-navigations back to login). Just enumerate the page
      // the user already landed on.

      // 1. Verify the bootstrap loaded and we're on the right page. The
      // probe writes the URL + pageHeading + a body-text snippet to the
      // diag log so we can debug without re-running.
      final probeRaw = await _ctrl!.evaluateJavascript(source: r'''
        JSON.stringify({
          bootstrap: !!window.__binaPortalScoutBootstrapped,
          installed: !!(window.__portalScout && window.__portalScout.installed),
          hasEnumerate: !!(window.__portalScout && typeof window.__portalScout.enumerateAllFrames === 'function'),
          frames: window.frames.length,
          url: location.href,
          pageTitle: document.title,
          pageHeading: (document.querySelector('h1, h2') && document.querySelector('h1, h2').innerText || '').trim().slice(0, 80),
          bodyTextHead: (document.body && document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 300),
        })
      ''');
      final probe = jsonDecode(probeRaw?.toString() ?? '{}');
      await LocalWriter.appendScoutDiagLine(portalId: _portal.id, batchStartedAt: _batchStartedAt!, event:
          {'phase': 'probe', ...Map<String, dynamic>.from(probe)});
      if (probe['installed'] != true || probe['hasEnumerate'] != true) {
        // Fallback: explicit injection of the bootstrap into the top frame.
        // (Subframes still need the UserScript path; if it didn't fire,
        // cross-frame enumeration will be limited.)
        await LocalWriter.appendScoutDiagLine(portalId: _portal.id, batchStartedAt: _batchStartedAt!, event:
            {'phase': 'bootstrap-fallback', 'reason': 'UserScript not detected'});
        await _ctrl!.evaluateJavascript(source: ScrapeJobs.bootstrapForUserScript());
      }

      // Start capture. The bootstrap is installed in every frame via
      // initialUserScripts; start() resets the records store and
      // broadcasts active=true to subframes.
      await _ctrl!.evaluateJavascript(source: 'window.__portalScout && window.__portalScout.start();');

      // 2. Let the SPA hydrate.
      setState(() => _status = 'Scout: waiting for SPA hydration (4s)…');
      await Future.delayed(const Duration(seconds: 4));

      // 3. Enumerate clickable elements across ALL frames. Use
      // callAsyncJavaScript so the Promise is properly awaited
      // (evaluateJavascript returns the Promise stringified, not its value).
      setState(() => _status = 'Scout: enumerating sections (all frames)…');
      final asyncResult = await _ctrl!.callAsyncJavaScript(functionBody: '''
        if (!window.__portalScout || typeof window.__portalScout.enumerateAllFrames !== 'function') {
          return JSON.stringify({ok:false, error:'portalScout not installed at enum time'});
        }
        const r = await window.__portalScout.enumerateAllFrames(2500);
        return JSON.stringify(r);
      ''');
      final linksRaw = asyncResult?.value;
      final linksDecoded = jsonDecode(linksRaw?.toString() ?? '{}');
      final allLinks = ((linksDecoded['candidates'] ?? const []) as List).cast<dynamic>();
      final byKind   = Map<String, dynamic>.from(linksDecoded['byKind'] ?? {});
      final byFrame  = Map<String, dynamic>.from(linksDecoded['byFrame'] ?? {});
      final frames   = linksDecoded['frames'] ?? 0;
      final clinical = allLinks
          .where((l) => l is Map && l['classification'] == 'clinical')
          .map<Map<String, dynamic>>((l) => Map<String, dynamic>.from(l as Map))
          .toList();
      // Count what got demoted to 'item' (per-item drilldowns) — these
      // are the per-section sample targets for the next scout phase.
      final itemCount = allLinks.where((l) => l is Map && l['classification'] == 'item').length;
      final skipCount = allLinks.where((l) => l is Map && l['classification'] == 'skip').length;
      setState(() {
        _batchTotal = clinical.length;
        _status = 'Scout: ${clinical.length} sections + $itemCount items '
            'in $frames frames (${allLinks.length} candidates)';
      });
      await LocalWriter.appendScoutDiagLine(portalId: _portal.id, batchStartedAt: _batchStartedAt!, event: {
        'phase': 'enumerated',
        'framesResponded': frames,
        'totalCandidates': allLinks.length,
        'clinicalLinks': clinical.length,
        'itemLinks': itemCount,
        'skipLinks': skipCount,
        'byElementKind': byKind,
        'byFrame': byFrame,
        'home': _currentUrl,
      });

      // If enumeration came back empty, write a "what was on the page"
      // snapshot to the spec so we can debug without a re-run.
      if (clinical.isEmpty) {
        await LocalWriter.appendScoutDiagLine(portalId: _portal.id, batchStartedAt: _batchStartedAt!, event: {
          'phase': 'no-clinical-links',
          'allCandidates': allLinks.take(30).toList(),
        });
      }

      // 3. Split visitable (has href) vs click-only (must DOM-click). v1
      // visits only the href-bearing set; click-only logged for later.
      final visitable = clinical.where((l) => l['href'] != null).toList();
      final clickOnly = clinical.where((l) => l['href'] == null).toList();
      if (clickOnly.isNotEmpty) {
        await LocalWriter.appendScoutDiagLine(portalId: _portal.id, batchStartedAt: _batchStartedAt!, event: {
          'phase': 'click-only-skipped',
          'count': clickOnly.length,
          'sample': clickOnly.take(10).map((l) => {
            'text': l['text'], 'kind': l['elementKind'], 'hintSource': l['hintSource'],
          }).toList(),
        });
      }

      // BFS visit loop with depth cap. Seed with the top-level visitable
      // set; after each successful visit, re-enumerate the now-loaded
      // page and queue any NEW clinical hrefs we haven't seen yet (one
      // level deeper). Stanford's sections expose their sub-nav only
      // once the section page itself renders — Allergies/Immunizations/
      // Conditions/etc. live under /signedin/records/* and aren't
      // visible from the top home page.
      final sections = <Map<String, dynamic>>[];
      final visitedUrls = <String>{};
      const int depthCap = 2;        // 0 = top, 1 = sub-nav, stop here
      const int totalVisitCap = 60;  // safety cap on BFS fan-out
      final Queue<Map<String, dynamic>> queue = Queue();
      for (final v in visitable) {
        queue.add({...v, 'depth': 0, 'discoveredFrom': null});
      }
      int visitNum = 0;
      while (queue.isNotEmpty && visitNum < totalVisitCap) {
        if (_abortRequested) {
          setState(() => _status = 'Scout aborted at $visitNum / queue+done=${queue.length + sections.length}');
          break;
        }
        final link = queue.removeFirst();
        final href = (link['href'] as String?) ?? '';
        if (href.isEmpty) continue;
        if (visitedUrls.contains(href)) continue;
        visitedUrls.add(href);
        visitNum++;
        final depth = link['depth'] as int;
        final label = (link['text'] as String? ?? '').isNotEmpty
            ? link['text']
            : (link['path'] ?? href);
        setState(() {
          _batchIndex = visitNum;
          _batchTotal = sections.length + queue.length + 1;
          _status = 'Scout #$visitNum (depth $depth, queued ${queue.length}): $label';
        });

        // Soft nav via JS location.href — Stanford's SPA invalidates the
        // session when we use loadUrl() (proven 2026-06-25 diag), but
        // setting window.location preserves the auth state because the
        // SPA's interceptors get a chance to handle the transition.
        _navCompleter = Completer<void>();
        final hrefJs = href.replaceAll(r'\', r'\\').replaceAll("'", r"\'");
        await _ctrl!.evaluateJavascript(source: "window.location.href = '$hrefJs';");
        try {
          await _navCompleter!.future.timeout(const Duration(seconds: 12));
        } on TimeoutException {
          // Continue anyway — SPA route transitions (hash-only changes)
          // don't always fire onLoadStop. The snapshot below confirms
          // where we actually landed.
        }

        // Let in-flight XHRs settle. 3s catches most Epic dashboards.
        await Future.delayed(const Duration(seconds: 3));

        // Snapshot + drain.
        final snapRaw = await _ctrl!.evaluateJavascript(source: ScrapeJobs.scoutSnapshotCurrent());
        final snap = jsonDecode(snapRaw?.toString() ?? '{}');
        final capsRaw = await _ctrl!.evaluateJavascript(source: ScrapeJobs.scoutGetCaptures(clear: true));
        final caps = (jsonDecode(capsRaw?.toString() ?? '[]') as List).cast<dynamic>();

        sections.add({
          'requestedHref': href,
          'requestedText': link['text'],
          'classification': link['classification'],
          'depth': depth,
          'discoveredFrom': link['discoveredFrom'],
          'snapshot': snap,
          'capturedXhrCount': caps.length,
          'capturedXhrs': caps,
        });

        int discovered = 0;
        // RECURSION: re-enumerate from the newly-loaded section page;
        // any unvisited clinical hrefs become depth+1 queue entries.
        if (depth + 1 < depthCap) {
          try {
            final reAsync = await _ctrl!.callAsyncJavaScript(functionBody: '''
              if (!window.__portalScout || typeof window.__portalScout.enumerateAllFrames !== 'function') {
                return JSON.stringify({ok:false, candidates:[]});
              }
              const r = await window.__portalScout.enumerateAllFrames(2500);
              return JSON.stringify(r);
            ''');
            final reDecoded = jsonDecode(reAsync?.value?.toString() ?? '{}');
            final newCands = ((reDecoded['candidates'] ?? const []) as List)
                .where((c) => c is Map && c['classification'] == 'clinical' && c['href'] != null)
                .cast<Map>();
            for (final c in newCands) {
              final h = c['href'] as String;
              if (visitedUrls.contains(h)) continue;
              if (queue.any((q) => q['href'] == h)) continue;
              queue.add({...Map<String, dynamic>.from(c), 'depth': depth + 1, 'discoveredFrom': href});
              discovered++;
            }
          } catch (_) { /* keep going */ }
        }

        await LocalWriter.appendScoutDiagLine(portalId: _portal.id, batchStartedAt: _batchStartedAt!, event: {
          'phase': 'visited',
          'visitNum': visitNum,
          'depth': depth,
          'href': href,
          'label': label,
          'finalUrl': snap['url'],
          'rowPatterns': (snap['rowPatterns'] as List?)?.length ?? 0,
          'xhrCount': caps.length,
          'discoveredSubLinks': discovered,
          'queueAfter': queue.length,
        });
      }

      // 4. Stop capture + write the spec.
      await _ctrl!.evaluateJavascript(source: 'window.__portalScout && window.__portalScout.stop();');
      final spec = {
        'portal': _portal.id,
        'scoutVersion': 'v1.12-2026-06-25',
        'startedAt': _batchStartedAt!.toUtc().toIso8601String(),
        'finishedAt': DateTime.now().toUtc().toIso8601String(),
        'home': _currentUrl,
        'enumeration': {
          'totalCandidates': allLinks.length,
          'byElementKind': byKind,
          'byFrame': byFrame,
          'framesResponded': frames,
          'clinical': clinical.length,
          'visited': sections.length,
          'clickOnlySkipped': clickOnly.length,
        },
        // FULL candidate list — every element the per-frame enumerators
        // surfaced, with its classification. v1.10 added this because
        // the only-clinical view obscures why specific top-nav items
        // (MESSAGES/VISITS/PROCEDURES) aren't being visited. Inspect
        // here to see whether each missing item was skipped, demoted to
        // 'item', or genuinely missing from the DOM.
        'allCandidates': allLinks,
        'clickOnlyTargets': clickOnly,
        'sections': sections,
      };
      final path = await LocalWriter.writeScoutSpec(spec);
      setState(() => _status = 'Scout done: ${sections.length} sections → $path');
    } catch (e) {
      setState(() => _status = 'Scout failed: $e');
      await LocalWriter.appendScoutDiagLine(
        portalId: _portal.id,
        batchStartedAt: _batchStartedAt ?? DateTime.now(),
        event: {'phase': 'error', 'message': e.toString()},
      );
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  Future<void> _onMenuSelected(String value) async {
    if (value == 'forget-login') {
      final portalId = _portal.id;
      final portalName = _portal.name;
      final exists = await CredentialsStore.has(portalId);
      if (!exists) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text('No saved $portalName login on this device.'),
          duration: const Duration(seconds: 3),
        ));
        return;
      }
      await CredentialsStore.clear(portalId);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('Saved $portalName login cleared from this device.'),
        duration: const Duration(seconds: 3),
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
    } else if (value == 'discover-messages') {
      await _discoverMessages();
    } else if (value == 'test-fetch-one-message') {
      await _testFetchOneMessage();
    } else if (value == 'fetch-all-message-bodies') {
      await _fetchAllMessageBodies();
    } else if (value == 'test-fetch-one-lab') {
      await _testFetchOneLab();
    } else if (value == 'fetch-all-lab-bodies') {
      await _fetchAllLabBodies();
    } else if (value == 'fetch-clinical-triad') {
      await _fetchClinicalTriad();
    } else if (value == 'toggle-auto-orchestrate') {
      setState(() => _autoOrchestrateOnSignIn = !_autoOrchestrateOnSignIn);
      if (!_autoOrchestrateOnSignIn) {
        _orchestratorAutoFireTimer?.cancel();   _orchestratorAutoFireTimer = null;
        _orchestratorAutoRepollTimer?.cancel(); _orchestratorAutoRepollTimer = null;
        if (_status.startsWith('Auto-scrape')) setState(() => _status = 'Auto-scrape deferred.');
      }
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(_autoOrchestrateOnSignIn
            ? 'Auto-scrape enabled. Runs all known scrapers after each sign-in.'
            : 'Auto-scrape disabled. Use "Run full scrape now" when ready.'),
        duration: const Duration(seconds: 4),
      ));
    } else if (value == 'run-full-scrape') {
      _orchestratorAutoFireTimer?.cancel();   _orchestratorAutoFireTimer = null;
      _orchestratorAutoRepollTimer?.cancel(); _orchestratorAutoRepollTimer = null;
      _orchestratorRanThisSession = false;
      await _runFullScrapePipeline();
    } else if (value == 'refetch-everything') {
      _orchestratorAutoFireTimer?.cancel();   _orchestratorAutoFireTimer = null;
      _orchestratorAutoRepollTimer?.cancel(); _orchestratorAutoRepollTimer = null;
      _orchestratorRanThisSession = false;
      final prev = _incrementalScrape;
      _incrementalScrape = false;
      try {
        await _runFullScrapePipeline();
      } finally {
        _incrementalScrape = prev;
      }
    } else if (value == 'toggle-auto-scout') {
      setState(() => _autoScoutOnSignIn = !_autoScoutOnSignIn);
      // Cancel any pending countdown / repoll if the user just disabled.
      if (!_autoScoutOnSignIn) {
        _scoutAutoFireTimer?.cancel();   _scoutAutoFireTimer = null;
        _scoutAutoRepollTimer?.cancel(); _scoutAutoRepollTimer = null;
        if (_status.startsWith('Auto-scout')) setState(() => _status = 'Auto-scout deferred.');
      }
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(_autoScoutOnSignIn
            ? 'Auto-scout enabled. Will run on next sign-in after URL stabilizes on /signedin/home.'
            : 'Auto-scout disabled. Use "Run portal scout now" when ready.'),
        duration: const Duration(seconds: 4),
      ));
    } else if (value == 'run-portal-scout') {
      _scoutAutoFireTimer?.cancel();   _scoutAutoFireTimer = null;
      _scoutAutoRepollTimer?.cancel(); _scoutAutoRepollTimer = null;
      _scoutRanThisSession = false; // allow manual re-trigger
      await _runPortalScout();
    } else if (value == 'go-to-login') {
      // Recover from a stray tap that navigated the WebView off the
      // portal's login/home page. Loads _portal.urls.login fresh.
      final ctrl = _ctrl;
      if (ctrl == null) return;
      await ctrl.loadUrl(
        urlRequest: URLRequest(url: WebUri(_portal.urls.login)));
      setState(() => _status = 'Returning to ${_portal.name} login…');
    } else if (value == 'switch-portal') {
      // Portal picker dialog. Persists choice via PortalRegistry
      // (Keychain-backed), then reloads the WebView at the new
      // portal's login URL. Any in-flight batch is a no-op precondition.
      if (_batchRunning || _orchestratorRunning) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Finish or abort the current batch before switching portals.'),
          duration: Duration(seconds: 3),
        ));
        return;
      }
      final current = _portal.id;
      final picked = await showDialog<String>(
        context: context,
        builder: (dialogCtx) => SimpleDialog(
          title: const Text('Switch portal'),
          children: [
            for (final p in PortalRegistry.instance.all)
              SimpleDialogOption(
                onPressed: () => Navigator.of(dialogCtx).pop(p.id),
                child: Row(children: [
                  Icon(
                    p.id == current ? Icons.radio_button_checked : Icons.radio_button_off,
                    color: p.id == current ? Colors.teal : Colors.grey,
                  ),
                  const SizedBox(width: 12),
                  Expanded(child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(p.name, style: const TextStyle(fontWeight: FontWeight.w500)),
                      Text(p.hosts.userFacing, style: const TextStyle(fontSize: 12, color: Colors.black54)),
                    ],
                  )),
                ]),
              ),
          ],
        ),
      );
      if (picked == null || picked == current) return;
      await PortalRegistry.instance.setActive(picked);
      // Reset per-session flags so the newly-active portal gets a
      // fresh orchestrator + scout arm on its post-auth landing.
      _orchestratorRanThisSession = false;
      _scoutRanThisSession = false;
      setState(() {
        _status = 'Switched to ${_portal.name}. Log in when ready.';
      });
      final ctrl = _ctrl;
      if (ctrl != null) {
        await ctrl.loadUrl(
          urlRequest: URLRequest(url: WebUri(_portal.urls.login)));
      }
    } else if (value == 'paste-into-focused') {
      // Bypass WKWebView's paste block (Stanford's page swallows onpaste;
      // iOS Simulator's cross-clipboard sync is also unreliable). Reads
      // the Flutter clipboard directly and JS-injects the value into
      // whatever input/textarea/contenteditable is currently focused,
      // using the React-safe setter path from loginAutofillAndCapture.
      final ctrl = _ctrl;
      if (ctrl == null) return;
      final data = await Clipboard.getData('text/plain');
      final text = data?.text;
      if (text == null || text.isEmpty) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Clipboard is empty.'),
          duration: Duration(seconds: 2),
        ));
        return;
      }
      final textJs = jsonEncode(text);
      final result = await ctrl.evaluateJavascript(source: '''
        (() => {
          const el = document.activeElement;
          if (!el) return { ok: false, reason: 'no-active-element' };
          const tag = el.tagName;
          if (tag !== 'INPUT' && tag !== 'TEXTAREA' && !el.isContentEditable) {
            return { ok: false, reason: 'not-editable', tag };
          }
          const val = $textJs;
          if (el.isContentEditable) {
            el.innerText = val;
          } else {
            const proto = tag === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, val);
          }
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return { ok: true, tag, len: val.length };
        })();
      ''');
      if (!mounted) return;
      final r = (result is Map) ? Map<String, dynamic>.from(result) : {'ok': false, 'reason': 'no-result'};
      final msg = r['ok'] == true
          ? 'Pasted ${r['len']} chars into <${(r['tag'] as String?)?.toLowerCase()}>'
          : 'Paste failed: ${r['reason'] ?? 'unknown'}';
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(msg),
        duration: const Duration(seconds: 3),
      ));
    } else if (value == 'probe-endpoints') {
      await _probeUcsfEndpoints();
    } else if (value == 'retry-failures') {
      // Aggregates failed + partially-captured CSNs across ALL prior
      // batches — so retry catches both never-worked visits AND multi-note
      // visits that captured some sub-notes but missed others.
      final failed = await LocalWriter.findIncompleteCsns(portalId: _portal.id);
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
    final portal = (m['portal'] ?? _portal.id).toString();
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
        title: Text('Save ${_portal.name} login?'),
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
    final portalId = _portal.id;
    final cred = await CredentialsStore.read(portalId);
    final js = ScrapeJobs.loginAutofillAndCapture(
      portalId: portalId,
      autofillEmail: cred?.email,
      autofillPassword: cred?.password,
    );
    try {
      await ctrl.evaluateJavascript(source: js);
    } catch (_) {
      // Page may have already navigated by the time JS runs — fine.
    }
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
        await LocalWriter.writeBatchManifest(portalId: _portal.id, m: BatchManifest(
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
            await LocalWriter.writeMultiNote(portalId: _portal.id, csn: csn, notes: subs);
          } else {
            final plain = html.replaceAll(RegExp(r'<[^>]+>'), '').trim();
            _captured.add(CapturedNote(
              csn: csn,
              html: html,
              htmlLength: html.length,
              visibleTextLength: plain.length,
              capturedAt: DateTime.now(),
            ));
            await LocalWriter.writeNote(portalId: _portal.id, csn: csn, html: html);
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
        portalId: _portal.id,
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
    final url = _portal.urls.visitDetailPattern
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
      await _ctrl!.evaluateJavascript(source: ScrapeJobs.epicSingleNote(pollMs: pollMs));
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

  // ── Messages: discovery pass (Phase 3-1) ───────────────────────────
  /// Walk inbox + outbox folder pages, collect every message's
  /// {id, subject, otherParty, date, isUnread, isReply}, and persist to
  /// a discovery JSON file. No body fetching yet — that's Phase 3-2.
  ///
  /// Pagination: try `?page=N` URL pattern first; if that yields the same
  /// rows or fewer than the prior page, stop. (Confirms the param works
  /// without needing to detect Stanford's exact pager UI upfront.)
  Future<void> _discoverMessages() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;
    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _status = 'Discovering Stanford messages — inbox first…';
    });
    final startedAt = DateTime.now();
    final allRows = <Map<String, dynamic>>[];
    // Per-page reports: ONE entry per HTTP page we visited, containing
    // the request URL, the landed URL (in case Stanford redirected), and
    // the full JS-side diagnostic block. Lets us debug any selector /
    // pagination / routing failure from a single discovery file.
    final pageReports = <Map<String, dynamic>>[];
    final meta = <String, dynamic>{
      'inboxPages': 0,
      'outboxPages': 0,
      'inboxRowCount': 0,
      'outboxRowCount': 0,
      'errors': <String>[],
    };

    try {
      for (final folder in const ['inbox', 'outbox']) {
        final baseUrl = folder == 'inbox'
            ? _portal.urls.messageInbox
            : _portal.urls.messageOutbox;
        final seenIds = <String>{};
        int page = 1;
        // Cursor pagination: Stanford's response carries `nextPageBeginMessageId`
        // when more pages exist. Pass it back in the next request's body
        // (the JS handles the actual body shape). The old `?page=N` URL
        // pattern was ignored by Stanford — it returned page 1 every time.
        String? cursor;
        bool keepGoing = true;
        while (keepGoing && page <= 80 && !_abortRequested) {
          setState(() => _status =
              'Discovering $folder page $page (collected ${allRows.length})…');
          final t0 = DateTime.now();
          // Skip the URL load on pages 2+ — we're already on the folder
          // page from p1; cursor is in the API body, not the URL.
          final res = await _scrapeMessageListPage(
            baseUrl,
            skipNav: page > 1,
            cursor: cursor,
          );
          final elapsedMs = DateTime.now().difference(t0).inMilliseconds;

          String landedUrl = '';
          try { landedUrl = (await _ctrl!.getUrl())?.toString() ?? ''; }
          catch (_) {}

          final report = <String, dynamic>{
            'folder': folder,
            'page': page,
            'requestedUrl': baseUrl,
            'cursorIn': cursor,
            'landedUrl': landedUrl,
            'elapsedMs': elapsedMs,
            ...?res?.map((k, v) => MapEntry(k, k == 'rows' ? null : v)),
          };
          pageReports.add(report);

          if (res == null) {
            meta['errors'].add('$folder p$page nav-failed');
            break;
          }
          final err = res['error']?.toString();
          final rows = (res['rows'] as List?) ?? const [];
          if (err != null && err.isNotEmpty && rows.isEmpty && page == 1) {
            meta['errors'].add('$folder p1: $err');
            break;
          }
          int newRowsThisPage = 0;
          for (final r in rows) {
            if (r is! Map) continue;
            final id = r['id']?.toString();
            if (id == null) continue;
            final key = '$folder:$id';
            if (seenIds.contains(key)) continue;
            seenIds.add(key);
            allRows.add(Map<String, dynamic>.from(r));
            newRowsThisPage += 1;
          }
          meta['${folder}Pages'] = page;
          meta['${folder}RowCount'] = seenIds.length;

          // Advance via Stanford's cursor; stop if the server says no more
          // pages, the new page gave us only duplicates, or it's empty.
          cursor = res['nextCursor']?.toString();
          if (cursor == null || newRowsThisPage == 0) {
            keepGoing = false;
            break;
          }
          page += 1;
        }
      }
      meta['pageReports'] = pageReports;

      final finishedAt = DateTime.now();
      final path = await LocalWriter.writeMessagesDiscovery(
        portalId: _portal.id,
        startedAt: startedAt,
        finishedAt: finishedAt,
        rows: allRows,
        meta: meta,
      );
      final dur = finishedAt.difference(startedAt);
      setState(() => _status =
          'Discovery done in ${dur.inSeconds}s — ${allRows.length} rows '
          '(${meta['inboxRowCount']} inbox, ${meta['outboxRowCount']} outbox) → $path');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// One-shot probe — picks the first message ID from the most recent
  /// discovery file and runs the detail fetcher against it. Saves the
  /// result to a test-fetch-*.json file for inspection. Used to
  /// confirm the per-message endpoint shape before committing to a
  /// full batch loop.
  Future<void> _testFetchOneMessage() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;

    // Pull the most recent discovery file from disk
    final pick = await LocalWriter.firstMessageRowFromLatestDiscovery(portalId: _portal.id);
    if (pick == null) {
      setState(() => _status = 'No message discovery file found — run '
          '"Discover messages" first.');
      return;
    }
    final folder = pick['folder'] as String;
    final id = pick['id'] as String;

    setState(() {
      _batchRunning = true;
      _status = 'Test fetch: $folder message $id…';
    });

    try {
      // We're already at /signedin/messages/... from prior nav (or login
      // landing). The detail endpoint is same-origin JSON, so as long
      // as the session cookie is present (it is), we don't need to
      // navigate to a specific page first.
      _messageDetailCompleter = Completer<Map<String, dynamic>>();
      try {
        await _ctrl!.evaluateJavascript(
          source: ScrapeJobs.epicMessageDetail(folder: folder, messageId: id),
        );
      } catch (e) {
        setState(() => _status = 'Inject failed: $e');
        return;
      }
      Map<String, dynamic> result;
      try {
        result = await _messageDetailCompleter!.future
            .timeout(const Duration(seconds: 30));
      } on TimeoutException {
        setState(() => _status = 'Test fetch timed out (30s).');
        return;
      }
      final path = await LocalWriter.writeMessageTestFetch(portalId: _portal.id, result: result);
      final ok = result['ok'] == true;
      final endpoint = result['endpoint']?.toString() ?? '(none worked)';
      setState(() => _status = 'Test fetch ${ok ? "OK" : "FAILED"} — '
          'endpoint=$endpoint → $path');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// One-shot probe — picks the first eorderid from the bundled
  /// stanford-lab-orders.json asset and fetches its HTML body, saves
  /// to a test-fetch file for inspection. Used to confirm the lab
  /// detail endpoint actually returns content (not a redirect / login
  /// shell / error page) before kicking off the full batch.
  Future<void> _testFetchOneLab() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;
    final orders = await _loadLabOrders();
    if (orders.isEmpty) {
      setState(() => _status =
          'No lab orders in assets — run tools/mobile/build_lab_orders_discovery.py '
          'then scripts/sync-mobile-assets.sh, and reinstall the app.');
      return;
    }
    final eorderid = orders.first['eorderid'] as String;
    setState(() {
      _batchRunning = true;
      _status = 'Test fetch: lab $eorderid…';
    });
    try {
      _labDetailCompleter = Completer<Map<String, dynamic>>();
      try {
        await _ctrl!.evaluateJavascript(
          source: ScrapeJobs.epicLabDetail(portal: _portal, eorderid: eorderid),
        );
      } catch (e) {
        setState(() => _status = 'Inject failed: $e');
        return;
      }
      Map<String, dynamic> result;
      try {
        result = await _labDetailCompleter!.future
            .timeout(const Duration(seconds: 30));
      } on TimeoutException {
        setState(() => _status = 'Test fetch timed out (30s).');
        return;
      }
      final path = await LocalWriter.writeLabTestFetch(portalId: _portal.id, result: result);
      final ok = result['ok'] == true;
      final details = result['details'];
      final hasBody = details is Map &&
          (details['results'] is List) &&
          (details['results'] as List).isNotEmpty;
      setState(() => _status = 'Test fetch ${ok ? "OK" : "FAILED"} — '
          'hasBody=$hasBody → $path');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// Iterate every eorderid in the bundled stanford-lab-orders.json asset
  /// and fetch its GetDetails JSON from mychart.stanfordhealthcare.org via
  /// the reverse-engineered two-step API flow (see ScrapeJobs.epicLabDetail).
  /// Per-result JSON files written immediately (crash-safe); consolidated
  /// batch index at end.
  Future<void> _fetchAllLabBodies() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;
    final orders = await _loadLabOrders();
    if (orders.isEmpty) {
      setState(() => _status =
          'No lab orders in assets — run tools/mobile/build_lab_orders_discovery.py '
          'then scripts/sync-mobile-assets.sh, and reinstall the app.');
      return;
    }
    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _batchTotal = orders.length;
      _batchIndex = 0;
      _batchStartedAt = DateTime.now();
      _status = 'Fetching ${orders.length} lab result bodies…';
    });
    final captured = <Map<String, dynamic>>[];
    final errors = <Map<String, dynamic>>[];
    final rng = Random();
    try {
      int skipped = 0;
      for (int i = 0; i < orders.length; i++) {
        if (_abortRequested) {
          setState(() => _status = 'Aborted at $i/${orders.length}');
          break;
        }
        final eorderid = orders[i]['eorderid'] as String;
        final code = (orders[i]['code'] ?? '').toString();
        // Skip-if-on-disk (incremental scrape). Lab bodies are effectively
        // immutable at Stanford; re-fetching costs ~320ms per lab, so a
        // 489-lab pass takes 2.5min for zero new data. Menu toggle
        // "Refetch everything" flips _incrementalScrape off for a full
        // re-pull (e.g., after an addendum).
        if (_incrementalScrape && await LocalWriter.hasLabBody(portalId: _portal.id, eorderid: eorderid)) {
          skipped++;
          continue;
        }
        setState(() {
          _batchIndex = i + 1;
          _status = 'Lab $_batchIndex/$_batchTotal '
              '— ${captured.length} captured, ${errors.length} errors, $skipped skipped';
        });
        _labDetailCompleter = Completer<Map<String, dynamic>>();
        try {
          await _ctrl!.evaluateJavascript(
            source: ScrapeJobs.epicLabDetail(portal: _portal, eorderid: eorderid),
          );
        } catch (e) {
          errors.add({'eorderid': eorderid, 'reason': 'inject-failed:$e'});
          continue;
        }
        Map<String, dynamic> result;
        try {
          result = await _labDetailCompleter!.future
              .timeout(const Duration(seconds: 25));
        } on TimeoutException {
          errors.add({'eorderid': eorderid, 'reason': 'fetch-timeout'});
          continue;
        }
        if (result['ok'] != true) {
          errors.add({
            'eorderid': eorderid,
            'reason': result['error']?.toString() ?? 'unknown',
            'status': result['status'],
          });
          continue;
        }
        final details = result['details'];
        if (details is! Map) {
          errors.add({'eorderid': eorderid, 'reason': 'details-not-an-object'});
          continue;
        }
        final resultsList = details['results'];
        final hasBody = resultsList is List && resultsList.isNotEmpty;
        captured.add({
          'eorderid': eorderid,
          'code': code,
          'hasBody': hasBody,
          'resultCount': (resultsList is List) ? resultsList.length : 0,
          'capturedAt': DateTime.now().toUtc().toIso8601String(),
        });
        await LocalWriter.writeLabBody(portalId: _portal.id, eorderid: eorderid, details: details);
        // Light pacing — GetDetails is ~4KB JSON; ~150-300ms is plenty
        if (i + 1 < orders.length) {
          final pauseMs = 200 + rng.nextInt(300);
          await Future.delayed(Duration(milliseconds: pauseMs));
        }
      }
      final finishedAt = DateTime.now();
      final path = await LocalWriter.writeLabBatchConsolidated(
        portalId: _portal.id,
        captured: captured,
        errors: errors,
        startedAt: _batchStartedAt!,
        finishedAt: finishedAt,
      );
      final dur = finishedAt.difference(_batchStartedAt!);
      setState(() => _status = 'DONE in ${dur.inSeconds}s — '
          '${captured.length} captured, ${errors.length} errors → $path');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// Read the bundled lab-orders asset and return the orders array.
  /// Empty list if the asset is missing (user hasn't run discovery + sync).
  Future<List<Map<String, dynamic>>> _loadLabOrders() async {
    try {
      final raw = await rootBundle.loadString('assets/stanford-lab-orders.json');
      final data = jsonDecode(raw) as Map<String, dynamic>;
      final orders = (data['orders'] as List?) ?? const [];
      return orders.whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList();
    } catch (_) {
      return const [];
    }
  }

  /// Iterate every {folder, id} pair in the most recent discovery file
  /// and POST /Mailbox/Message for each. Per-message files written
  /// immediately (crash-safe); manifest rewritten per message;
  /// consolidated batch at end. Mirrors the per-visit-note resilience
  /// model.
  Future<void> _fetchAllMessageBodies() async {
    if (_ctrl == null || !_onSignedInPage) return;
    if (_batchRunning) return;

    final ids = await LocalWriter.allMessageRowsFromLatestDiscovery(portalId: _portal.id);
    if (ids.isEmpty) {
      setState(() => _status = 'No discovery rows — run "Discover messages" first.');
      return;
    }

    setState(() {
      _batchRunning = true;
      _abortRequested = false;
      _batchTotal = ids.length;
      _batchIndex = 0;
      _batchStartedAt = DateTime.now();
      _status = 'Fetching ${ids.length} message bodies…';
    });

    final captured = <Map<String, dynamic>>[];
    final errors = <Map<String, dynamic>>[];
    final rng = Random();

    try {
      int skipped = 0;
      for (int i = 0; i < ids.length; i++) {
        if (_abortRequested) {
          setState(() => _status = 'Aborted at $i/${ids.length}');
          break;
        }
        final folder = ids[i]['folder']!;
        final id = ids[i]['id']!;
        // Skip-if-on-disk (incremental scrape). Message bodies are
        // immutable at Stanford once sent; ~752 msgs × ~180ms = 2+ min
        // wasted on a no-op re-pull.
        if (_incrementalScrape && await LocalWriter.hasMessageBody(portalId: _portal.id, folder: folder, msgId: id)) {
          skipped++;
          continue;
        }
        setState(() {
          _batchIndex = i + 1;
          _status = 'Fetching $folder/$id  ($_batchIndex/$_batchTotal '
              '— ${captured.length} captured, ${errors.length} errors, $skipped skipped)';
        });
        await LocalWriter.writeMessageBatchManifest(portalId: _portal.id, m: MessageBatchManifest(
          startedAt: _batchStartedAt!,
          totalCount: ids.length,
          currentIndex: i,
          capturedCount: captured.length,
          errorCount: errors.length,
          currentFolder: folder,
          currentId: id,
        ));

        _messageDetailCompleter = Completer<Map<String, dynamic>>();
        try {
          await _ctrl!.evaluateJavascript(
            source: ScrapeJobs.epicMessageDetail(folder: folder, messageId: id),
          );
        } catch (e) {
          errors.add({'folder': folder, 'id': id, 'reason': 'inject-failed:$e'});
          continue;
        }
        Map<String, dynamic> result;
        try {
          result = await _messageDetailCompleter!.future
              .timeout(const Duration(seconds: 20));
        } on TimeoutException {
          errors.add({'folder': folder, 'id': id, 'reason': 'fetch-timeout'});
          continue;
        }

        if (result['ok'] != true) {
          errors.add({
            'folder': folder, 'id': id,
            'reason': result['error']?.toString() ?? 'unknown',
            'attempts': result['attempts'],
          });
          continue;
        }
        captured.add(result);
        await LocalWriter.writeMessageBody(portalId: _portal.id, folder: folder, id: id, data: result);

        // Light pacing to stay below Stanford's rate-limit threshold.
        // Each message fetch is ~200ms; total batch with 752 messages
        // would be ~3 min at no-pace, ~7 min with this jitter.
        if (i + 1 < ids.length) {
          final pauseMs = 200 + rng.nextInt(400);
          await Future.delayed(Duration(milliseconds: pauseMs));
        }
      }

      final finishedAt = DateTime.now();
      final path = await LocalWriter.writeMessageBatchConsolidated(
        portalId: _portal.id,
        captured: captured,
        errors: errors,
        startedAt: _batchStartedAt!,
        finishedAt: finishedAt,
      );
      final dur = finishedAt.difference(_batchStartedAt!);
      setState(() => _status = 'DONE in ${dur.inSeconds}s — '
          '${captured.length} captured, ${errors.length} errors → $path');
    } finally {
      setState(() => _batchRunning = false);
    }
  }

  /// Load one message-list URL, inject the list scraper, await the result.
  /// Returns the JS payload with a `dartTimings` field added — per-phase
  /// breakdown (navMs / settleMs / jsMs / totalMs) so we can tell where
  /// each second goes between Dart-side waits and JS-side work.
  /// Load (optionally) + inject the message-list scraper.
  ///
  /// [skipNav] true → don't reload the URL; reuse the current page.
  ///   Used for pages 2+ of the same folder (cursor pagination): the
  ///   API call is a same-origin XHR that doesn't care about the
  ///   visible URL, so re-navigating wastes a network round-trip.
  /// [cursor] → Stanford's `nextPageBeginMessageId` for cursor pagination.
  ///
  /// Removes the 2s Dart-side settle that earlier versions added "for
  /// SPA mount" — the API endpoint accepts requests the moment the
  /// session cookie is valid, which is true by the time onLoadStop
  /// fires. Saves ~2s × N pages.
  Future<Map<String, dynamic>?> _scrapeMessageListPage(
    String url, {
    bool skipNav = false,
    String? cursor,
  }) async {
    final stopwatch = Stopwatch()..start();
    final phases = <String, int>{};

    if (!skipNav) {
      _navCompleter = Completer<void>();
      try {
        await _ctrl!.loadUrl(urlRequest: URLRequest(url: WebUri(url)));
      } catch (_) {
        return {'dartTimings': {'totalMs': stopwatch.elapsedMilliseconds}, 'error': 'loadUrl-threw'};
      }
      final navStart = stopwatch.elapsedMilliseconds;
      try {
        await _navCompleter!.future.timeout(const Duration(seconds: 20));
      } on TimeoutException {
        return {
          'dartTimings': {'navMs': stopwatch.elapsedMilliseconds - navStart, 'totalMs': stopwatch.elapsedMilliseconds},
          'error': 'nav-timeout',
        };
      }
      phases['navMs'] = stopwatch.elapsedMilliseconds - navStart;
    } else {
      phases['navMs'] = 0;
    }

    final jsStart = stopwatch.elapsedMilliseconds;
    _messageListCompleter = Completer<Map<String, dynamic>>();
    try {
      await _ctrl!.evaluateJavascript(
          source: ScrapeJobs.epicMessageList(cursor: cursor));
    } catch (e) {
      return {
        'dartTimings': {...phases, 'totalMs': stopwatch.elapsedMilliseconds},
        'error': 'inject-failed: $e',
      };
    }
    Map<String, dynamic> result;
    try {
      result = await _messageListCompleter!.future
          .timeout(const Duration(seconds: 60));
    } on TimeoutException {
      return {
        'dartTimings': {...phases, 'jsMs': stopwatch.elapsedMilliseconds - jsStart, 'totalMs': stopwatch.elapsedMilliseconds},
        'error': 'scrape-timeout',
      };
    }
    phases['jsMs'] = stopwatch.elapsedMilliseconds - jsStart;
    phases['totalMs'] = stopwatch.elapsedMilliseconds;
    result['dartTimings'] = phases;
    return result;
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
