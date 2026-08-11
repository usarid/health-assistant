import 'dart:convert';

/// Portal configuration loaded from assets/portals/*.json at app startup.
///
/// Contract: this holds VALUES only (hostnames, URLs, auth field names,
/// keepalive endpoints). Meaty scraping logic lives in code, not config —
/// see lib/scrape/scrape_jobs.dart. Selectors that vary per portal read
/// their variance from here.
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
    );
  }
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
