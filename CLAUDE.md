# Bina Health — Personal Health Vault

## Working style (Claude, read this)

- Don't editorialize about my time, energy, or stamina. No "long session," "go rest," "good place to pause for the day," "you've earned a break," or any variant. I manage my own pace.
- Don't append unsolicited mood-management lines to summaries. End on the substance.
- "Next reasonable moves" framings are welcome and useful; emotional-state framings are not.

## What This Is

A self-hosted personal health record system that aggregates clinical data from multiple institutions (Stanford, UCSF, Mayo, MSKCC, Sutter), Apple Health wearables, and Epic on FHIR into a unified FHIR R4 database. Includes an AI assistant (Claude), medication reconciliation, trend analysis, and a reminder system.

Personal project, single-user. Runs on a Mac Mini behind a Cloudflare Tunnel.

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

### Pill Identification (NLM Pillbox)

The Meds tab has an "Identify pill" button that opens a modal with photos and structured metadata (imprint code, color, shape, manufacturers) for each prescription pill.

**Data source:** locally-hosted NLM Pillbox archive (frozen August 2020, ~9,800 photographed US prescription pills). Files live under `data/pillbox/` (gitignored, ~3 GB extracted + ~2 GB of background-removed PNGs). Without this data, the endpoint gracefully returns `{"error": "Pillbox database not available..."}` and the rest of the app works fine.

**Setup on a new machine**: run `./scripts/setup-pillbox.sh`. By default it tries the **fast path**: shallow-clone the companion data repo at [`usarid/nlm-pillbox-images`](https://github.com/usarid/nlm-pillbox-images) (which ships the original JPEGs, the pre-processed background-removed PNGs, and the metadata CSV — ~3 GB, a few minutes over a residential connection). If that clone fails, or you set `PILLBOX_NO_CLONE=1`, it falls back to the **slow path**: download the 1 GB image archive directly from NLM and run rembg locally (~30 min on M2 Pro). Either way, the SQLite DB is generated locally from the metadata CSV at the end. After it completes, `docker compose up -d --force-recreate api web` to mount the new data.

**Background-removal pipeline:** `scripts/remove_pillbox_backgrounds.py` walks `data/pillbox/images/*.jpg`, passes each through rembg's `u2netp` model, and writes the cutout as a PNG with alpha to `data/pillbox/images_nobg/`. Idempotent + resumable (skips files whose output already exists), so re-running is cheap. Runs in a venv at `data/pillbox/.venv/` to keep rembg/onnxruntime/Pillow off the system Python. Quality is good for pills on uniform photo backgrounds; if a future case fails, switch to `--model isnet-general-use` (slower, higher quality).

**How the lookup works:** `GET /api/meds/pill-image?name={drug}` (in `api/meds.py`) cleans the input name (strips dose/form/salt suffixes), searches the SQLite DB by ingredient or brand name, and groups results by physical appearance (color + shape + imprint) so multiple manufacturers' versions of the same physical pill collapse into one card. Image URLs point at the no-background PNGs: `/pillbox-images/{filename}.png`.

**Docker wiring:**
- `api` mounts `./data/pillbox` at `/pillbox` (read-only) so it can open `pillbox.db`
- `web` mounts `./data/pillbox/images_nobg` at `/usr/share/pillbox-images` (read-only) — note: the no-bg dir, NOT the original `images/` (originals are kept on disk for reprocessing but are not served)
- nginx serves `/pillbox-images/*.png` with long-cache static (config in `nginx.conf`)

**Pill picker (per-med chosen photo):** the Meds tab "Choose picture…" button lets the user pick which physical pill they actually take from the gallery of matches. The choice is persisted via `PUT /api/meds/pill-choice` into four extra columns on `med_overrides` (`pill_imprint`, `pill_color`, `pill_shape`, `pill_image_url`), rides along in `GET /api/meds`, and renders as an inline 60px thumbnail on the med card. Re-opening the picker highlights the current selection with a green check + border and shows a "Clear selection" link.

**Frontend gating:** the "Choose picture…" button is hidden for non-pill forms — see the `isPillForm` regex in `medsRenderCard()` in `web/index.html`. Whitelist: tablet, capsule, caplet, chewable, softgel, sublingual, ODT, lozenge, troche.

## Environment Variables

```
ANTHROPIC_API_KEY     — Required for assistant/analyst
ANTHROPIC_MODEL       — Default: claude-sonnet-4-6 (chat path; latency-sensitive). Bare alias — do not pin to dated snapshots; they retire on a calendar and return 404.
ANTHROPIC_MODEL_ANALYST — Default: claude-opus-4-8 (analyst path; reasoning-heavy, latency-tolerant). Falls back to ANTHROPIC_MODEL if unset.
ASSISTANT_DB          — SQLite path (default: /data/chat.db)
HAPI_BASE             — FHIR server URL (default: http://hapi-v2:8080/fhir)
OLLAMA_BASE           — Local LLM (default: http://host.docker.internal:11434)
OLLAMA_MODEL          — Default: mistral
EPIC_CLIENT_ID        — Epic OAuth (optional)
EPIC_CLIENT_SECRET    — Epic OAuth (optional)
EPIC_REDIRECT_URI     — Default: https://binahealth.com/callback
```

## Common Operations

```bash
# Start everything
docker compose up -d

# Rebuild API after code changes
docker compose up -d --build api && docker compose restart web

# CRITICAL — after editing docker-compose.yml (volumes, ports, env vars),
# a plain `up -d` will NOT recreate containers if the image hasn't changed.
# Force recreation so the new config takes effect:
docker compose up -d --force-recreate api web

# Restart web only (nginx picks up HTML/JS changes)
docker compose restart web

# One-time: set up the Pillbox archive for the Meds pill-identification feature
./scripts/setup-pillbox.sh

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
