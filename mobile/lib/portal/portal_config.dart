import 'dart:convert';

/// Portal configuration loaded from assets/portals/*.json at app startup.
///
/// Contract: this holds VALUES only (hostnames, URLs, auth field names,
/// keepalive endpoints, ingest-side references). Meaty scraping logic
/// lives in code, not config — see lib/scrape/scrape_jobs.dart.
/// Selectors that vary per portal read their variance from here.
///
/// The same JSON is also read by tools/portal_registry.py on the ingest
/// side, keeping mobile-scrape config and FHIR-ingest config in a single
/// source of truth per portal.
class PortalConfig {
  final String id;
  final String name;
  final String vendor;

  final PortalHosts hosts;
  final String basePath;
  final PortalAuth auth;
  final PortalUrls urls;
  final List<String> keepaliveUrls;
  final String userAgent;

  /// Fields consumed by the Python FHIR converters, not the mobile app.
  /// Present in every portal JSON but never accessed in Dart today —
  /// exposed as-is on the config object in case a future mobile feature
  /// wants them (e.g. showing "Patient/xxx" in a debug pane).
  final PortalIngest ingest;

  const PortalConfig({
    required this.id,
    required this.name,
    required this.vendor,
    required this.hosts,
    required this.basePath,
    required this.auth,
    required this.urls,
    required this.keepaliveUrls,
    required this.userAgent,
    required this.ingest,
  });

  factory PortalConfig.fromJsonString(String source) {
    final map = json.decode(source) as Map<String, dynamic>;
    return PortalConfig.fromMap(map);
  }

  factory PortalConfig.fromMap(Map<String, dynamic> m) {
    return PortalConfig(
      id: m['id'] as String,
      name: m['name'] as String,
      vendor: m['vendor'] as String,
      hosts: PortalHosts.fromMap(m['hosts'] as Map<String, dynamic>),
      basePath: m['basePath'] as String,
      auth: PortalAuth.fromMap(m['auth'] as Map<String, dynamic>),
      urls: PortalUrls.fromMap(m['urls'] as Map<String, dynamic>),
      keepaliveUrls:
          (m['keepaliveUrls'] as List).map((e) => e as String).toList(),
      userAgent: m['userAgent'] as String,
      ingest: PortalIngest.fromMap(m['ingest'] as Map<String, dynamic>),
    );
  }
}

class PortalIngest {
  /// FHIR Patient reference the converters attach to every resource
  /// (e.g. "Patient/eLnGIs…"). Per-portal because the vault holds one
  /// sub-identity Patient per data source.
  final String patientRef;

  /// Prefix for FHIR Identifier.system values. Converters append the
  /// data-type suffix (":allergy", ":message", etc.). Kept per portal
  /// because existing data uses institution-specific systems and we
  /// don't want to re-key on migration.
  final String identifierSystemPrefix;

  /// Value stamped on the `urn:bina:src-portal` meta tag — the
  /// coarse-grained "which portal did this come from" marker.
  final String srcPortalTag;

  /// Short slug used in `tools/v3/out/<slug>-…` directory names on the
  /// ingest side (e.g. `stanford-clinical`, `stanford-labs`).
  final String inputDirName;

  const PortalIngest({
    required this.patientRef,
    required this.identifierSystemPrefix,
    required this.srcPortalTag,
    required this.inputDirName,
  });

  factory PortalIngest.fromMap(Map<String, dynamic> m) => PortalIngest(
        patientRef: m['patientRef'] as String,
        identifierSystemPrefix: m['identifierSystemPrefix'] as String,
        srcPortalTag: m['srcPortalTag'] as String,
        inputDirName: m['inputDirName'] as String,
      );
}

class PortalHosts {
  /// The origin (host only) users navigate to; may be null for
  /// single-origin portals where API and SPA share one host.
  final String? wrapper;

  /// The API host — always non-null. For single-origin portals this
  /// equals the host the SPA is served from.
  final String api;

  const PortalHosts({required this.wrapper, required this.api});

  factory PortalHosts.fromMap(Map<String, dynamic> m) => PortalHosts(
        wrapper: m['wrapper'] as String?,
        api: m['api'] as String,
      );

  /// The origin where session cookies live and where user-facing pages
  /// are served. Equals `wrapper` when the portal splits SPA and API
  /// across two origins (e.g. Stanford: myhealth.* vs mychart.*), and
  /// falls back to `api` for single-origin portals (e.g. UCSF).
  String get userFacing => wrapper ?? api;
}

class PortalAuth {
  /// Name of the hidden input on shell pages carrying the anti-CSRF token
  /// we must extract and echo back in a request header.
  final String csrfInputName;

  /// Name of the request header the API expects the CSRF token in.
  final String csrfHeaderName;

  const PortalAuth({
    required this.csrfInputName,
    required this.csrfHeaderName,
  });

  factory PortalAuth.fromMap(Map<String, dynamic> m) => PortalAuth(
        csrfInputName: m['csrfInputName'] as String,
        csrfHeaderName: m['csrfHeaderName'] as String,
      );
}

class PortalUrls {
  /// URL the WebView opens on app launch.
  final String login;

  /// Explicit signed-in landing the orchestrator can navigate to after
  /// detecting post-auth (some portals route-bounce during MFA and don't
  /// fire onLoadStop reliably).
  final String signedInHome;

  /// Substring that appears in the WebView URL once auth is established.
  final String signedInMarker;

  /// URL template with %CSN% placeholder for individual visit AVS pages.
  final String visitDetailPattern;

  final String messageInbox;
  final String messageOutbox;

  /// Template with %FOLDER% and %MSG_ID% placeholders.
  final String messageDetailPattern;

  const PortalUrls({
    required this.login,
    required this.signedInHome,
    required this.signedInMarker,
    required this.visitDetailPattern,
    required this.messageInbox,
    required this.messageOutbox,
    required this.messageDetailPattern,
  });

  factory PortalUrls.fromMap(Map<String, dynamic> m) => PortalUrls(
        login: m['login'] as String,
        signedInHome: m['signedInHome'] as String,
        signedInMarker: m['signedInMarker'] as String,
        visitDetailPattern: m['visitDetailPattern'] as String,
        messageInbox: m['messageInbox'] as String,
        messageOutbox: m['messageOutbox'] as String,
        messageDetailPattern: m['messageDetailPattern'] as String,
      );
}
