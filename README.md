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
  database.py   SQLite engine (WAL), additive column migration, DATABASE_URL override
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
- **No queue/broker (Celery, etc.)**: a single sequential
  `BackgroundTasks` call is enough for a 10-molecule demo job. Using a
  real broker here would be over-engineering, not a stronger signal —
  the production direction is documented, not implemented.
- **Rule-based status buckets** (`Nominated` / `Shortlisted` /
  `Review`) instead of one combined score: scientific metrics are kept
  as separate columns, per the explicit design decision not to invent
  a scientific weighting formula with no basis.

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --port 8008
```

Open <http://127.0.0.1:8008> — the API serves the UI. API docs are at `/docs`.

```bash
.venv/bin/python -m pytest tests/ -q     # 52 tests, uses a temporary database
```

`MDP_STAGE_DELAY_SECONDS` (default `0.8`) sets the deliberate pause in each
*mocked* stage so progress is visible; set `0` for instant runs.

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
- Runs execute in the API process; a restart mid-run leaves a job in a
  non-terminal state. A durable queue + CPU/GPU workers is the production
  direction, deliberately not built here.
- mmCIF support covers the `_atom_site` loop and a few header items only.
- No pocket detection or structure preparation; "validation" is structural.
- Mocked stages are deterministic placeholders. A real integration replaces one
  adapter class in `backend/services/engines.py`.
