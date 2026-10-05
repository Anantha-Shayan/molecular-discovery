# Demo Data

What the demo runs on, what is real, what is mocked, and how each demo
component would be replaced by a real partner service.

## Demo target

`data/fixtures/demo_kras_g12d_7rpz.pdb` — an unmodified copy of RCSB entry
**7RPZ** (KRAS G12D in complex with MRTX-1133). Provenance and sha256 are in
`data/fixtures/README.md`.

Everything the UI shows about it is parsed from the file: chain A, 168
residues, 1,690 atom records (first conformer), X-ray diffraction at 1.30 Å,
non-solvent HETATM residues `6IC`, `GDP`, `MG`.

It is a *demo fixture*: realistic input data, nothing more. It is not a
claim about KRAS G12D as a drug target.

> **Correction.** Earlier versions of this project labelled the target
> "KRAS G12D / 8AZX". 8AZX is KRAS **G12C** (BI-2865 complex). 7RPZ is used
> because residue 12 of its chain A is genuinely `ASP`.

The PDB-ID intake path answers `7RPZ` from this bundled file, so the happy path
never needs the network. Other IDs are fetched from RCSB and fail with a clear
message when offline.

## Demo molecule library

`backend/stages/library.py` — 40 well-known molecular structures (approved
drugs and common natural products) as deterministic stand-ins for "what a
screening stage returned".

- **They are not screening results.** They were not selected for any target.
- Every SMILES parses in RDKit, so the one real stage runs on genuine
  chemistry. The recorded `sa_reference` values are regression anchors only;
  the pipeline always recomputes SA.
- Roughly a fifth of the library scores above the default SA threshold of 5.0
  (8 of 40), so the filter visibly rejects candidates.
- Library size options 10 / 25 / 40 slice the list in order, and each slice
  keeps a mix of easy and hard structures.

Default funnel (25 compounds, SA ≤ 5.0, strict ADMET gate, shortlist 8):

```
25 screened → 20 pass SA → 13 pass ADMET → 8 shortlisted → 8 with kinetics
```

## Real vs mocked

| Component | Status | Notes |
|---|---|---|
| Target intake (upload / PDB ID / demo) | **Real** | |
| PDB / mmCIF parsing & validation | **Real** | structural checks only |
| Target persistence & artifact storage | **Real** | SQLite + `data/targets/` |
| Job creation, configuration persistence | **Real** | |
| Pipeline orchestration & state machine | **Real** | commits per stage |
| Molecule identity & lineage | **Real** | `Molecule.id`, never SMILES |
| SA score | **Real** | RDKit Ertl & Schuffenhauer |
| Molecular weight, 2D depiction | **Real** | RDKit |
| SDF generation | **Real** | RDKit I/O |
| Chemical-space screening | **Mocked** | fixed library |
| ADMET | **Mocked** | 4 illustrative values + label |
| Binding affinity (Kd, ΔG) | **Mocked** | deterministic placeholders |
| Unbinding kinetics (koff, τ) | **Mocked** | deterministic placeholders |
| Status buckets (Nominated…) | App logic | presentation labels, not science |

Mocked `StageResult` rows are stored with `is_mocked = true`, carry a `note`,
and surface as "mocked" in the API, the UI and the exported SDF.

## Deterministic behaviour

Mocked values are a SHA-256 hash of `stage : target_seed : smiles`, where
`target_seed = <structure checksum>:<chain>`.

- Same target + same configuration → identical results, every run.
- A different structure file → different mocked values (the target is consumed,
  not just displayed). SA scores do **not** change, because SA is a property of
  the molecule alone.
- This is a demo property, not science: the hash is not a physical model.

The only nondeterminism is timing. Mocked stages sleep briefly
(`STAGE_DELAY_SECONDS`, default 0.8 s per stage, or `stage_delay_seconds`
per job) so progress is watchable; real SA is never delayed. Tests set it to 0.

## Generated artifacts

```
DATA_DIR/                      ./data locally, /var/lib/mdp in the container
  app.db                       SQLite database (local development only)
  targets/{target_id}/structure.pdb      stored copy of each target
  runs/{job_id}/
    target/target.pdb          structure staged into the run
    screening/screening_candidates.smi
    sa/sa_scores.json
    admet/admet_results.json
    affinity/affinity_results.json
    kinetics/kinetics_results.json
    final/candidates.sdf       real RDKit SDF of final candidates
```

Each file has an `Artifact` row pointing at it (path stored relative to `DATA_DIR`);
file contents are never stored in the database. The demo fixture lives separately, in
`data/fixtures/` inside the application (and the Docker image), so it is available
regardless of where `DATA_DIR` points.

## Reset / regenerate

**Docker deployment** (PostgreSQL) — see `docs/DEPLOYMENT.md` §12:

```bash
docker compose down -v      # DESTRUCTIVE: deletes the database and all artifacts
docker compose up -d        # fresh and migrated
```

**Local development** (SQLite):

```bash
rm -f data/app.db data/app.db-wal data/app.db-shm
rm -rf data/runs data/targets
```

Tables and folders are recreated on next start. Fixtures are untouched. Same inputs
reproduce the same results.

## Replacing demo components with partner services

Each stage is an adapter class in `backend/services/engines.py`. Replacing a
mock means rewriting one class body; the orchestrator, schema, API and UI do
not change.

| Adapter | Real counterpart would… |
|---|---|
| `ChemicalSpaceScreeningService` | submit the target to a screening engine and return hits with scores/poses |
| `SAService` | already real |
| `ADMETService` | call the ADMET service and return its descriptor set (store full output in `StageResult.payload`) |
| `BindingAffinityService` | run the CPU/Docker affinity job on target + ligand pose |
| `UnbindingKineticsService` | run the GPU/Docker kinetics job; this stage is long-running |

Likely changes at that point (deliberately not built now): replace the
in-process background task with a durable queue and CPU/GPU worker pools; add
retry/backoff using the existing `JobStageLog` attempt column; move artifacts
to object storage; move SQLite to Postgres with proper migrations. The partner
input/output contracts are unknown, so none of this is invented here.
