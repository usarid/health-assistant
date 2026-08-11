import 'package:flutter/services.dart' show rootBundle, AssetManifest;

import 'portal_config.dart';
export 'portal_config.dart';

/// Loads every assets/portals/*.json at app startup and exposes the
/// currently-active portal. Callers use PortalRegistry.instance.active
/// wherever they used to reference StanfordConfig.X.
///
/// R-1 behavior: the active portal is the first (or only) loaded portal.
/// R-4 will add a persistent user-selected active portal when we ship a
/// picker UI alongside ucsf.json.
class PortalRegistry {
  static PortalRegistry? _instance;

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
  /// assets/portals/*.json, decodes each, and picks the first as active.
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

    // portalKeys is sorted; byId was populated in that order; Dart's
    // default Map preserves insertion order → byId.keys.first is the
    // alphabetically-first portal id. Deterministic default.
    _instance = PortalRegistry._(byId, byId.keys.first);
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

  /// Set the active portal. Used by the picker UI once >1 portal exists.
  /// Throws on unknown id.
  void setActive(String portalId) {
    if (!_byId.containsKey(portalId)) {
      throw ArgumentError('Unknown portal id: $portalId');
    }
    _activeId = portalId;
  }
}
