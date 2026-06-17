# Personal Health Vault

A self-hosted personal health record system that aggregates clinical data from multiple healthcare institutions into a unified, patient-controlled FHIR R4 database — with AI-powered analysis, full-text search, and direct EHR connectivity via Epic on FHIR.

## Why

Patient health data is scattered across hospitals, labs, pharmacies, and wearable devices. Each institution shows you a partial view. This project puts all of it in one place under your control, stored in a standards-compliant FHIR R4 server you run locally.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│   Browser    │────▶│  nginx (TLS) │────▶│  FastAPI (API) │
│  localhost   │     │   :3000/443  │     │     :8000      │
└─────────────┘     └──────────────┘     └────────┬───────┘
                                                   │
                         ┌─────────────────────────┼─────────────────┐
                         │                         │                 │
                    ┌────▼─────┐          ┌───────▼──────┐   ┌─────▼──────┐
                    │ HAPI FHIR│          │  PostgreSQL   │   │ OpenSearch  │
                    │   R4     │─────────▶│    :5432      │   │   :9200    │
                    │  :8080   │          └──────────────┘   └────────────┘
                    └──────────┘
```

All services run in Docker. Data persists in Docker volumes between restarts.

## Features

**Data Aggregation** — ingest pipelines for lab results, clinical notes, visit records, medications, and wearable data from multiple institutions (Stanford, UCSF, MSKCC, Mayo, Sutter, and others). Converters normalize heterogeneous source formats into FHIR R4 resources.

**Apple Health Integration** — import clinical records and wearable data (blood pressure, heart rate, SpO2, etc.) exported from Apple Health.

**Epic on FHIR (OAuth 2.0)** — SMART on FHIR patient-facing app with PKCE. Connect directly to Epic-based health systems to pull data incrementally.

**AI Health Assistant** — conversational interface powered by Claude that can answer questions about your health data, explain lab results, and identify trends. Includes a narrator (plain-language health summaries) and analyst (structured data analysis).

**Patient Profile** — unified view of vitals (averaged over clinically appropriate time windows), active/resolved conditions with inference engine, medications, and allergies. Supports user overrides for condition statuses.

**Full-Text Search** — OpenSearch-backed search with LOINC synonym expansion across all resource types.

**Timeline** — longitudinal view of lab results, vitals, and clinical events with auto-discovered metrics.

**Data Quality** — cross-source deduplication, provenance tracking, specimen mapping correction, and quality patch generation.

## Quick Start

### Prerequisites

- Docker and Docker Compose
- (Optional) An Anthropic API key for the AI assistant features
- (Optional) An Epic on FHIR developer account for EHR connectivity

### Setup

```bash
# Create persistent Docker volumes
docker volume create phv-pgdata
docker volume create phv-osdata
docker volume create phv-aidata

# Configure environment
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY, EPIC_CLIENT_ID, etc.

# Generate self-signed TLS cert for local HTTPS (required for Epic OAuth)
mkdir -p certs
openssl req -x509 -nodes -days 3650 \
  -newkey rsa:2048 \
  -keyout certs/localhost.key \
  -out certs/localhost.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# Start all services
docker compose up -d

# Wait for HAPI FHIR to initialize (~2 minutes on first run)
docker compose logs -f hapi
```

Then open [https://localhost:3000](https://localhost:3000) and accept the self-signed certificate.

## Project Structure

```
health-assistant/
├── api/                    # FastAPI backend
│   ├── app.py              # Main app with search, timeline, health endpoints
│   ├── assistant.py        # AI chat assistant (Claude)
│   ├── narrator.py         # Plain-language health summaries
│   ├── analyst.py          # Structured data analysis
│   ├── meds.py             # Medication management
│   ├── patient_profile.py  # Profile with vitals, conditions, inference
│   └── epic_oauth.py       # Epic on FHIR OAuth 2.0 + PKCE
├── web/                    # Frontend (single-page app)
│   ├── index.html          # Main UI
│   └── callback.html       # Epic OAuth redirect handler
├── hapi-config/            # HAPI FHIR server configuration
├── ingest/
│   ├── converters/         # Institution-specific data converters
│   ├── scrapers/           # MyChart and EHR data extraction scripts
│   ├── loaders/            # Bash scripts to load data into HAPI
│   └── apple/              # Apple Health data parsers
├── quality/                # Data quality and dedup pipelines
├── tools/                  # Utility scripts (search, analysis, setup)
├── lib/                    # Shared libraries (LOINC mapping, FHIR utils)
├── docs/                   # Architecture docs and guides
├── docker-compose.yml
└── nginx.conf              # Reverse proxy with TLS termination
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | API key for Claude AI features | — |
| `ANTHROPIC_MODEL` | Claude model — use the bare alias (`claude-sonnet-4-6`, `claude-opus-4-8`), not dated snapshots | `claude-sonnet-4-6` |
| `EPIC_CLIENT_ID` | Epic on FHIR app client ID | — |
| `EPIC_CLIENT_SECRET` | Epic sandbox client secret | — |
| `EPIC_REDIRECT_URI` | OAuth callback URL | `https://localhost:3000/callback` |
| `OLLAMA_BASE` | Local LLM endpoint (optional) | `http://host.docker.internal:11434` |
| `OLLAMA_MODEL` | Local model name | `mistral` |

## Data Sources

The ingest pipeline supports data from:

- **Epic MyChart** — direct FHIR API access via OAuth, plus DOM/API scrapers for data not exposed via standard FHIR endpoints
- **Apple Health** — clinical records (labs, vitals, conditions) and wearable data (blood pressure, heart rate, SpO2, activity)
- **Institution exports** — CSV/PDF lab results and clinical notes from Stanford, UCSF, MSKCC, Mayo Clinic, Sutter Health
- **FHIR bundles** — standard FHIR R4 Bundle resources from any source

## License

This is a personal project. Use at your own risk. Not intended for clinical decision-making.
