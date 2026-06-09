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

  /// Login landing page; opens here on app launch.
  static const String loginUrl = '$wrapperOrigin/#/';

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

  /// Mobile Safari UA — Stanford may serve different HTML/JS to mobile
  /// vs desktop. Using a real-iPhone-flavored UA from the simulator means
  /// what we test here matches what an iOS device sees.
  static const String mobileUserAgent =
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) '
      'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 '
      'Mobile/15E148 Safari/604.1';
}
