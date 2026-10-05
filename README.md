# Molecular Discovery Platform — Integration MVP

Take-home scaffold for the Molecular Solutions AI/ML Developer Intern
assignment. This is an **integration layer** over a mocked drug-discovery
pipeline, not a new scientific model — see the design discussion this
was built from for the full architecture rationale.

## What's real vs. mocked (important — read this first)

| Stage | Status | Detail |
|---|---|---|
| 01 Target intake | **real** | upload (`.pdb/.cif`), PDB ID (RCSB fetch), or bundled demo; parsed, validated and persisted |
| 01b Structure validation | **real** | readable file, atom records, finite coordinates, chains, residues — structural checks only, not a druggability assessment |
| 02 Chemical-space screening | **mocked** | fixed 40-compound demo library stands in for ultra-large virtual screening |
| 03 SA scoring | **real** | RDKit's bundled Ertl & Schuffenhauer (2009) SAscore implementation, unmodified |
| 03b Molecular weight / 2D depiction | **real** | RDKit `Descriptors.MolWt` and `rdMolDraw2D` |
| 04 ADMET | **mocked** | deterministic "Favorable/Moderate" + a few illustrative properties |
| 05 Binding affinity | **mocked** | deterministic placeholder Kd / ΔG |
| 06 Unbinding kinetics | **mocked** | deterministic placeholder koff / residence time |
| SDF export | **real I/O** | genuine RDKit molblock + property tags; mocked values are tagged `_mocked` |

Every mocked `StageResult` row is flagged `is_mocked=True` in the
database and surfaced as "(mocked)" in the UI — this was a deliberate
requirement, not an oversight, since a take-home should never present
simulated output as if it were a real prediction.

"10T+ chemical space" in the UI is kept as a **conceptual scale label**
distinct from this run's actual (small) screened count — see
`schemas.StageCard.scale_note` — because actually screening a
multi-trillion-compound library is obviously out of scope for a demo.

## Architecture

```
backend/
  config.py     all environment configuration, in one place
  database.py   engine/session (PostgreSQL; SQLite for dev/tests only), DB readiness wait
  migrations.py programmatic Alembic access;  alembic/ holds the migrations
  storage.py    artifact paths: stored relative to DATA_DIR, jail-checked
  entrypoint.py container start: wait for DB -> migrate -> serve
  logging_config.py  stdout logging (JSON outside development) with job/stage context
  models.py     Target, Job, JobStageLog, Molecule, StageResult, Artifact
  schemas.py    Pydantic request/response models
  pipeline.py   Sequential orchestrator + state machine; commits at each stage
  targets/
    structure.py  PDB/mmCIF parser + input validation (pure functions)
    service.py    intake routes, persistence, artifact staging
  services/     one adapter class per stage; declares is_mocked honestly
  stages/
    sa_scoring.py   real RDKit SA scoring
    depiction.py    real RDKit 2D SVG + SDF export
    mocks.py        deterministic placeholder science, seeded by target
    library.py      40-compound demo library
  main.py       FastAPI app; also serves the frontend at /app
frontend/
  discoveries.html · new-discovery.html · target-validation.html ·
  configure-review.html   the New Discovery flow (Stitch design system)
  dashboard.html          Discovery Run / results, bound to ?job=<id>
  assets/                 shared tokens, runtime, styles
data/
  fixtures/     bundled demo structure (7RPZ) — see its README
  runs/{job_id}/  per-run artifacts (generated, git-ignored)
```

User flow: **Discoveries → New Discovery → Target Validation → Configure &
Review → Start Discovery → Discovery Run.** See `docs/UI_UX_DEMO_GUIDE.md`
for the screen-by-screen walkthrough and demo script, and `docs/DEMO_DATA.md`
for the fixture, library and real-vs-mocked detail.

### Why these MVP choices (see also the earlier design discussion)

- **Generic `StageResult.payload` JSON column** instead of one typed
  table per stage: several stages are mocked and the real partner
  schemas are unknown, so a JSON payload avoids repeated migrations
  while that's still true. Production direction: split into typed
  tables once a real partner's output schema is locked in.
- **`JobStageLog`** is an append-only transition log — the basis for
  progress tracking, retries, and failure diagnosis, implemented for
  real because it costs nothing and is what interviewers are most
  likely to probe ("what happens if a stage fails?").
- **Pipeline runs inside the API process** (a background task). That is simple and
  enough here, but a restart abandons any run in flight — it is marked `FAILED`
  on the next start — and exactly one app process is supported. Production
  direction: API → durable job queue → CPU workers → GPU workers
  (see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)).
- **No queue/broker (Celery, etc.)**: a single sequential
  `BackgroundTasks` call is enough for a 10-molecule demo job. Using a
  real broker here would be over-engineering, not a stronger signal —
  the production direction is documented, not implemented.
- **Rule-based status buckets** (`Nominated` / `Shortlisted` /
  `Review`) instead of one combined score: scientific metrics are kept
  as separate columns, per the explicit design decision not to invent
  a scientific weighting formula with no basis.

## Running it

### Docker deployment (PostgreSQL) — the supported way to run it

Requires Docker with the Compose plugin. Full detail: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

```bash
cp .env.example .env                      # then set POSTGRES_PASSWORD (openssl rand -hex 24)
docker compose build
docker compose up -d                      # starts postgres, waits for it, migrates, starts the app
docker compose ps                         # both services should report "healthy"
python scripts/smoke_test.py              # optional: end-to-end API check of the deployment
```

Open <http://127.0.0.1:8008>. The app container runs `alembic upgrade head`
itself before serving, so no manual migration step is needed (to run it by hand:
`docker compose run --rm app alembic upgrade head`).

```bash
docker compose logs -f                    # follow logs (JSON, written to stdout)
docker compose down                       # stop; the database and run artifacts are kept
docker compose down -v                    # DESTRUCTIVE: also deletes the database and all artifacts
```

| Where | What lives there |
|---|---|
| Docker volume `molecular-discovery_pgdata` | the PostgreSQL database |
| Docker volume `molecular-discovery_mdp_data` → `/var/lib/mdp` | stored uploads (`targets/`) and per-run artifacts: PDB, stage outputs, SDF (`runs/<job_id>/…`) |
| inside the image | application code, UI, and the bundled demo fixture (read-only) |

Configuration is by environment variables, all listed in `.env.example`
(`APP_ENV`, `LOG_LEVEL`, `CORS_ORIGINS`, `PORT`, `MAX_UPLOAD_MB`, …). PostgreSQL is
not published to the host; only the app port is.

**Reset the demo to a clean state** (deletes all runs and uploads):

```bash
docker compose down -v && docker compose up -d
```

### Local development (no Docker)

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn backend.main:app --port 8008
```

With `APP_ENV` unset (= `development`) and no `DATABASE_URL`, this uses a local
SQLite file (`data/app.db`) — a zero-setup convenience that is refused in `demo`
and `production`. To develop against PostgreSQL instead, set `DATABASE_URL` and run
`.venv/bin/alembic upgrade head` once. Open <http://127.0.0.1:8008>; API docs are at `/docs`.

```bash
.venv/bin/python -m pytest tests/ -q                      # SQLite, temporary database
TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/mdp_test \
    .venv/bin/python -m pytest tests/ -q                  # PostgreSQL (use a disposable DB)
```

`STAGE_DELAY_SECONDS` (default `0.8`) sets the deliberate pause in each *mocked*
stage so progress is visible; set `0` for instant runs.

### Endpoints

- `POST /api/targets/upload` · `POST /api/targets/pdb-id` · `POST /api/targets/demo`
- `GET /api/targets/{id}` · `GET /api/targets/{id}/structure`
- `GET /api/engines` — which stage engines are real vs demo adapters
- `POST /api/jobs` — create + start a run (`target_id` + config; legacy
  `target_name`/`pdb_id` body still accepted). Returns immediately.
- `GET /api/jobs` · `GET /api/jobs/{id}` · `GET /api/jobs/{id}/candidates`
- `GET /api/jobs/{id}/candidates.sdf` — all final candidates, one SDF
- `GET /api/candidates/{id}` · `GET /api/candidates/{id}/sdf`

## Known gaps / what I'd do next with more time

- No retry/backoff on stage failure (the log records failure; nothing
  auto-retries yet).
- No auth — fine for a take-home, not for production.
- Runs execute in the API process: a restart abandons the run in flight (it is
  marked failed on next start) and only one app process is supported. A durable
  queue + CPU/GPU workers is the production direction, deliberately not built here.
- Artifacts are on a local Docker volume, not object storage; no authentication;
  no TLS termination (put a reverse proxy in front for anything public).
- mmCIF support covers the `_atom_site` loop and a few header items only.
- No pocket detection or structure preparation; "validation" is structural.
- Mocked stages are deterministic placeholders. A real integration replaces one
  adapter class in `backend/services/engines.py`.
