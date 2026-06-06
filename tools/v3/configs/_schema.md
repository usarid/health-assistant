# v3 scrape-config schema

Each portal gets one JSON config file. Top-level shape:

```jsonc
{
  "portal_id": "ucsf.mychart",          // stable ID, used in provenance tags
  "name": "UCSF MyChart",                // human-readable name
  "base_path": "/UCSFMyChart",           // first path segment in URLs
  "host_match": ["mychart.ucsf.edu"],    // hosts the runtime should run against

  "auth": {
    "token_selector": "input[name=\"__RequestVerificationToken\"]",
    "token_header": "__requestverificationtoken",
    "page_nonce_regex": null              // optional; only set if portal requires PageNonce
  },

  "jobs": {
    "<job_name>": {
      "discovery": { /* see below */ },
      "filter": null,                     // optional structured filter spec; see "Filter spec" section
      "endpoint": { /* see below */ },
      "depends_on": null,                 // optional; e.g. "visits" means use prior job's results
      "max_concurrency": 1,
      "rate_limit_ms": 200
    }
  }
}
```

## Discovery modes

Two modes are supported.

### `epic_rendered_data`

Reads the work-list from Epic's SPA runtime state. Per-portal: which instance.
Optionally drives a UI paginator before reading.

```jsonc
"discovery": {
  "mode": "epic_rendered_data",
  "instance": 7,                          // Epic.PatientAccess.Components.__Instances[N].RenderedData
  "paginator": {                          // optional — set when the list paginates via a "Load more" button
    "type": "click_until_gone",           // currently the only supported type
    "button_text": "load more past visits",  // case-insensitive substring match on button text
    "max_iterations": 50,                 // safety cap; default 50
    "settle_ms": 200,                     // wait after each click before next iteration; default 200
    "wait_for_growth_ms": 6000,           // wait this long for RenderedData to grow after a click; default 6000
    "stop_when_seen": {                   // optional — for incremental scrapes
      "path": "Csn",                      // path on a RenderedData item
      "value": "PRIOR_HIGH_WATERMARK"     // stop paginating once any item matches
    }
  }
}
```

### `epic_component_data`

Reads the work-list from an Epic SPA component's `.Data.<path>` rather than from `.RenderedData`. Some Epic components (e.g. Stanford's UpcomingVisits component, instance 5) keep their work-list split across multiple sibling arrays under `.Data` — this mode concatenates the listed paths.

```jsonc
"discovery": {
  "mode": "epic_component_data",
  "instance": 5,
  "data_paths": ["NextNDaysVisits", "LaterVisitsList", "InProgressVisits"]
}
```

Each path is read with the same null-tolerant getter the filter spec uses. If a path resolves to an array its entries are appended to the work-list; if it resolves to a scalar/object it's appended as a single entry. No paginator support — upcoming-style lists are bounded.

### `dom_href_scan`

Walks `<a href>` elements in the rendered inbox / list, extracting query parameters from each href as work-items.

```jsonc
"discovery": {
  "mode": "dom_href_scan",
  "selector": "a[href]",
  "params": ["id", "org"]                 // params to extract from each href
}
```

### `from_dependency`

For follow-up jobs: use the output of a previous job as input. Example: notes scrape iterates the visits-with-shareable-notes from the visits job.

```jsonc
"discovery": { "mode": "from_dependency" }
```

## Filter spec

Filters are structured JSON, evaluated by the runtime without `eval` or `new Function` (those are CSP-blocked on most real portals, see 2026-06-06 UCSF live-test findings). Accepted shapes:

```jsonc
null | true                        // accept all (no filter)
false                              // reject all
"a.b.c"                            // shorthand: path must be truthy
[spec, spec, …]                    // implicit AND
{ "and": [spec, spec, …] }
{ "or":  [spec, spec, …] }
{ "not": spec }
{ "path_truthy": "a.b.c" }
{ "path_equals": ["a.b.c", value] }
```

Paths are dotted; `getPath` is null-tolerant (no exception on missing intermediates). The match value in `path_equals` is compared with strict equality (`===`).

Examples — see `ucsf.json` for working uses:

```jsonc
// All visits where IsLocal is exactly true
{ "path_equals": ["IsLocal", true] }

// Visits with a clinical note that the patient can view
{ "and": [
  { "path_equals": ["response.notesInfo.isAtLeastOneNoteShareable", true] },
  "response.notesInfo.notesReport.reportID"     // truthy-shorthand
]}
```

## Endpoint declaration

```jsonc
"endpoint": {
  "path": "/api/visits/past-details/GetVisitDetailsPast",
  "method": "POST",
  "headers": { /* added on top of base headers; usually empty */ },
  "body_template": {
    "csn": "{item.Csn}",                  // resolved against the current item
    "eorgID": ""                          // literal
  }
}
```

### Template tokens

In `body_template` values:

- `"{item.<path>}"` — dotted path into the current work-item. Throws if missing.
- `"{item.<path>?}"` — same, but resolves to `""` if missing.
- `"{auto_nonce}"` — random 32-char lowercase-hex string, freshly generated per request.
- `"{auto_seq}"` — monotonically-increasing integer, scoped to the job run.
- `"{auto_iso_now}"` — current time as ISO8601.
- Anything else is treated as a literal.

Strings interpolate (`"EID-{auto_seq}"`). Non-string values pass through unchanged.

## What's deliberately NOT in the schema

- **Response parsing.** The runtime returns the raw API response JSON for each
  item, paired with the item itself. Normalization to FHIR happens in
  Python converters (the `tools/v2/convert_*.py` pattern), not in the scrape
  runtime. This keeps the JS surface tiny.
- **Pagination.** No Epic portal's relevant endpoints paginate the responses we
  care about; the discovery layer returns the full list and we iterate. If a
  future portal does paginate, a third discovery mode would handle it.
- **Auth-flow itself.** The runtime assumes the user is already logged in. The
  v3 mobile app handles login via WebView; for console-paste testing the
  developer logs in manually before pasting the runtime.

## Provenance contract integration

Every job execution emits a provenance block per the contract spec in
`docs/CONCLUSIONS_LOG.md`:

```json
{
  "_provenance": {
    "scraped_at": "2026-06-04T17:32:11Z",
    "portal_id": "ucsf.mychart",
    "config_version": "ucsf.v1",
    "runtime_version": "v3.0.0",
    "job": "notes",
    "endpoint": "/api/report-content/LoadReportContent",
    "source_item_key": "<the work-item's CSN or thread ID>"
  }
}
```

Downstream converters in `tools/v2/` already know how to read this block and
map it to the right FHIR `meta.tag` entries.
