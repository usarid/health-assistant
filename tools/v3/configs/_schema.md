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
      "filter": null,                     // optional JS expr against item; e.g. "item.IsLocal === true"
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

```jsonc
"discovery": {
  "mode": "epic_rendered_data",
  "instance": 7,                          // Epic.PatientAccess.Components.__Instances[N].RenderedData
  "path_in_state": null                   // optional; "instances[7].RenderedData" if Epic ever changes
}
```

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
