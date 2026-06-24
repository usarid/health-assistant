/// Stanford portal config — mirrors the relevant fields from
/// tools/v3/configs/stanford.json. Iteration 3 will load the v3 JSON
/// configs at runtime so the same Dart code drives every portal; for now
/// these are inline constants so iteration 2 stays focused on the loop +
/// keepalive scaffolding.
class StanfordConfig {
  /// Base of the wrapper-origin URLs we navigate to per visit. Stanford
  /// gates Clinical Notes content on Origin: myhealth.* + Sec-Fetch-Dest:
  /// document, so all per-visit navigation MUST happen here (see C-021).
  static const String wrapperOrigin = 'https://myhealth.stanfordhealthcare.org';

  /// URL pattern for an individual visit's after-visit-summary page.
  /// %CSN% is the URL-encoded Csn (the WP-24… encrypted token from
  /// Epic's RenderedData).
  static const String visitDetailUrlPattern =
      '$wrapperOrigin/signedin/appointments/after-visit-summary/csn=%CSN%&encType=3';

  /// Messaging URLs. Both folder pages render Epic-style paginated message
  /// lists; row links target /signedin/messages/detail/<folder>/<msgId>.
  static const String messageInboxUrl  = '$wrapperOrigin/signedin/messages/inbox';
  static const String messageOutboxUrl = '$wrapperOrigin/signedin/messages/outbox';
  static const String messageDetailUrlPattern =
      '$wrapperOrigin/signedin/messages/detail/%FOLDER%/%MSG_ID%';

  /// Login landing page; opens here on app launch.
  static const String loginUrl = '$wrapperOrigin/#/';

  /// Canonical signed-in landing the portal-scout navigates to before
  /// enumerating. Stanford's SPA route-bounces back to '/#/' (sign-in
  /// shell) briefly during the post-MFA auth handshake without firing
  /// onLoadStop, so relying on Dart's last-known URL is unreliable —
  /// we navigate explicitly to a known signed-in page first.
  static const String signedInHomeUrl = '$wrapperOrigin/signedin/home';

  /// Marker the WebView's URL transitions through once auth is established.
  /// We watch for it to enable the "Scrape all" action.
  static const String signedInMarker = '/signedin/';

  /// Keepalive endpoints. Two URLs because Stanford has independent session
  /// timers on the wrapper origin and the API origin — pinging only one
  /// lets the other expire mid-scrape (proven 2026-06-08).
  ///
  /// Both must be hit via fetch() with credentials:'include'; Image() GET
  /// returns 503 (the server insists on certain Accept headers / non-image
  /// response negotiation; also proven 2026-06-08).
  static const List<String> keepaliveUrls = [
    'https://mychart.stanfordhealthcare.org/myhealth_sso/keepalive.asp',
    '$wrapperOrigin/signedin/keepalive.asp',
  ];

  /// Desktop Safari UA. We intentionally do NOT use a mobile UA: Stanford
  /// MyHealth detects mobile browsers and serves a "Use the MyHealth app"
  /// interstitial whose "Continue to the website" button doesn't reliably
  /// work in an embedded WebView. The whole scrape architecture (wrapper-
  /// origin after-visit-summary URLs etc.) was designed against Stanford's
  /// desktop site anyway — the mobile site has a different structure.
  /// Using desktop Safari UA makes the WebView get the same HTML/JS shape
  /// we know how to scrape.
  static const String mobileUserAgent =
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/605.1.15 (KHTML, like Gecko) '
      'Version/17.0 Safari/605.1.15';
}
