# Scraper architecture v3

The v1/v2 scrapers (`ingest/scrapers/scrape_*.js`) are console-paste scripts
hardcoded to specific portals. Adding a new portal means writing a new script;
adapting to a portal UI change means editing many scripts.

v3 splits scraping into two layers:

- **Runtime** (generic, ~one file, lives in the mobile app and the v3 tooling).
  Knows how to authenticate against an Epic-based MyChart portal, discover a
  work-list, iterate, and POST to declared API endpoints. Knows nothing about
  any specific portal.
- **Config** (per portal, JSON). Declares: base path, where the work-list
  lives, the names of the API endpoints, and the shape of the request bodies.

A new portal is a config-only addition. A portal UI change is a config edit.
The runtime never has to change for portal-specific reasons.

This is the architecture the mobile app's "remote-config-driven scraper" model
needs: ship the runtime in the app; pull configs from a server at run time;
update configs without an app update when a portal tweaks its DOM or API.

## Layout

```
tools/v3/
├── README.md                       this file
├── configs/
│   ├── _schema.md                  config-schema documentation
│   ├── ucsf.json                   reference implementation — works today
│   └── stanford.template.json      starter template with portal-specific TODOs
└── runtime/
    └── scrape_runtime.js           generic runtime (browser-runnable)
```

## What v3 explicitly does NOT do (yet)

- **Does not handle DOM scraping of arbitrary rendered text** — Epic's API endpoints
  return clean JSON, which is what we want per P-STRUCTURED-FIRST. If a portal's
  data isn't available via API, falling back to DOM scrape is a separate runtime mode
  not implemented in this first cut.
- **Does not handle non-Epic portals.** All known Epic-based customer instances
  share the anti-forgery-token + JSON-API pattern. Cerner, Meditech, Athena would
  need different runtime modes.
- **Does not solve auth.** The runtime assumes the user is already logged into
  the portal and the runtime is running in a context where session cookies and
  the anti-forgery token are accessible (a browser tab on the portal, or a
  WebView in a mobile app rendering the portal).

## What lives where

| Concern | Where |
|---|---|
| Anti-forgery token extraction | runtime — assumes `input[name="__RequestVerificationToken"]` is universal across Epic portals (verified at 3 portals so far) |
| Discovery: Epic SPA `RenderedData` | runtime, parameterized by `instance` number in config |
| Discovery: DOM href scan | runtime, parameterized by selector + params in config |
| Base path (`/UCSFMyChart`, `/MyHealth`, etc.) | config |
| Per-endpoint API path | config |
| Request body shape | config (with `{item.X}` templates + `{auto_nonce}` / `{auto_seq}` runtime values) |
| Filter logic | config (a JS expression evaluated against the item) |
| Job dependencies (notes depend on visits) | config |
| Response parsing | runtime (returns raw JSON) — caller normalizes |

## How to add a new portal

1. Copy `configs/stanford.template.json` to `configs/<portal>.json`.
2. Fill in the TODOs by inspecting the portal in browser DevTools — see the
   "discovery procedure" comment in the template.
3. Run a one-job dry run via the runtime; verify the response shape is what
   you expect.
4. Hand the JSON back; the runtime executes the full pipeline.

## Per-portal discovery procedure (what to look for in DevTools)

When adapting to a new Epic portal, capture:

1. **Base path.** First segment after the host. UCSF: `/UCSFMyChart`. MSKCC: `/MyChart`. Stanford: `/MyHealth`. Look at any in-portal navigation link.
2. **Epic component instance for the work-list.** In console: `Epic.PatientAccess.Components.__Instances`. Find the index whose `.RenderedData` matches the rendered list on screen (visits, notes, messages, etc.). UCSF visits = 7; other portals will likely differ.
3. **Endpoint paths.** Open the Network tab; trigger the action (click a visit, expand a note); copy the path of the POST that returns the structured data.
4. **Request body fields.** From the same Network capture, inspect the POST body. Map each field to either `{item.<source-path>}` or `{auto_<runtime-value>}` or a literal.

This procedure is approximately what `mychart_internal_api.js` and
`network_interceptor.js` (legacy) help automate. The v3 runtime makes the
captured info portable — once described in a config it works the same way
across re-scrapes.
