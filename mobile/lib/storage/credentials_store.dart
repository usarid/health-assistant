import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Per-portal credential persistence.
///
/// Backing store on iOS: Keychain Services (kSecAttrAccessibleWhenUnlocked).
/// Means the credential is encrypted at rest, only accessible when the
/// device is unlocked, and protected by Secure Enclave on supported
/// hardware. Same primitive every password manager and Safari's
/// "save password" feature uses.
///
/// Single-portal-key scheme: each portal has at most one stored credential.
/// Keyed by a portal slug ("stanford", "ucsf", ...) so iteration 3+ can
/// store credentials for multiple portals without collision.
class CredentialsStore {
  static const _storage = FlutterSecureStorage(
    iOptions: IOSOptions(
      accessibility: KeychainAccessibility.unlocked,
    ),
  );

  static String _emailKey(String portal) => 'portal.$portal.email';
  static String _passwordKey(String portal) => 'portal.$portal.password';

  /// Save a credential pair. Overwrites any prior credential for the portal.
  static Future<void> save({
    required String portal,
    required String email,
    required String password,
  }) async {
    await _storage.write(key: _emailKey(portal), value: email);
    await _storage.write(key: _passwordKey(portal), value: password);
  }

  /// Read a stored credential pair. Returns null for either field if absent.
  static Future<StoredCredential?> read(String portal) async {
    final email = await _storage.read(key: _emailKey(portal));
    final password = await _storage.read(key: _passwordKey(portal));
    if (email == null || password == null) return null;
    return StoredCredential(email: email, password: password);
  }

  /// Wipe a portal's stored credential. Used by the in-app "forget my login"
  /// affordance.
  static Future<void> clear(String portal) async {
    await _storage.delete(key: _emailKey(portal));
    await _storage.delete(key: _passwordKey(portal));
  }

  static Future<bool> has(String portal) async {
    return (await read(portal)) != null;
  }
}

class StoredCredential {
  final String email;
  final String password;
  StoredCredential({required this.email, required this.password});
}
