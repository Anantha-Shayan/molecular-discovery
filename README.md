# Molecular Discovery Platform

Upload a protein (or fetch it by PDB ID), mark the binding pocket, and run a **persistent, batch-based
screening campaign**: dock a compound library, re-score with HYDE, filter by synthetic accessibility and
predicted ADMET, and download ranked candidates as SDF. Built for the Molecular Solutions AI/ML intern
evaluation. Status and evidence: [`docs/ACCEPTANCE_REPORT.md`](docs/ACCEPTANCE_REPORT.md).
Deployment: company server [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) · VPS [`docs/VPS.md`](docs/VPS.md) · production `docker-compose.prod.yml` · local Docker: `docker-compose.dev.yml` (compose = shared base + one override per environment).

## What is real, what is not

| Stage | Status | Engine |
|---|---|---|
| Target intake + validation | real | RCSB fetch / upload, structural checks |
| Binding-site definition | real | pocket of a co-crystallised ligand, or manual box (Vina) |
| Compound library | real | ZINC20 catalogue fetched at run start into an on-disk cache (`scripts/prefetch_zinc.py` pre-fills it), or built-in/uploaded |
| Docking — AutoDock Vina | **real, validated** | Vina 1.2.7 (re-docking of the 7RPZ ligand: 0.51 Å RMSD) |
| Docking — BioSolveIT FlexX | **real** | FlexX 6.5.3; real run + `tests/integration` pass (output format learned from the real binary) |
| HYDE re-scoring | **real** | `hydescorer` 2.5.3, vendor tags parsed |
| Synthetic accessibility | **real** | RDKit SAscore |
| ADMET | **real model** | ADMET-AI (Chemprop-RDKit, TDC data), CPU |
| Binding affinity, unbinding kinetics (Koffee) | **placeholder, deferred** | values labelled "mocked", never used for filtering/ranking |
| FastGrow analogue generation | **real, optional** | FastGrow 1.6.3 grows fragments from the top candidates; results are *generated suggestions*, listed separately |

Scores are never combined: docking, HYDE, SA and ADMET are separate, labelled columns; the ranking
policy (a single named measure) is stated on every campaign.

## Run it locally

```bash
python -m venv .venv
.venv/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu -c constraints.txt -r requirements-dev.txt
./scripts/install_tools.sh ~/Downloads/biosolveit-folder   # unpacks FlexX/HYDE/FastGrow + licence, downloads Vina, verifies
./scripts/dev.sh                          # http://127.0.0.1:8008 (SQLite); tools are found automatically
.venv/bin/python scripts/verify_tools.py  # runs each real tool once; PASS/FAIL/SKIP
python scripts/run_campaign.py --count 20 # or use the UI
.venv/bin/pytest -q                       # 119 tests, no external tools needed
.venv/bin/pytest -q -m real tests/integration   # runs the REAL tools (skips what is missing)
```

## Architecture (one process, simple on purpose)

```
browser ── FastAPI ── SQLite/PostgreSQL ── data/runs/<campaign>/<stage>/batch_NNN/…
              └── campaign runner thread ── subprocess (vina / hydescorer / flexx), safe & killable
```

* **Campaign → stages → batches → molecules**, all in the database (`jobs`, `job_stages`, `batches`,
  `stage_results`, `molecules`). The UI is a pure view of that state; refresh/restart loses nothing.
* A batch is claimed with a conditional `UPDATE` (no duplicate execution), has a hard timeout, is
  cancellable, retried automatically up to `MAX_BATCH_ATTEMPTS`, then left `failed` for selective retry.
* Resume after crash: unfinished batches go back to `pending`; finished batches are never repeated;
  orphaned tool processes from a hard kill are terminated.
* Why no Celery/Redis: one host, one campaign at a time, and the database already is the queue. A broker
  would add failure modes without adding capacity. The runner is isolated in `backend/campaign/runner.py`
  if a worker pool is ever needed.
* Tool adapters: `backend/science/engines/{vina,flexx,hyde,fastgrow}.py`; filters/ranking: `campaign/runner.py`
  (`_apply_filter`) and `campaign/results.py`. Frontend: plain HTML + compiled Tailwind, no CDN needed.

See `docs/SCIENCE_NOTES.md` for the scientific decisions and their limits.

## API (selection)

`GET /api/tools` · `GET /api/targets/{id}/sites` · `POST /api/jobs` · `GET /api/jobs/{id}` ·
`GET /api/jobs/{id}/batches[/{batch}/log]` · `GET /api/jobs/{id}/molecules?outcome=` ·
`GET /api/jobs/{id}/candidates[.sdf|.csv]` · `POST /api/jobs/{id}/{cancel|resume|retry}` ·
`GET /api/candidates/{id}[/sdf|/complex]` · `GET /api/jobs/{id}/artifacts`. Interactive docs at `/docs`.
