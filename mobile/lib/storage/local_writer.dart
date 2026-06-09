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
/// All files land in the app's documents directory. On iOS Simulator
/// that's under ~/Library/Developer/CoreSimulator/Devices/<dev>/
/// data/Containers/Data/Application/<app>/Documents/.
class LocalWriter {
  /// Write one captured note to its own file. Called after every successful
  /// scrape so partial progress survives a crash.
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

  static String _csnSlug(String csn) {
    final safe = csn.replaceAll(RegExp(r'[^a-zA-Z0-9_-]'), '_');
    return safe.length > 32 ? safe.substring(0, 32) : safe;
  }
}

class CapturedNote {
  final String csn;
  final String html;
  final int htmlLength;
  final int visibleTextLength;
  final DateTime capturedAt;
  CapturedNote({
    required this.csn,
    required this.html,
    required this.htmlLength,
    required this.visibleTextLength,
    required this.capturedAt,
  });
  Map<String, dynamic> toJson() => {
    'csn': csn,
    'htmlLength': htmlLength,
    'visibleTextLength': visibleTextLength,
    'capturedAt': capturedAt.toUtc().toIso8601String(),
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
