import 'package:flutter/services.dart' show rootBundle, AssetManifest;
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'portal_config.dart';
export 'portal_config.dart';

/// Loads every assets/portals/*.json at app startup and exposes the
/// currently-active portal. Callers use PortalRegistry.instance.active
/// wherever they used to reference StanfordConfig.X.
///
/// The active portal id is persisted to the iOS Keychain (via
/// flutter_secure_storage, already used for credentials) so a user's
/// picker choice survives app kill/relaunch — but not app reinstall,
/// same as their saved credentials.
class PortalRegistry {
  static PortalRegistry? _instance;
  static const _activePortalKey = 'active_portal_id';
  static const _storage = FlutterSecureStorage();

  final Map<String, PortalConfig> _byId;
  String _activeId;

  PortalRegistry._(this._byId, this._activeId);

  static PortalRegistry get instance {
    final r = _instance;
    if (r == null) {
      throw StateError(
        'PortalRegistry not initialized — call PortalRegistry.load() '
        'from main() before runApp().',
      );
    }
    return r;
  }

  /// One-shot init. Reads AssetManifest.json to discover
  /// assets/portals/*.json, decodes each, and selects the active portal
  /// (persisted from a prior session if valid, else alphabetically-first).
  /// Throws if no portal configs are shipped — that's a build error.
  static Future<PortalRegistry> load() async {
    final manifest = await AssetManifest.loadFromAssetBundle(rootBundle);
    final portalKeys = manifest
        .listAssets()
        .where((k) => k.startsWith('assets/portals/') && k.endsWith('.json'))
        .toList()
      ..sort();

    if (portalKeys.isEmpty) {
      throw StateError(
        'No portal configs found under assets/portals/. Did you forget '
        'to declare it in pubspec.yaml?',
      );
    }

    final byId = <String, PortalConfig>{};
    for (final key in portalKeys) {
      final src = await rootBundle.loadString(key);
      final cfg = PortalConfig.fromJsonString(src);
      if (byId.containsKey(cfg.id)) {
        throw StateError(
          'Duplicate portal id "${cfg.id}" — one appears twice in '
          'assets/portals/. Check the "id" field in each JSON.',
        );
      }
      byId[cfg.id] = cfg;
    }

    // Prefer a persisted picker choice from a prior session. If the
    // persisted id is unknown (portal was removed since), or nothing
    // was persisted, fall back to the alphabetically-first portal.
    String? persisted;
    try {
      persisted = await _storage.read(key: _activePortalKey);
    } catch (_) {
      // Keychain read can throw when entitlements aren't set up —
      // that's task_b5685403 territory. Fall back silently.
      persisted = null;
    }
    final activeId = (persisted != null && byId.containsKey(persisted))
        ? persisted
        : byId.keys.first;

    _instance = PortalRegistry._(byId, activeId);
    return _instance!;
  }

  /// The currently-active portal. Everything in the app that used to
  /// reference StanfordConfig.X now goes through this.
  PortalConfig get active => _byId[_activeId]!;

  /// All portal configs, in the order their JSON files sort alphabetically
  /// (deterministic for UI listings).
  List<PortalConfig> get all {
    final ids = _byId.keys.toList()..sort();
    return [for (final id in ids) _byId[id]!];
  }

  /// Set the active portal + persist the choice for next launch.
  /// Throws on unknown id. Keychain write failures are swallowed
  /// (the in-memory switch still succeeds — the choice just won't
  /// survive relaunch until the underlying entitlement is fixed).
  Future<void> setActive(String portalId) async {
    if (!_byId.containsKey(portalId)) {
      throw ArgumentError('Unknown portal id: $portalId');
    }
    _activeId = portalId;
    try {
      await _storage.write(key: _activePortalKey, value: portalId);
    } catch (_) {
      // Same rationale as the read in load() — best-effort persistence.
    }
  }
}
