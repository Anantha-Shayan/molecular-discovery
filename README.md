# Molecular Discovery Platform — Integration MVP

Take-home scaffold for the Molecular Solutions AI/ML Developer Intern
assignment. This is an **integration layer** over a mocked drug-discovery
pipeline, not a new scientific model — see the design discussion this
was built from for the full architecture rationale.

## What's real vs. mocked (important — read this first)

| Stage | Status | Detail |
|---|---|---|
| 01 Target | input | user-supplied protein label / PDB ID, not validated against RCSB in this MVP |
| 02 Chemical-space screening | **mocked** | fixed 10-molecule seed library stands in for ultra-large virtual screening |
| 03 SA scoring | **real** | RDKit's bundled Ertl & Schuffenhauer (2009) SAscore implementation, unmodified |
| 03b Molecular weight / 2D depiction | **real** | RDKit `Descriptors.MolWt` and `rdMolDraw2D` |
| 04 ADMET | **mocked** | deterministic pseudo-random "Favorable/Moderate" + a few illustrative properties, not a real 220-descriptor model |
| 05 Binding affinity | **mocked** | deterministic pseudo-random Kd / ΔG |
| 06 Unbinding kinetics | **mocked** | deterministic pseudo-random koff / residence time, loosely coupled to the mocked affinity |
| SDF export | **real I/O** | genuine RDKit molblock + property tags; only the property *values* inside are mocked |

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
  database.py   SQLite engine/session (swap DATABASE_URL for Postgres in prod)
  models.py     Target, Job, JobStageLog, Molecule, StageResult, Artifact
  schemas.py    Pydantic response models, shaped to match the dashboard
  pipeline.py   Sequential stage orchestrator + state machine + stage log
  stages/
    sa_scoring.py   real RDKit SA scoring
    depiction.py    real RDKit 2D SVG + SDF export
    mocks.py        screening / ADMET / affinity / kinetics mocks
  main.py       FastAPI app (see endpoints below)
frontend/
  dashboard.html  the provided dashboard UI, wired to the API via a
                  <script> block at the end of the file (visual design
                  untouched — only IDs added to elements that needed
                  live data, plus a status banner and empty states)
```

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
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8008
```

Then open `frontend/dashboard.html` directly in a browser (it calls the
API at `http://127.0.0.1:8008` — change `API_BASE` at the top of the
`<script>` block if you serve it elsewhere). Click **Run Pipeline**.

API docs: `http://127.0.0.1:8008/docs` (FastAPI's auto-generated Swagger UI).

### Endpoints

- `POST /api/jobs` — create + start a pipeline run
- `GET /api/jobs/{job_id}` — funnel/stage status (for the dashboard's stepper)
- `GET /api/jobs/{job_id}/candidates` — ranked candidate table
- `GET /api/candidates/{molecule_id}` — single-candidate detail panel
- `GET /api/candidates/{molecule_id}/sdf` — real SDF file download

## Known gaps / what I'd do next with more time

- No retry/backoff on stage failure (the log records failure; nothing
  auto-retries yet).
- No auth — fine for a take-home, not for production.
- No file-based PDB upload wired up yet (target is currently just a
  name + PDB ID string).
- Mocked stages are static functions; a drop-in real integration would
  replace just the body of each function in `stages/mocks.py` — the
  rest of the system (schema, orchestrator, API, UI) shouldn't need to
  change.
