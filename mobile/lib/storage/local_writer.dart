import 'dart:convert';
import 'dart:io';
import 'package:path_provider/path_provider.dart';

/// Persistence for scraped clinical notes.
///
/// Two output shapes:
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
  static Future<String> writeNote(String csn, String html) async {
    final dir = await getApplicationDocumentsDirectory();
    final notesDir = Directory('${dir.path}/notes');
    await notesDir.create(recursive: true);
    final file = File('${notesDir.path}/stanford-note-${_csnSlug(csn)}.json');
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
  static Future<String> writeMultiNote(String csn, List<SubNote> notes) async {
    final dir = await getApplicationDocumentsDirectory();
    final notesDir = Directory('${dir.path}/notes');
    await notesDir.create(recursive: true);
    final file = File('${notesDir.path}/stanford-multinote-${_csnSlug(csn)}.json');
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
  static Future<String> writeBatchManifest(BatchManifest m) async {
    final dir = await getApplicationDocumentsDirectory();
    final file = File('${dir.path}/stanford-batch-manifest.json');
    await file.writeAsString(jsonEncode(m.toJson()));
    return file.path;
  }

  /// Write the consolidated batch — all captured notes + the error list —
  /// at the end of a run. This is what iteration 3 will POST to the
  /// BinaHealth backend.
  static Future<String> writeConsolidated({
    required List<CapturedNote> captured,
    required List<ScrapeError> errors,
    required DateTime startedAt,
    required DateTime finishedAt,
  }) async {
    final dir = await getApplicationDocumentsDirectory();
    final ts = finishedAt.toUtc().toIso8601String().replaceAll(':', '-');
    final file = File('${dir.path}/stanford-batch-$ts.json');
    final payload = {
      'portal': 'stanford',
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

  /// Append one diag event to the current batch's diag JSONL file. Each
  /// line is a self-contained JSON object — easy to grep/parse later.
  /// Metadata only (URL paths, lengths, counts); never note text or labels.
  /// File path includes the batch start timestamp so consecutive runs don't
  /// clobber each other.
  ///
  /// Writes are serialized through [_diagWriteChain] — fire-and-forget
  /// callers from the JS handler bridge can race otherwise (proven by a
  /// 33% line-corruption rate when this used naked writeAsString+
  /// FileMode.append).
  static Future<void> appendDiagLine(DateTime batchStartedAt, Map<String, dynamic> event) {
    final stamped = Map<String, dynamic>.from(event);
    stamped['at'] = DateTime.now().toUtc().toIso8601String();
    final line = '${jsonEncode(stamped)}\n';
    _diagWriteChain = _diagWriteChain.then((_) async {
      final dir = await getApplicationDocumentsDirectory();
      final ts = batchStartedAt.toUtc().toIso8601String().replaceAll(':', '-');
      final file = File('${dir.path}/stanford-diag-$ts.jsonl');
      await file.writeAsString(line, mode: FileMode.append, flush: true);
    });
    return _diagWriteChain;
  }

  static Future<void> _diagWriteChain = Future.value();

  /// Aggregate "incomplete" CSNs across ALL consolidated batch files in the
  /// docs directory — the union of:
  ///   - CSNs that errored in any batch and have never since been captured
  ///   - CSNs captured as multi-note with at least one sub-note still empty
  ///     (the old 1500-char threshold rejected short notes; the new code
  ///     captures them, so these CSNs are worth re-running)
  ///
  /// Excludes:
  ///   - CSNs cleanly captured (single-note with html, or multi-note with
  ///     every sub-note having content) — done, no need to retry
  ///   - CSNs that have never appeared in any batch — never attempted on
  ///     purpose; not "failed", so not retried
  ///
  /// Used by the "Retry failed visits" menu action.
  static Future<List<String>> findIncompleteCsns() async {
    final dir = await getApplicationDocumentsDirectory();
    final entries = await Directory(dir.path).list().toList();
    final batches = entries
        .whereType<File>()
        .where((f) =>
            f.path.contains('stanford-batch-2') &&
            !f.path.contains('manifest'))
        .toList()
      ..sort((a, b) => a.path.compareTo(b.path)); // oldest → newest

    // Track the BEST state we've seen for each CSN across all batches:
    // 'errored' < 'partial' < 'complete' (higher wins).
    final state = <String, String>{};
    int rank(String s) => switch (s) { 'complete' => 3, 'partial' => 2, 'errored' => 1, _ => 0 };
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
            if (csn is String) upgrade(csn, 'errored');
          }
        }
      } catch (_) {}
    }

    return state.entries
        .where((e) => e.value != 'complete')
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
