import 'dart:convert';
import 'dart:io';
import 'package:path_provider/path_provider.dart';

/// Day-one: write one captured note JSON to the app's documents directory.
///
/// On macOS sim / desktop the docs dir is under
/// ~/Library/Containers/<bundle-id>/Data/Documents. On iOS device it's the
/// app's sandbox-private documents container. `path_provider` abstracts this.
class LocalWriter {
  static Future<String> writeNote(String csn, String html) async {
    final dir = await getApplicationDocumentsDirectory();
    // Filename-safe CSN slice for the filename only — full CSN is preserved
    // inside the JSON body.
    final safe = csn.replaceAll(RegExp(r'[^a-zA-Z0-9_-]'), '_');
    final slug = safe.length > 32 ? safe.substring(0, 32) : safe;
    final file = File('${dir.path}/stanford-test-note-$slug.json');
    final body = jsonEncode({
      'csn': csn,
      'savedAt': DateTime.now().toUtc().toIso8601String(),
      'htmlLength': html.length,
      'html': html,
    });
    await file.writeAsString(body);
    return file.path;
  }
}
