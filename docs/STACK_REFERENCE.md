# Personal Health Vault — Stack Reference

## Docker Compose Services

| Service (compose) | Container Name   | Image/Build            | Ports         | Internal Hostname |
|--------------------|-----------------|------------------------|---------------|-------------------|
| `postgres`         | phv-postgres    | postgres:16-alpine     | 5432:5432     | postgres:5432     |
| `opensearch`       | phv-opensearch  | opensearch:2.12.0      | 9200:9200     | opensearch:9200   |
| `hapi`             | phv-hapi-fhir   | custom (Dockerfile)    | 8080:8080     | hapi:8080         |
| `api`              | phv-api         | custom (FastAPI)       | (internal)    | api:8000          |
| `web`              | phv-web         | nginx:alpine           | 3000:80       | web:80            |

## Common Commands

```bash
# Start / stop
docker compose up -d
docker compose down

# Restart a single service
docker compose restart hapi          # NOT hapi-fhir or phv-hapi-fhir

# Logs
docker compose logs -f hapi
docker compose logs -f api

# Shell into containers
docker compose exec postgres psql -U hapi -d hapi
docker compose exec api python3 -c "..."
docker compose exec opensearch curl -s http://localhost:9200/...

# Status
docker compose ps
```

**Important:** `docker compose` commands use the **service name** (left column: `postgres`, `opensearch`, `hapi`, `api`, `web`), NOT the container name (`phv-postgres`, `phv-hapi-fhir`, etc.).

## Key Configuration Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Service definitions, ports, volumes, health checks |
| `hapi-config/application.yaml` | HAPI FHIR server config (DB, OpenSearch, search settings) |
| `api/app.py` | FastAPI API layer (search, timeline metrics, KNOWN_METRICS) |
| `web/index.html` | Single-page web UI (timeline, search, events) |
| `nginx.conf` | Reverse proxy config (routes /api → FastAPI, /fhir → HAPI) |

## HAPI FHIR Configuration Notes

- **FHIR version:** R4
- **Database:** PostgreSQL (`jdbc:postgresql://postgres:5432/hapi`)
- **OpenSearch:** Connected via Hibernate Search (`opensearch:9200`)
- **`advanced_lucene_indexing`:** Currently `false` (was `true` but caused search results to be incomplete — OpenSearch index was missing ~19K observations that existed in PostgreSQL)
- **`store_resource_in_lucene_index_enabled`:** `true`
- **`search_prefetch_thresholds`:** `13,503,2003,-1`
- **Full-text search (`_content`):** Requires OpenSearch + Hibernate Search config

## Data Quality Scripts (run from host, not inside containers)

All scripts connect to `http://localhost:8080/fhir` (HAPI via Docker port mapping).

```bash
python3 improve_specimen_mapping.py --dry-run    # Specimen/LOINC mismatch correction
python3 backfill_reference_ranges.py --dry-run   # Add missing reference ranges
python3 backfill_interpretations.py --dry-run    # Add H/L/N interpretations
python3 dedup_cross_source.py --dry-run --stats  # Find & remove duplicate observations
```

## Volumes

| Volume | Mount Point | Purpose |
|--------|-------------|---------|
| `phv-pgdata` | /var/lib/postgresql/data | PostgreSQL data |
| `phv-osdata` | /usr/share/opensearch/data | OpenSearch indices |
