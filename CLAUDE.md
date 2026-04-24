# Bina Health — Personal Health Vault

## What This Is

A self-hosted personal health record system that aggregates clinical data from multiple institutions (Stanford, UCSF, Mayo, MSKCC, Sutter), Apple Health wearables, and Epic on FHIR into a unified FHIR R4 database. Includes an AI assistant (Claude), medication reconciliation, trend analysis, and a reminder system.

Built by Uri Sarid. Single-user, runs on a Mac Mini behind a Cloudflare Tunnel at `bina.saridium.com`.

## Architecture

```
Browser → Cloudflare Tunnel → nginx (port 3080/HTTP, 3000/HTTPS)
                                 ├── /api/*          → FastAPI (port 8000)
                                 ├── /fhir/*         → HAPI FHIR (port 8080)
                                 └── /*              → static web/index.html
```

**5 Docker services** (docker-compose.yml):
- **postgres** — PostgreSQL 16 backing HAPI FHIR
- **opensearch** — Full-text search for FHIR resources
- **hapi** — HAPI FHIR R4 server (Java/Spring Boot)
- **api** — FastAPI backend (Python), all application logic
- **web** — nginx reverse proxy + static frontend

**Databases:**
- PostgreSQL (HAPI-managed) — all FHIR resources
- SQLite (`/data/chat.db`) — chat threads, med overrides, condition overrides, reminders, Epic tokens, user preferences
- OpenSearch — full-text index of FHIR resources

All ports bound to `127.0.0.1` — external access via Cloudflare Tunnel only.

## Key Files

### API Layer (`api/`)

| File | Purpose | Key Routes |
|------|---------|------------|
| `app.py` | Main app, search, health check | `GET /api/search`, `GET /api/health` |
| `assistant.py` | AI chat with streaming SSE | `POST /api/assistant/chat`, threads CRUD, `POST /api/assistant/execute-action` |
| `meds.py` | Medication reconciliation | `GET /api/meds`, `PUT /api/meds/override`, `POST /api/meds/add` |
| `analyst.py` | Deep analysis (trend, med review, pre-appointment) | `POST /api/analyst/analyze` |
| `narrator.py` | Local LLM summaries via Ollama | `POST /api/narrator/narrate` |
| `patient_profile.py` | Unified patient view, vitals, conditions | `GET /api/profile` |
| `reminders.py` | Follow-up reminders with action buttons | `GET/POST/PUT/DELETE /api/reminders` |
| `epic_oauth.py` | Epic on FHIR PKCE OAuth flow | `GET /api/epic/auth-url`, callback |

### Frontend (`web/`)

Single-page app in `web/index.html` — vanilla JS, no build step, Chart.js for graphs.

**Tabs:** Home (reminders + appointments), Profile, Timeline, Assistant, Medications

The frontend is a single large HTML file with embedded CSS and JS. All state is in-memory JS variables (e.g., `medsData`, `medsInitialized`). Cache invalidation: set `medsInitialized = false` to force reload on next tab switch.

### Data Pipeline (`ingest/`)

Institution-specific converters (C-CDA XML, CSV, scraped HTML → FHIR R4), Apple Health parser, and `ingest_to_hapi.py` batch loader. MyChart scrapers in JS.

## Critical Design Patterns

### Medication Normalization

`meds.py:normalize_med_name()` strips dosage, salt forms, and pharmacy jargon to group the same drug across records. Example: "doxycycline monohydrate 50 mg tablet" → "doxycycline".

**Important:** The `med_key` used in overrides MUST be the normalized key. The assistant sees medications with `[key: ...]` annotations so it uses the correct key. Both `execute_proposed_action()` and `PUT /api/meds/override` normalize incoming keys as a safety net.

Aliases (`med_aliases` table) handle merges: `resolve_key(normalize_med_name(raw), aliases)`.

### Assistant Action System

The assistant proposes updates via `user_propose_update` tool → frontend shows a confirmation card → user clicks Approve → `POST /api/assistant/execute-action` executes it.

**Action types:** `add_observation`, `update_medication_status`, `add_medication`, `add_note`, `create_reminder`

**Two-step medication changes:** When a patient pauses/stops a med, the assistant must first propose `update_medication_status`, then `create_reminder` for follow-up. Never create only a reminder without changing the status.

### Reminder System

Reminders have `status` (active/snoozed/completed/dismissed), `due_at`, optional `linked_med`/`linked_condition`, and `actions` (JSON array of `{label, action_type, params}` for resolution buttons).

The Home tab computes due status from date comparison (overdue/today/tomorrow/future). Future reminders are visually dimmed (opacity 0.55). Resolution buttons call `executeReminderAction()` which executes the action and marks the reminder completed.

The reschedule button opens a calendar date picker modal (no browser prompt).

### SSE Streaming

`POST /api/assistant/chat` streams responses as SSE. nginx config for `/api/assistant/` has `proxy_buffering off` and 300s timeout. Frontend uses `fetch()` + `getReader()` with a 2-minute timeout via `Promise.race()`. Always check `resp.ok` before reading the stream body.

### Document Index (Clinical Notes)

`buildDocumentIndex()` pre-fetches all DocumentReference and Binary resource IDs at page load. `getDocsForEncounter()` uses a triple-match strategy: encounter reference → CSN identifier → date fallback. Notes render inline in expandable appointment details.

Stanford DocumentReferences exist but their Binary content was never ingested (scraping limitation).

## Environment Variables

```
ANTHROPIC_API_KEY     — Required for assistant/analyst
ANTHROPIC_MODEL       — Default: claude-sonnet-4-20250514
ASSISTANT_DB          — SQLite path (default: /data/chat.db)
HAPI_BASE             — FHIR server URL (default: http://hapi:8080/fhir)
OLLAMA_BASE           — Local LLM (default: http://host.docker.internal:11434)
OLLAMA_MODEL          — Default: mistral
EPIC_CLIENT_ID        — Epic OAuth (optional)
EPIC_CLIENT_SECRET    — Epic OAuth (optional)
EPIC_REDIRECT_URI     — Default: https://bina.saridium.com/callback
```

## Common Operations

```bash
# Start everything
docker compose up -d

# Rebuild API after code changes
docker compose up -d --build api && docker compose restart web

# Restart web only (nginx picks up HTML/JS changes)
docker compose restart web

# Check API logs
docker compose logs -f api

# Query the SQLite database
docker compose exec api python3 -c "import sqlite3; ..."

# Ingest new data
cd ingest && python3 ingest_to_hapi.py --file bundle.json
```

## Deployment

Runs on a Mac Mini (M2 Pro, 32 GB) behind Cloudflare Tunnel.
- Setup script: `scripts/setup-tunnel.sh`
- Auth: Cloudflare Access (see `scripts/setup-access.md`)
- All Docker ports bound to 127.0.0.1
- Tunnel connects to nginx port 3080 (HTTP), Cloudflare handles TLS

## Shared Libraries (`lib/`)

- `loinc_synonyms.py` — Search synonym expansion (CRP↔c-reactive protein, A1C↔HbA1c, etc.)
- `loinc_mapper.py` — 12,000+ LOINC code reference database
- `loinc_validator.py` — LOINC code format and display name validation
- `fhir_utils.py` — Resource ID generation, narrative building, HTML sanitization
