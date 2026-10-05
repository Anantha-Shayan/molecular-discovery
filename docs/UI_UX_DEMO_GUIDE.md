# UI/UX & Demo Guide

How the application is organised, what each screen demonstrates technically,
and exactly how to walk through it in the take-home interview.

## 1. Product UX philosophy

**Scientific minimalism.** The interface should read like a computational
chemistry workstation, not a generic admin dashboard: warm off-white ground,
deep navy type, one restrained teal accent, thin borders, monospace for IDs
and numbers.

Three rules drive every screen:

1. **Every number on screen came from somewhere real.** Target metadata is
   parsed from the user's file; stage counts come from the database; engine
   badges come from the backend's own engine registry (`GET /api/engines`).
   Nothing is typed into the HTML.
2. **Real vs mocked is always visible.** Mocked stages carry a "mocked" or
   "demo adapter" badge on every screen they appear on, and the mocked values
   keep a `_mocked` suffix even inside exported SDF files.
3. **Show the funnel, not just the answer.** The product's value is the
   orchestration — target → stages → shrinking candidate set → provenance —
   so the run screen leads with the per-stage in → out counts.

## 2. Information architecture

```
Discoveries            (list of every run)
  └─ New Discovery     (step 1: choose target input)
       └─ Target Validation   (step 2: review what was parsed)
            └─ Configure & Review   (step 3: set parameters, review, start)
                 └─ Discovery Run   (live stage state → candidates → SDF)
```

Wizard state travels in the URL (`?target=<id>`, then `?job=<id>`), so every
step is reloadable and linkable. No client-side store.

| Page | File |
|---|---|
| Discoveries | `frontend/discoveries.html` |
| New Discovery | `frontend/new-discovery.html` |
| Target Validation | `frontend/target-validation.html` |
| Configure & Review | `frontend/configure-review.html` |
| Discovery Run | `frontend/dashboard.html` (existing screen, narrowly edited) |

The pages are served by the FastAPI app itself (`/app/...`), so the whole demo
is one process on one port.

## 3. The screens

### New Discovery
Three ways to define a target, all converging on one backend code path
(`parse_structure` → validate → persist):

| Input | Endpoint | Notes |
|---|---|---|
| Upload `.pdb/.ent/.cif/.mmcif` | `POST /api/targets/upload` | multipart; drag-and-drop supported |
| PDB ID | `POST /api/targets/pdb-id` | fetches from RCSB; clear error if offline |
| Demo target | `POST /api/targets/demo` | bundled fixture, works with no network |

This step **does not start a run**. Its only output is a validated Target
record plus a stored structure artifact.
*Demonstrates:* intake is separated from execution; failures are reported
with the specific gate that failed.

### Target Validation
Everything here is derived from the file:

- **3D viewer** — 3Dmol.js renders the actual stored structure text (cartoon /
  sticks / spheres, chain selector, non-solvent HETATM shown as sticks).
- **Structural metadata** — chains, residues, atom records, source, method,
  resolution, ligands/cofactors, title. Fields the file doesn't state show
  "Not stated in file" rather than a guess.
- **Five input checks** — structure readable, atom records present, coordinates
  detected, chains available, residues available. Each shows a computed detail
  line (e.g. "1,690 atom records (1,342 ATOM, 348 HETATM)").
- **Scope notice** — states plainly that this is structural input validation,
  not an assessment of druggability or docking suitability.

*Demonstrates:* real parsing and persistence; honest scoping of what
"validated" means.

### Configure & Review
Only parameters that actually change the run are exposed:

| Control | Effect |
|---|---|
| Demo candidate library (10 / 25 / 40) | slices the demo library |
| Max SA score | the real RDKit gate threshold |
| ADMET / Affinity / Kinetics toggles | stage is skipped and shown as skipped |
| "Favorable only" ADMET gate | tightens the mocked ADMET filter |
| Shortlist size | how many survive the mocked affinity stage |

The right-hand panel is a pre-flight review of target, pipeline, configuration
and environment (real vs demo engines). **Start Discovery** calls
`POST /api/jobs`, which returns a job ID immediately and starts the pipeline
as a background task, then redirects to the run screen.
*Demonstrates:* configuration is persisted with the job; the API returns
before the work finishes.

### Discovery Run
The original results workspace, now bound to one job (`?job=<id>`):

- target header from the stored Target record
- seven-stage funnel with live status, per-stage counts and in → out detail
- candidate table (ranked by mocked residence time — a presentation rule)
- candidate inspector with 2D depiction, properties and provenance trace
- per-candidate and whole-run SDF export

It polls `GET /api/jobs/{id}` once a second until the job is COMPLETE or
FAILED. Stage transitions are committed as they happen, so you can watch the
funnel fill in.

### Candidate inspection & SDF export
Selecting a row shows its real SA score and molecular weight (RDKit) alongside
clearly badged mocked values. The provenance trace lists each stage with a
real/mocked dot. "Export Candidate (.SDF)" and "Export All (.SDF)" return
genuine RDKit-generated SDF; property tags for mocked values end in `_mocked`
and each record carries a `PROVENANCE` statement.

## 4. What each screen shows technically

| Screen | Engineering point |
|---|---|
| New Discovery | input abstraction; one validated intake path |
| Target Validation | real parsing, persistence, artifact storage |
| Configure & Review | configuration as persisted job state; honest environment disclosure |
| Discovery Run | async orchestration, state machine, molecule lineage, provenance |
| SDF export | real RDKit I/O; provenance surviving outside the app |

## 5. Demo script (≈ 6 minutes)

Start the app: `uvicorn backend.main:app --port 8008`, open
`http://127.0.0.1:8008`.

1. **Open Discoveries.** Point out the honest framing box: what is real, what
   is a demo adapter.
2. **Click New Discovery.** Note the pipeline preview on the right — badges
   come from the backend, not the HTML.
3. **Click Load Demo Target.** (Optionally first show Upload and a bad file —
   e.g. an empty `.pdb` — to show the failing gate being named.)
4. **Review validation.** Rotate the 3D structure. Point at the metadata and
   the five checks: "these are read from the file." Read the scope notice.
5. **Continue to Configuration.** Change the SA threshold or library size so
   the review panel updates. Point at the engine table: one real, four demo.
6. **Click Start Discovery.** A job ID exists immediately.
7. **Watch the run screen.** The funnel fills stage by stage
   (25 → 20 → 13 → 8 for the default settings). Hover a stage for its in → out
   detail.
8. **Select a candidate.** Show real SA score vs mocked Kd / koff, and the
   provenance trace.
9. **Download the SDF.** Open it; show the `_mocked` suffixes and the
   `PROVENANCE` tag.
10. *(Optional)* **Re-run This Configuration** — identical results, showing
    determinism. Upload a different structure to show results change.

## 6. What NOT to claim

| Do **not** say | Say instead |
|---|---|
| "We screened 10 trillion molecules." | "The production architecture is meant to integrate ultra-large chemical-space screening. This MVP uses a deterministic demo library of 40 compounds, and the 10T+ figure is only a conceptual scale label." |
| "This is a validated drug candidate." | "This is a computationally generated candidate that would need downstream scientific and experimental validation." |
| "The binding affinity / koff predictions are…" | "Those values are deterministic placeholders from demo adapters. The architecture is built so a real engine replaces one adapter." |
| "ADMET evaluates 220 properties." | "The partner stage is described as covering 220+ properties; the demo adapter returns four illustrative values." |
| "The pipeline docked these compounds into KRAS." | "The compounds come from a fixed library and are not selected for the target. The structure is carried through the pipeline as an input artifact and seeds the demo values." |
| "We validated the target's druggability." | "Validation here is structural: the file is readable and has atoms, chains and residues." |
| "It runs on a GPU cluster." | "It runs as a background task in one local process. CPU/GPU separation is a documented production direction, not implemented." |
| "Koffee is…" | The original brief's "Koffee" naming is unexplained; use "binding affinity" and "unbinding kinetics". |

Also avoid: FEP and patent testing (out of scope), any claim that mocked values
are experimental, and any statement that results are clinically meaningful.
