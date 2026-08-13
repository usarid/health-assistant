import 'dart:convert';
import 'dart:io';
import 'package:path_provider/path_provider.dart';

/// Persistence for scraped clinical notes.
///
/// Every write/read function takes `required String portalId` so files
/// are namespaced per portal (`$portalId-*` filename prefixes,
/// per-portal payload tags). Prevents one portal's ingest from
/// overwriting another's on-disk sandbox.
///
/// Two output shapes for note captures:
///   - Per-note JSON files (resilient — a crash mid-batch leaves all
///     previously-captured notes intact on disk)
///   - A running manifest (rewritten every note for incremental safety)
///     + a final consolidated batch JSON at run end
///
/// Per-CSN payloads come in two flavors:
///   - **single**: one inline note rendered in the visit's Clinical Notes
///     tab. Persisted as { csn, html, ... }.
///   - **multi**: a list view where each row needs a VIEW NOTE click.
///     Persisted as { csn, html: '', notes: [{label, html, htmlLength}, ...] }.
///
/// All files land in the app's documents directory. On iOS Simulator
/// that's under ~/Library/Developer/CoreSimulator/Devices/<dev>/
/// data/Containers/Data/Application/<app>/Documents/.
class LocalWriter {
  /// Write one captured inline note to its own file.
  static Future<String> writeNote({
    required String portalId,
    required String csn,
    required String html,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final notesDir = Directory('${dir.path}/notes');
    await notesDir.create(recursive: true);
    final file = File('${notesDir.path}/$portalId-note-${_csnSlug(csn)}.json');
    final body = jsonEncode({
      'csn': csn,
      'savedAt': DateTime.now().toUtc().toIso8601String(),
      'htmlLength': html.length,
      'html': html,
    });
    await file.writeAsString(body);
    return file.path;
  }

  /// Write a multi-note visit's per-note payload to a single file keyed by
  /// the parent CSN. The JSON contains an array of sub-notes (each with
  /// its row label + HTML). One file per CSN keeps the resilience model
  /// intact (a crash mid-iteration leaves the previously-captured CSNs
  /// fully written; the in-progress CSN is just absent).
  static Future<String> writeMultiNote({
    required String portalId,
    required String csn,
    required List<SubNote> notes,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final notesDir = Directory('${dir.path}/notes');
    await notesDir.create(recursive: true);
    final file = File('${notesDir.path}/$portalId-multinote-${_csnSlug(csn)}.json');
    final body = jsonEncode({
      'csn': csn,
      'savedAt': DateTime.now().toUtc().toIso8601String(),
      'noteCount': notes.length,
      'notes': notes.map((n) => n.toJson()).toList(),
    });
    await file.writeAsString(body);
    return file.path;
  }

  /// Rewrite the batch-progress manifest. Small file; rewriting it after
  /// each note is cheap. A crash mid-batch leaves a manifest pointing at
  /// the last completed index so we can resume or report.
  static Future<String> writeBatchManifest({
    required String portalId,
    required BatchManifest m,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final file = File('${dir.path}/$portalId-batch-manifest.json');
    await file.writeAsString(jsonEncode(m.toJson()));
    return file.path;
  }

  /// Write the consolidated batch — all captured notes + the error list —
  /// at the end of a run. This is what iteration 3 will POST to the
  /// BinaHealth backend.
  static Future<String> writeConsolidated({
    required String portalId,
    required List<CapturedNote> captured,
    required List<ScrapeError> errors,
    required DateTime startedAt,
    required DateTime finishedAt,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final ts = finishedAt.toUtc().toIso8601String().replaceAll(':', '-');
    final file = File('${dir.path}/$portalId-batch-$ts.json');
    final payload = {
      'portal': portalId,
      'startedAt': startedAt.toUtc().toIso8601String(),
      'finishedAt': finishedAt.toUtc().toIso8601String(),
      'capturedCount': captured.length,
      'errorCount': errors.length,
      'captured': captured.map((c) => c.toJson()).toList(),
      'errors': errors.map((e) => e.toJson()).toList(),
    };
    await file.writeAsString(jsonEncode(payload));
    return file.path;
  }

  /// Write a messages-discovery dump: the full list of message rows we
  /// found across both folders (inbox + outbox) on a single discovery
  /// pass. One file per discovery run, timestamped. Caller pre-aggregates
  /// rows from all paginated pages of both folders into the [rows] list.
  ///
  /// This is the input for Phase 3-2 (per-message body fetch): we walk
  /// these IDs and capture the body from each detail page.
  static Future<String> writeMessagesDiscovery({
    required String portalId,
    required DateTime startedAt,
    required DateTime finishedAt,
    required List<Map<String, dynamic>> rows,
    required Map<String, dynamic> meta,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final ts = finishedAt.toUtc().toIso8601String().replaceAll(':', '-');
    final file = File('${dir.path}/$portalId-messages-discovery-$ts.json');
    final payload = {
      'portal': portalId,
      'startedAt': startedAt.toUtc().toIso8601String(),
      'finishedAt': finishedAt.toUtc().toIso8601String(),
      'rowCount': rows.length,
      'meta': meta,
      'rows': rows,
    };
    await file.writeAsString(jsonEncode(payload));
    return file.path;
  }

  /// Pull the first usable {folder, id} pair from the most recent
  /// per-portal messages-discovery file. Used by the Phase 3-2
  /// test-fetch probe to pick a real message ID without hardcoding one.
  static Future<Map<String, String>?> firstMessageRowFromLatestDiscovery({
    required String portalId,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final entries = await Directory(dir.path).list().toList();
    final files = entries
        .whereType<File>()
        .where((f) => f.path.contains('$portalId-messages-discovery-'))
        .toList()
      ..sort((a, b) => b.path.compareTo(a.path));
    if (files.isEmpty) return null;
    try {
      final raw = await files.first.readAsString();
      final m = jsonDecode(raw);
      if (m is! Map) return null;
      final rows = m['rows'];
      if (rows is! List) return null;
      for (final r in rows) {
        if (r is Map && r['id'] is String && r['folder'] is String) {
          return {'folder': r['folder'] as String, 'id': r['id'] as String};
        }
      }
    } catch (_) {}
    return null;
  }

  /// Save one test-fetch result for inspection. Per-file, timestamped,
  /// so multiple probes don't clobber each other.
  static Future<String> writeMessageTestFetch({
    required String portalId,
    required Map<String, dynamic> result,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final ts = DateTime.now().toUtc().toIso8601String().replaceAll(':', '-');
    final file = File('${dir.path}/$portalId-message-test-fetch-$ts.json');
    await file.writeAsString(jsonEncode(result));
    return file.path;
  }

  /// Read ALL {folder, id} pairs from the most recent per-portal
  /// discovery file. Used by the Phase 3-2 batch loop to know which
  /// messages to fetch.
  static Future<List<Map<String, String>>> allMessageRowsFromLatestDiscovery({
    required String portalId,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final entries = await Directory(dir.path).list().toList();
    final files = entries
        .whereType<File>()
        .where((f) => f.path.contains('$portalId-messages-discovery-'))
        .toList()
      ..sort((a, b) => b.path.compareTo(a.path));
    if (files.isEmpty) return const [];
    try {
      final raw = await files.first.readAsString();
      final m = jsonDecode(raw);
      if (m is! Map) return const [];
      final rows = m['rows'];
      if (rows is! List) return const [];
      final out = <Map<String, String>>[];
      for (final r in rows) {
        if (r is Map && r['id'] is String && r['folder'] is String) {
          out.add({'folder': r['folder'] as String, 'id': r['id'] as String});
        }
      }
      return out;
    } catch (_) {}
    return const [];
  }

  /// Save one lab test-fetch result for inspection — Phase 4-2 probe.
  static Future<String> writeLabTestFetch({
    required String portalId,
    required Map<String, dynamic> result,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final ts = DateTime.now().toUtc().toIso8601String().replaceAll(':', '-');
    final file = File('${dir.path}/$portalId-lab-test-fetch-$ts.json');
    await file.writeAsString(jsonEncode(result));
    return file.path;
  }

  /// Per-lab-result JSON body — the GetDetails response for one eorderid.
  /// Written once per fetched lab during the Phase 4-2 batch loop. The
  /// Phase 4-3 Python ingest reads the JSON directly (resultComponents
  /// for structured numerical labs, studyResult.contentAsHtml for
  /// imaging/pathology bodies).
  static Future<String> writeLabBody({
    required String portalId,
    required String eorderid,
    required Object details,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final labDir = Directory('${dir.path}/labs');
    await labDir.create(recursive: true);
    final file = File('${labDir.path}/$portalId-lab-${_csnSlug(eorderid)}.json');
    await file.writeAsString(jsonEncode({'eorderid': eorderid, 'details': details}));
    return file.path;
  }

  /// True if we already have a saved body for this eorderid.
  /// The auto-scrape orchestrator uses this to skip re-fetching bodies
  /// that are already on disk — labs are effectively immutable at Epic
  /// portals, so once captured they don't need to be re-POSTed on every
  /// sign-in. Manual "Refetch all" can be used if content changed
  /// (addenda / corrections).
  static Future<bool> hasLabBody({
    required String portalId,
    required String eorderid,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final file = File('${dir.path}/labs/$portalId-lab-${_csnSlug(eorderid)}.json');
    return file.exists();
  }

  /// True if we already have a saved body for this (folder, msgId).
  /// Same idempotency rationale as [hasLabBody] — message bodies are
  /// immutable at Epic portals once sent.
  static Future<bool> hasMessageBody({
    required String portalId,
    required String folder,
    required String msgId,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final file = File('${dir.path}/messages/$portalId-msg-$folder-${_csnSlug(msgId)}.json');
    return file.exists();
  }

  /// Consolidated lab batch index — small metadata-only file written
  /// at end of run, lists every captured eorderid + size + per-result
  /// error reasons. Phase 4-3 Python ingest reads this to find which
  /// HTML files to parse.
  static Future<String> writeLabBatchConsolidated({
    required String portalId,
    required List<Map<String, dynamic>> captured,
    required List<Map<String, dynamic>> errors,
    required DateTime startedAt,
    required DateTime finishedAt,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final ts = finishedAt.toUtc().toIso8601String().replaceAll(':', '-');
    final file = File('${dir.path}/$portalId-lab-batch-$ts.json');
    final payload = {
      'portal': portalId,
      'kind': 'lab-bodies',
      'startedAt': startedAt.toUtc().toIso8601String(),
      'finishedAt': finishedAt.toUtc().toIso8601String(),
      'capturedCount': captured.length,
      'errorCount': errors.length,
      'captured': captured,
      'errors': errors,
    };
    await file.writeAsString(jsonEncode(payload));
    return file.path;
  }

  /// Per-message body file — one JSON per message ID for crash safety
  /// during long batch runs.
  static Future<String> writeMessageBody({
    required String portalId,
    required String folder,
    required String id,
    required Map<String, dynamic> data,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final msgDir = Directory('${dir.path}/messages');
    await msgDir.create(recursive: true);
    final file = File('${msgDir.path}/$portalId-msg-$folder-${_csnSlug(id)}.json');
    final body = jsonEncode({
      'folder': folder,
      'id': id,
      'savedAt': DateTime.now().toUtc().toIso8601String(),
      'data': data,
    });
    await file.writeAsString(body);
    return file.path;
  }

  /// Per-batch progress manifest — small file rewritten as each message
  /// completes so a crash mid-batch leaves a recovery anchor.
  static Future<String> writeMessageBatchManifest({
    required String portalId,
    required MessageBatchManifest m,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final file = File('${dir.path}/$portalId-message-batch-manifest.json');
    await file.writeAsString(jsonEncode(m.toJson()));
    return file.path;
  }

  /// Consolidated message-bodies batch written at run end. The shape is
  /// the input the FHIR Communications translator (Phase 3-2 next
  /// commit) consumes.
  static Future<String> writeMessageBatchConsolidated({
    required String portalId,
    required List<Map<String, dynamic>> captured,
    required List<Map<String, dynamic>> errors,
    required DateTime startedAt,
    required DateTime finishedAt,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final ts = finishedAt.toUtc().toIso8601String().replaceAll(':', '-');
    final file = File('${dir.path}/$portalId-message-batch-$ts.json');
    final payload = {
      'portal': portalId,
      'startedAt': startedAt.toUtc().toIso8601String(),
      'finishedAt': finishedAt.toUtc().toIso8601String(),
      'capturedCount': captured.length,
      'errorCount': errors.length,
      'captured': captured,
      'errors': errors,
    };
    await file.writeAsString(jsonEncode(payload));
    return file.path;
  }

  /// Clinical list response — one JSON file per section per fetch.
  /// File: Documents/clinical/$portalId-<section>-<ts>.json
  /// Reading: the Phase 5 Python converter walks this dir, loads the
  /// most recent file per section, and emits FHIR resources.
  static Future<String> writeClinicalList({
    required String portalId,
    required String section,
    required Object listJson,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final clinDir = Directory('${dir.path}/clinical');
    await clinDir.create(recursive: true);
    final ts = DateTime.now().toUtc().toIso8601String().replaceAll(':', '-');
    final safeSection = section.toLowerCase().replaceAll(RegExp(r'[^a-z0-9]+'), '-');
    final file = File('${clinDir.path}/$portalId-$safeSection-$ts.json');
    await file.writeAsString(jsonEncode({
      'section': section,
      'fetchedAt': DateTime.now().toUtc().toIso8601String(),
      'list': listJson,
    }));
    return file.path;
  }

  /// Portal-scout spec — one JSON document per scout run. Captures the
  /// portal's API surface as discovered by the autonomous crawl:
  /// per-section URL, snapshot (title/heading/row patterns), and the raw
  /// XHR capture records the api-capture hook collected while that section
  /// was loaded. The host-side spec synthesizer reads this and emits
  /// per-resource scraper code.
  ///
  /// Portal id is read out of spec['portal'] which the caller already
  /// stamps with _portal.id.
  static Future<String> writeScoutSpec(Map<String, dynamic> spec) async {
    final dir = await getApplicationDocumentsDirectory();
    final ts = DateTime.now().toUtc().toIso8601String().replaceAll(':', '-');
    final portal = (spec['portal'] ?? 'unknown').toString();
    final file = File('${dir.path}/portal-scout-spec-$portal-$ts.json');
    await file.writeAsString(jsonEncode(spec));
    return file.path;
  }

  /// One unified line for the scout run's diagnostic log. Use the existing
  /// per-diag-batch chain so concurrent appends from the bridge are
  /// serialized. Pass the same batchStartedAt across the whole scout so
  /// the file name is stable.
  static Future<void> appendScoutDiagLine({
    required String portalId,
    required DateTime batchStartedAt,
    required Map<String, dynamic> event,
  }) =>
      appendDiagLine(portalId: portalId, batchStartedAt: batchStartedAt, event: event);

  /// One JSON line per discovered scout event. Each
  /// line is a self-contained JSON object — easy to grep/parse later.
  /// Metadata only (URL paths, lengths, counts); never note text or labels.
  /// File path includes the batch start timestamp so consecutive runs don't
  /// clobber each other.
  ///
  /// Writes are serialized through [_diagWriteChain] — fire-and-forget
  /// callers from the JS handler bridge can race otherwise (proven by a
  /// 33% line-corruption rate when this used naked writeAsString+
  /// FileMode.append).
  static Future<void> appendDiagLine({
    required String portalId,
    required DateTime batchStartedAt,
    required Map<String, dynamic> event,
  }) {
    final stamped = Map<String, dynamic>.from(event);
    stamped['at'] = DateTime.now().toUtc().toIso8601String();
    final line = '${jsonEncode(stamped)}\n';
    _diagWriteChain = _diagWriteChain.then((_) async {
      final dir = await getApplicationDocumentsDirectory();
      final ts = batchStartedAt.toUtc().toIso8601String().replaceAll(':', '-');
      final file = File('${dir.path}/$portalId-diag-$ts.jsonl');
      await file.writeAsString(line, mode: FileMode.append, flush: true);
    });
    return _diagWriteChain;
  }

  static Future<void> _diagWriteChain = Future.value();

  /// Aggregate "incomplete" CSNs across ALL consolidated batch files in the
  /// docs directory for a given portal. Same rank ordering as before:
  ///   'errored' < 'empty' < 'partial' < 'complete' (higher wins).
  ///
  /// Excludes:
  ///   - CSNs cleanly captured (single-note with html, or multi-note with
  ///     every sub-note having content) — done, no need to retry
  ///   - CSNs that have never appeared in any batch — never attempted on
  ///     purpose; not "failed", so not retried
  ///
  /// Used by the "Retry failed visits" menu action.
  static Future<List<String>> findIncompleteCsns({required String portalId}) async {
    final dir = await getApplicationDocumentsDirectory();
    final entries = await Directory(dir.path).list().toList();
    final batches = entries
        .whereType<File>()
        .where((f) =>
            f.path.contains('$portalId-batch-2') &&
            !f.path.contains('manifest'))
        .toList()
      ..sort((a, b) => a.path.compareTo(b.path)); // oldest → newest

    final state = <String, String>{};
    int rank(String s) => switch (s) {
      'complete' => 4,
      'partial' => 3,
      'empty' => 2,
      'errored' => 1,
      _ => 0,
    };
    void upgrade(String csn, String to) {
      final cur = state[csn];
      if (cur == null || rank(to) > rank(cur)) state[csn] = to;
    }

    for (final file in batches) {
      try {
        final raw = await file.readAsString();
        final m = jsonDecode(raw);
        if (m is! Map) continue;

        final cap = m['captured'];
        if (cap is List) {
          for (final c in cap) {
            if (c is! Map) continue;
            final csn = c['csn'];
            if (csn is! String) continue;
            final notes = c['notes'];
            if (notes is List) {
              // multi-note: complete only if every sub-note has content
              bool anyEmpty = false;
              bool anyContent = false;
              for (final n in notes) {
                if (n is Map) {
                  final len = (n['htmlLength'] is int) ? n['htmlLength'] as int : 0;
                  if (len > 0) {
                    anyContent = true;
                  } else {
                    anyEmpty = true;
                  }
                }
              }
              if (!anyContent) {
                upgrade(csn, 'errored');
              } else if (anyEmpty) {
                upgrade(csn, 'partial');
              } else {
                upgrade(csn, 'complete');
              }
            } else {
              final html = c['html'];
              if (html is String && html.isNotEmpty) {
                upgrade(csn, 'complete');
              } else {
                upgrade(csn, 'errored');
              }
            }
          }
        }

        final errs = m['errors'];
        if (errs is List) {
          for (final e in errs) {
            if (e is! Map) continue;
            final csn = e['csn'];
            final reason = e['reason'];
            if (csn is! String) continue;
            // Portal-confirmed empty signals — don't keep retrying:
            //   'no-notes-available' = portal explicitly said "No Notes
            //     Available" (the canonical empty-state panel)
            //   'no-notes-tab'       = the visit's after-visit-summary
            //     page doesn't render a Clinical Notes tab at all
            if (reason == 'no-notes-available' || reason == 'no-notes-tab') {
              upgrade(csn, 'empty');
            } else {
              upgrade(csn, 'errored');
            }
          }
        }
      } catch (_) {}
    }

    return state.entries
        .where((e) => e.value != 'complete' && e.value != 'empty')
        .map((e) => e.key)
        .toList()
      ..sort();
  }

  static String _csnSlug(String csn) {
    final safe = csn.replaceAll(RegExp(r'[^a-zA-Z0-9_-]'), '_');
    return safe.length > 32 ? safe.substring(0, 32) : safe;
  }
}

/// One CSN's payload in the consolidated batch. For single-note visits,
/// [html] holds the inline note's outerHTML and [subNotes] is null. For
/// multi-note visits, [html] is empty and [subNotes] holds the per-note
/// captures. [htmlLength]/[visibleTextLength] are the aggregate across
/// sub-notes when multi.
class CapturedNote {
  final String csn;
  final String html;
  final int htmlLength;
  final int visibleTextLength;
  final DateTime capturedAt;
  final List<SubNote>? subNotes;

  CapturedNote({
    required this.csn,
    required this.html,
    required this.htmlLength,
    required this.visibleTextLength,
    required this.capturedAt,
    this.subNotes,
  });

  bool get isMulti => subNotes != null && subNotes!.isNotEmpty;

  Map<String, dynamic> toJson() => {
    'csn': csn,
    'htmlLength': htmlLength,
    'visibleTextLength': visibleTextLength,
    'capturedAt': capturedAt.toUtc().toIso8601String(),
    'html': html,
    if (subNotes != null) 'notes': subNotes!.map((n) => n.toJson()).toList(),
  };
}

/// One row inside a multi-note visit (e.g., "Care Plan Note · Signed by
/// Jennifer Tu, RN on 3/26/2024 at 6:05 AM"). The label is the raw row
/// text from the list page; downstream parsing extracts title/signer/date.
class SubNote {
  final String label;
  final String html;
  final int htmlLength;
  final int visibleTextLength;

  SubNote({
    required this.label,
    required this.html,
    required this.htmlLength,
    required this.visibleTextLength,
  });

  Map<String, dynamic> toJson() => {
    'label': label,
    'htmlLength': htmlLength,
    'visibleTextLength': visibleTextLength,
    'html': html,
  };
}

class ScrapeError {
  final String csn;
  final int index;
  final String reason;
  final DateTime at;
  ScrapeError({
    required this.csn,
    required this.index,
    required this.reason,
    required this.at,
  });
  Map<String, dynamic> toJson() => {
    'csn': csn,
    'index': index,
    'reason': reason,
    'at': at.toUtc().toIso8601String(),
  };
}

class BatchManifest {
  final DateTime startedAt;
  final int totalCount;
  final int currentIndex;
  final int capturedCount;
  final int errorCount;
  final String? currentCsn;
  BatchManifest({
    required this.startedAt,
    required this.totalCount,
    required this.currentIndex,
    required this.capturedCount,
    required this.errorCount,
    this.currentCsn,
  });
  Map<String, dynamic> toJson() => {
    'startedAt': startedAt.toUtc().toIso8601String(),
    'totalCount': totalCount,
    'currentIndex': currentIndex,
    'capturedCount': capturedCount,
    'errorCount': errorCount,
    'currentCsn': currentCsn,
  };
}

class MessageBatchManifest {
  final DateTime startedAt;
  final int totalCount;
  final int currentIndex;
  final int capturedCount;
  final int errorCount;
  final String? currentFolder;
  final String? currentId;
  MessageBatchManifest({
    required this.startedAt,
    required this.totalCount,
    required this.currentIndex,
    required this.capturedCount,
    required this.errorCount,
    this.currentFolder,
    this.currentId,
  });
  Map<String, dynamic> toJson() => {
    'startedAt': startedAt.toUtc().toIso8601String(),
    'totalCount': totalCount,
    'currentIndex': currentIndex,
    'capturedCount': capturedCount,
    'errorCount': errorCount,
    'currentFolder': currentFolder,
    'currentId': currentId,
  };
}
