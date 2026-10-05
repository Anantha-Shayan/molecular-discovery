# Deployment

Practical guide to running the Molecular Discovery Platform as a containerised,
PostgreSQL-backed stack. This is a take-home MVP deployment: clean,
reproducible and honest about its limits — not a hardened production platform
(see §13).

## 1. Architecture

```
            Internet / browser
                   │  :8008 (only published port)
                   ▼
   ┌───────────────────────────────┐
   │ app container                 │      read-only root filesystem
   │  FastAPI + UI + pipeline      │      non-root (uid 10001)
   │  python -m backend.entrypoint │
   └───────┬───────────────┬───────┘
           │ DATABASE_URL  │ /var/lib/mdp
           │ (service name │ (volume: uploads, run artifacts)
           │  "postgres")  ▼
           ▼          mdp_data volume
   ┌───────────────────────────────┐
   │ postgres container            │      not published to the host
   │  PostgreSQL 16                │
   └───────────────┬───────────────┘
                   ▼
              pgdata volume
```

Startup sequence inside the app container (`backend/entrypoint.py`):

1. **Wait for PostgreSQL** — retries with capped backoff and a connect timeout
   (default budget 60 s). No fixed `sleep`; it works whether or not Compose
   ordered the containers.
2. **Migrate** — `alembic upgrade head`. Idempotent (applies only what is not yet
   applied) and non-destructive; guarded by a Postgres advisory lock.
3. **Serve** — single-process uvicorn, no reload.

Compose additionally gates the app on a Postgres `pg_isready` healthcheck.

The UI is static files served by the same app, so there is one process and one port.
Scientific stages are deterministic demo adapters in every environment; only SA
scoring is real (see `docs/DEMO_DATA.md`).

## 2. Prerequisites

- Docker with the Compose plugin (Compose v2+).
- ~1.5 GB free disk for the image (≈670 MB, dominated by RDKit and NumPy) and volumes.
- Outbound network is needed only to build the image and for "Use PDB ID" with IDs other
  than the bundled `7RPZ`. The demo itself runs fully offline.

## 3. Environment variables

Set in `.env` (copy `.env.example`; `.env` is git-ignored). Compose reads it.

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_PASSWORD` | *(required)* | Database password. Compose refuses to start without it. Use URL-safe characters (`openssl rand -hex 24`). |
| `POSTGRES_DB` / `POSTGRES_USER` | `molecular_discovery` / `mdp` | Database name / user. |
| `APP_ENV` | `demo` (compose) | `development`, `demo` or `production` — see below. |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL`. |
| `PORT` | `8008` | Port the app listens on and publishes. |
| `CORS_ORIGINS` | *(empty)* | Comma-separated origins allowed cross-origin. Empty = no CORS headers (the UI is same-origin). `*` is rejected in `production`. |
| `MAX_UPLOAD_MB` | `128` | Structure-file upload limit (enforced from `Content-Length` and while streaming). |
| `STAGE_DELAY_SECONDS` | `0.8` | Pause inside each *mocked* stage so progress is visible. `0` for instant runs. |
| `ENABLE_API_DOCS` | on, except `production` | Serve `/docs` and `/openapi.json`. |
| `RUN_MIGRATIONS` | `true` | Apply migrations at container start. |
| `DB_WAIT_TIMEOUT_SECONDS` | `60` | How long startup waits for PostgreSQL. |
| `RCSB_TIMEOUT_SECONDS` | `15` | Timeout when fetching a structure by PDB ID. |
| `DATABASE_URL` | built by Compose | Set automatically to `postgresql://…@postgres:5432/…`. Set it yourself only outside Compose. `postgres://` and `postgresql://` are both accepted. |
| `DATA_DIR` | `/var/lib/mdp` (image) | Where uploads and run artifacts are written. |

All variables are read in one place, `backend/config.py`, which fails fast with a
clear message on invalid values.

**Environments** — `APP_ENV` changes operational behaviour only, never the science:

| | `development` | `demo` | `production` |
|---|---|---|---|
| Database | PostgreSQL, or SQLite fallback | PostgreSQL required | PostgreSQL required |
| Error detail in API responses | shown | hidden | hidden |
| `/docs` | on | on | off (unless `ENABLE_API_DOCS=true`) |
| `CORS_ORIGINS=*` | allowed | allowed | rejected |

## 4. PostgreSQL

- Image `postgres:16-alpine`, data in the `pgdata` named volume.
- **Not published.** The `postgres` service has no `ports:`; only the app, on the Compose
  network, can reach it (as host `postgres`). To inspect it:
  `docker compose exec postgres psql -U mdp -d molecular_discovery`.
- The app uses SQLAlchemy with the psycopg 3 driver, a small connection pool and
  `pool_pre_ping`, so connections dropped by a database restart are replaced transparently.
- SQLite exists only for local development and the default test run; `demo` and
  `production` refuse it.

## 5. Docker build

```bash
docker compose build
```

Two-stage `Dockerfile`: dependencies are installed into a venv in a builder stage
(`pip install -c constraints.txt -r requirements.txt`), then copied into a slim runtime
image with a non-root user. The image contains code, migrations, the UI and the demo
fixture — not tests, docs, `.env`, databases or run data (`.dockerignore`).

A build-time import check fails the **build** if RDKit's native libraries are missing,
rather than letting the container crash-loop at runtime.

**Reproducibility:** `requirements.txt` pins direct dependencies; `constraints.txt` pins
the full resolved set. After changing `requirements.txt`, regenerate it from a clean
interpreter:

```bash
docker run --rm -i python:3.14-slim sh -c \
  "cat > /tmp/r.txt && python -m venv /tmp/v && /tmp/v/bin/pip install -q -r /tmp/r.txt && /tmp/v/bin/pip freeze --exclude pip" \
  < requirements.txt
```

## 6. Docker Compose startup

```bash
cp .env.example .env        # set POSTGRES_PASSWORD
docker compose up -d
docker compose ps           # postgres and app should both become "healthy"
```

Open <http://127.0.0.1:8008>. Optional end-to-end API check (stdlib only, runs a real job
against PostgreSQL and parses the SDF):

```bash
python scripts/smoke_test.py            # or: python scripts/smoke_test.py http://host:8008
```

## 7. Database migration

Normally automatic (§1). Manual operation:

```bash
docker compose run --rm app alembic current          # show current revision
docker compose run --rm app alembic upgrade head     # apply pending migrations
```

Fresh database: just start the stack — an empty database is migrated from nothing to
`head`. `Base.metadata.create_all()` is **never** used on PostgreSQL.

Changing the schema (development): edit `backend/models.py`, then

```bash
DATABASE_URL=postgresql://… .venv/bin/alembic revision --autogenerate -m "describe change"
# review the generated file, then:
.venv/bin/alembic upgrade head
.venv/bin/alembic check        # must report "No new upgrade operations detected"
```

Review autogenerated migrations by hand. Note in `0001_initial_schema.py`: `targets` →
`artifacts` → `jobs` → `targets` is a foreign-key cycle, so that one constraint is
created in a separate step; autogenerate would otherwise emit it in a form PostgreSQL
rejects or silently skips.

`/ready` returns 503 if the schema is behind the migrations this build ships.

## 8. Persistent storage

| Data | Location | Survives |
|---|---|---|
| Database | volume `pgdata` | `docker compose down`, image rebuilds |
| Uploads: `targets/<id>/structure.pdb` | volume `mdp_data` at `/var/lib/mdp` | same |
| Run artifacts: `runs/<job_id>/{target,screening,sa,admet,affinity,kinetics,final}/…` | volume `mdp_data` | same |
| Demo fixture | inside the image, `/app/data/fixtures` | rebuilt with the image |

Artifact paths in the database are stored **relative to `DATA_DIR`**, so the volume can
be moved or `DATA_DIR` changed without orphaning anything. The fixture lives outside
`DATA_DIR` so a volume can never shadow it.

Prefer a host directory over a named volume? In `docker-compose.yml` replace
`mdp_data:/var/lib/mdp` with `./data/runtime:/var/lib/mdp` and make it writable by the
container user: `mkdir -p data/runtime && sudo chown 10001:10001 data/runtime`
(on SELinux hosts also add `:z` to the mount).

## 9. Health checks

| Endpoint | Meaning | Touches |
|---|---|---|
| `GET /health` | process is alive and serving → 200 | nothing external |
| `GET /ready` | ready for traffic → 200, else 503 | PostgreSQL, schema revision, `DATA_DIR` writable, demo fixture present |

`/ready` reports per-check status and never includes connection strings or error text.
The image's `HEALTHCHECK` uses `/health`; Compose's healthcheck uses `/ready`, so
`docker compose ps` shows "healthy" only when the app can actually serve.

## 10. Logs

All logs go to stdout/stderr — no log files.

```bash
docker compose logs -f              # both services
docker compose logs -f app          # app only
```

Outside `development`, app logs are one JSON object per line, with `job_id`, `stage`,
`molecule_id` and `request_id` where applicable:

```json
{"ts":"…","level":"ERROR","logger":"mdp.pipeline","msg":"sa FAILED: …","job_id":"…","stage":"sa"}
```

Database URLs are logged only with the password redacted. Unhandled errors return a
`request_id` to the client; the traceback is in the log under that ID. Docker log
rotation is configured (10 MB × 3 files per service).

## 11. Updating / redeploying

```bash
git pull
docker compose build
docker compose up -d        # recreates only what changed; migrations run on start
```

Data volumes are untouched. Because pipelines run inside the app process, **redeploying
while a run is in progress interrupts it**: on the next start that run is marked
`FAILED` ("Interrupted: the application restarted…"), and it can be re-run from the UI.

## 12. Resetting demo data

```bash
docker compose down            # stop, keep everything
docker compose down -v         # DESTRUCTIVE: deletes the database AND all uploads/run artifacts
docker compose up -d           # fresh, migrated, empty
```

`down -v` is irreversible and removes both volumes. There is no automatic destructive
initialisation anywhere: the app and migrations only ever add.

To remove just the demo runs while keeping the schema, delete rows (or `TRUNCATE jobs,
molecules, stage_results, job_stage_logs, artifacts, targets CASCADE`) and
`docker compose exec app sh -c 'rm -rf /var/lib/mdp/runs/* /var/lib/mdp/targets/*'`.

## 13. Current MVP limitations

Stated plainly — this is a take-home deployment, not production-ready:

- **Pipelines run inside the API process.** A restart abandons the run in flight
  (recorded as failed on next start). Exactly one app process is supported; do not run
  multiple replicas or uvicorn workers — restart recovery would mark a sibling's healthy
  run as failed.
- **Scientific engines are mocked** (screening, ADMET, affinity, kinetics). Only SA
  scoring, structure handling, persistence and orchestration are real.
- **Artifacts are on a local Docker volume**, not object storage.
- **No authentication or authorisation**, no per-user data separation.
- **No TLS.** Put a reverse proxy (Caddy, nginx, a cloud load balancer) in front for
  anything exposed beyond a trusted network; also enforce a request-size limit there,
  since chunked uploads without a `Content-Length` are only bounded by the app's
  streaming check after the body is received.
- **No rate limiting**, no retry/backoff for stage failures, no job cancellation.
- **No GPU/CPU worker pools.** CPU/GPU separation is represented as labels, not infrastructure.
- Single PostgreSQL instance, no backups configured (`pg_dump` the `pgdata` volume yourself).
- Frontend loads Tailwind and fonts from public CDNs and 3Dmol.js from cdnjs, so the UI
  needs internet in the *browser* (the server does not). No CSP header is set because the
  CDN Tailwind build requires inline scripts.

## 14. Production evolution

Direction, deliberately **not** built here:

```
   API (stateless, N replicas)
        │ enqueue job
        ▼
   durable job queue          ← e.g. Postgres-backed queue, Celery/Redis, or Temporal
        │
        ├──► CPU workers  — screening adapter, SA, ADMET, binding affinity (Docker)
        └──► GPU workers  — unbinding kinetics (Docker + GPU)
        │
        ▼
   PostgreSQL (managed, backed up)   +   object storage for PDB/SDF artifacts
```

What that changes: jobs survive API restarts; workers scale independently and are
retried with backoff (the `JobStageLog.attempt` column already exists for this); mocked
adapters in `backend/services/engines.py` are replaced one class at a time by partner
integrations without touching the orchestrator, schema or UI; artifacts move to object
storage behind `backend/storage.py`; authentication, TLS, rate limiting and observability
(metrics, tracing) are added at the edge. None of that is needed to demonstrate the
integration architecture, which is why it is documented rather than implemented.
