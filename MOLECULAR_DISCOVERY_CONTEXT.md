# Molecular Discovery Platform — Claude Code Context

> **Purpose:** Persistent project context for Claude Code / coding agents.
>
> **Last updated:** 2026-10-05
>
> **Project type:** AI/ML Developer Intern take-home assignment
>
> **Company:** Molecular Solutions, Bengaluru
>
> **Primary goal:** Build a credible MVP integration platform for a computational drug-discovery workflow. The project is primarily an **integration / orchestration / engineering** problem, not an exercise in inventing new computational chemistry models.

---

> **Update 2026-10-05:** the New Discovery flow, PDB validation, target persistence,
> service adapters, per-run artifacts and 40-compound library are now implemented.
> Sections below describing "no PDB upload" or a hardcoded target are historical;
> see README.md and docs/ for current behaviour.

---

# 1. PROJECT IDENTITY

## Project

**Molecular Discovery Platform — Integration MVP**

The intended product is a unified enterprise/scientific application that takes a protein target as input and runs a multi-stage computational drug-discovery workflow, integrating pre-existing scientific engines/services.

The conceptual workflow is:

```text
Protein Target / PDB
        ↓
Chemical Space Screening / Docking
        ↓
Synthetic Accessibility (SA)
        ↓
ADMET Prediction
        ↓
Binding Affinity
        ↓
Unbinding Kinetics / koff
        ↓
Ranked Candidate Molecules
```

The output is **not a guaranteed drug**. It is a set of computationally identified/generated candidate molecules that require experimental validation.

The original assignment context says Molecular Solutions already has software for the individual scientific stages, through internal/partner technologies, and the developer's role is to integrate those capabilities into a unified application.

The original project brief explicitly instructed that proprietary implementation details must not be invented and that unknowns should be distinguished from confirmed information.

---

# 2. ORIGINAL SOURCE / WORKFLOW IMAGE

The original workflow slide is titled:

**Modern Drug Discovery Workflow**

The visible funnel contains approximately:

1. Chemical Space Docking — shown as `10 Trillion Spaces`
2. SA Score
3. ADMET Predictor — shown as `220+ ADMET Properties`
4. Koffee Binding Affinity
5. Koffee Unbinding Kinetics — associated with residence time
6. Free Energy Perturbation / FEP
7. Patent testing
8. Experimental Validation

The company/project instructions say that **FEP and patent testing can be ignored for this take-home MVP**.

Important terminology caution:

- The slide says **"Koffee"** for binding affinity and unbinding kinetics.
- It is unknown whether Koffee is a specific proprietary product, internal naming, or a design label.
- Do not invent what Koffee specifically is.
- For the MVP, use technology-agnostic terms:
  - Binding Affinity
  - Unbinding Kinetics
  - koff
  - Residence Time

The original understanding and subsequent analysis concluded that the "10 trillion" figure should be treated as the conceptual scale of a chemical search space, **not as 10 trillion full physics-based docking runs**.

---

# 3. IMPORTANT SCIENTIFIC / PRODUCT ASSUMPTIONS

## Confirmed / project-level facts

- The application is intended to integrate multiple computational drug-discovery stages.
- The individual scientific engines are not expected to be reimplemented from scratch.
- PDB is the main protein structure format mentioned by the company/project.
- SDF is the main small-molecule structure format mentioned by the company/project.
- Binding affinity software reportedly runs in Linux/Docker and uses CPU.
- Unbinding kinetics software reportedly runs in Linux/Docker and uses GPU.
- FEP and patent testing are out of scope for this MVP.
- The system should eventually produce candidate molecules, not claim to produce validated drugs.
- The MVP may use mocks where proprietary/partner software is unavailable.

## Reasonable scientific interpretation

### Chemical-space stage

The `10T+` figure is best treated as an **ultra-large chemical-space / virtual-screening scale**.

Do not implement or claim to implement a trillion-compound search in the MVP.

The current implementation uses a tiny deterministic seed library as a mock of the screening stage.

### SA score

The conventional Ertl & Schuffenhauer synthetic accessibility score is:

```text
1 = easier to synthesize
10 = harder to synthesize
```

The MVP uses the real RDKit implementation of this score.

Do not use a 1–100 scale.

### ADMET

The project information says the ADMET stage can produce `220+` properties/descriptors.

The exact partner schema is unknown.

The MVP therefore uses a small illustrative mocked subset and records `property_count = 220`.

Do not claim that the MVP implements a real 220-property ADMET model.

### Binding affinity

The real partner implementation is unknown.

The MVP represents the result with:

- `Kd` in nM
- illustrative `ΔG` in kcal/mol

These values are mocked.

### Unbinding kinetics

The real implementation is unknown.

The MVP represents:

- `koff` in s^-1
- residence time in minutes

The conceptual relationship is:

```text
residence time τ ≈ 1 / koff
```

The current values are deterministic mock values, not physical simulations.

---

# 4. SCIENTIFIC LAYER VS INTEGRATION LAYER

## Scientific layer

These are computational chemistry / ML capabilities that would normally belong to partner or specialized scientific software:

- Ultra-large chemical-space screening
- Docking / screening
- Synthetic accessibility scoring
- ADMET prediction
- Binding-affinity estimation
- Unbinding kinetics / koff estimation
- Molecular dynamics / enhanced sampling, if used by the real partner

## Integration layer — primary responsibility of this project

The MVP should demonstrate:

- UI
- REST API
- Job creation
- Pipeline orchestration
- Stage state tracking
- Data persistence
- Molecule identity tracking
- PDB/SDF artifact handling
- External-service adapter boundaries
- CPU/GPU workload representation
- Mock service integration
- Results aggregation
- Candidate presentation
- SDF export
- Provenance
- Clear distinction between real and mocked outputs
- Engineering trade-offs and extensibility

Do **not** start rebuilding scientific models unless there is a very strong reason.

---

# 5. DATA FLOW AND STAGE INPUT/OUTPUT CONTRACTS

This section is intentionally explicit because future coding decisions should preserve these contracts.

## Stage 01 — Target / Protein

### Input

One of:

- PDB file upload
- PDB ID

Current MVP: upload / PDB ID / demo target. The demo target is **7RPZ** (genuine KRAS G12D). The earlier "8AZX" label was wrong — 8AZX is KRAS G12C.

### Intended output

A validated/stored target record plus a PDB artifact.

Conceptually:

```text
Target
 ├── target_id
 ├── name
 ├── source
 ├── pdb_id
 └── artifact_id
```

### Future validation

Potential checks:

- file type
- parseability
- valid ATOM/HETATM records
- chain selection
- missing structural data
- malformed coordinates
- target identity
- optional pocket/preparation metadata

Do not add all of these unless required; they are production-direction ideas.

---

## Stage 02 — Chemical Space Screening

### Input

Primary input:

```text
Protein / PDB
```

Potentially also:

- pocket/chain information
- screening parameters

### Real-world conceptual output

A collection of candidate molecules, potentially with:

- molecule structure
- SMILES
- SDF
- screening score / pose metadata

### MVP output

A fixed deterministic list of 10 drug-like SMILES from:

```text
backend/stages/mocks.py
```

The seed library is intentionally tiny.

It represents:

```text
10T+ conceptual chemical space
        ↓
small demo seed set
```

It does NOT actually screen 10T+ molecules.

Every screening result is:

```text
is_mocked = True
```

### Molecule identity rule

Create the internal canonical `Molecule.id` when a candidate first enters the system.

Never depend on downstream SMILES matching as the primary identity mechanism.

---

## Stage 03 — Synthetic Accessibility

### Input

Candidate molecule:

```text
SMILES
```

### MVP implementation

This stage is **REAL**.

File:

```text
backend/stages/sa_scoring.py
```

Uses:

```text
RDKit
Ertl & Schuffenhauer SAscore
```

Also computes:

- molecular weight
- ring count

### Output

Example shape:

```json
{
  "sa_score": 2.96,
  "scale": "1 (easy) - 10 (hard)",
  "molecular_weight": 468.5,
  "num_rings": 4,
  "method": "Ertl & Schuffenhauer SAscore (RDKit Contrib/SA_Score)"
}
```

`is_mocked = False`.

### Current filtering

The job parameter:

```text
sa_threshold
```

defaults to:

```text
5.0
```

Candidates with:

```text
sa_score <= threshold
```

survive.

---

## Stage 04 — ADMET

### Input

Primarily:

```text
Small molecule structure / SMILES / SDF
```

The protein is not required for ordinary ligand-level ADMET prediction.

### Real-world conceptual output

Potentially hundreds of descriptors/endpoints.

The exact schema is unknown.

### MVP implementation

File:

```text
backend/stages/mocks.py
```

Returns a deterministic mock containing:

```json
{
  "profile": "Favorable",
  "property_count": 220,
  "sample_properties": {
    "logP_est": "...",
    "hbd_est": "...",
    "hba_est": "...",
    "caco2_permeability_class": "..."
  },
  "note": "Mocked — illustrative subset only, not a real ADMET model output."
}
```

`is_mocked = True`.

Current filter:

```text
Favorable OR Moderate
```

so effectively all current mocked survivors continue.

---

## Stage 05 — Binding Affinity

### Input

Conceptually:

```text
Protein PDB
+
candidate ligand structure / pose
```

The actual partner input contract is unknown.

### MVP output

Mock:

```json
{
  "kd_nm": 12.4,
  "delta_g_kcal_mol": -8.7
}
```

Values are deterministic pseudo-random values based on SMILES.

`is_mocked = True`.

The UI currently labels the stage:

```text
Kd & ΔG Ranking
```

This is a UI representation, not proof of the real partner's algorithm.

The reported CPU/Docker requirement should be represented as a constraint:

```text
CPU / Docker
```

not as a claim about a specific algorithm.

---

## Stage 06 — Unbinding Kinetics

### Input

Conceptually:

```text
Protein
+
bound ligand / pose
+
possibly affinity/trajectory parameters
```

Exact partner contract is unknown.

### MVP output

Mock:

```json
{
  "koff_per_s": 8.4e-5,
  "residence_time_min": 198.4
}
```

The current mock uses affinity loosely to influence the output, but this is NOT physical modeling.

`is_mocked = True`.

UI represents:

```text
koff
Residence Time τ
GPU / Docker
```

Do not describe this as real metadynamics in the MVP.

---

## Stage 07 — Candidate Output

The final output is a shortlist of candidate molecules.

The MVP displays:

- rank
- molecule ID
- 2D structure
- molecular weight
- SA score
- ADMET profile
- Kd
- koff
- residence time
- status

Final candidates can be downloaded as SDF.

The SDF structure/file generation itself is **real RDKit I/O**, but the ADMET/affinity/kinetics property values embedded in it may be mocked.

---

# 6. CURRENT MVP ARCHITECTURE

```text
                 ┌─────────────────────────────┐
                 │        Browser UI           │
                 │ frontend/dashboard.html     │
                 └──────────────┬──────────────┘
                                │ HTTP
                                ▼
                 ┌─────────────────────────────┐
                 │        FastAPI API           │
                 │ backend/main.py              │
                 └──────────────┬──────────────┘
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
             ┌─────────────┐       ┌─────────────────┐
             │ SQLAlchemy  │       │ Pipeline        │
             │ SQLite      │       │ Orchestrator    │
             └─────────────┘       └────────┬────────┘
                                             │
                    ┌────────────────────────┼─────────────────────┐
                    ▼                        ▼                     ▼
              Real SA/RDKit             Mock ADMET           Mock Affinity
                    │                        │                     │
                    └────────────────────────┼─────────────────────┘
                                             ▼
                                      Mock Kinetics
                                             │
                                             ▼
                                    Candidate Results
                                             │
                                             ▼
                                        SDF Export
```

---

# 7. CURRENT REPOSITORY STRUCTURE

Current delivered MVP:

```text
molecular-discovery-mvp/
│
├── requirements.txt
├── README.md
│
├── frontend/
│   └── dashboard.html
│
├── backend/
│   ├── __init__.py
│   ├── main.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── pipeline.py
│   │
│   └── stages/
│       ├── __init__.py
│       ├── mocks.py
│       ├── depiction.py
│       └── sa_scoring.py
│
└── data/
    └── app.db   (created at runtime)
```

---

# 8. CURRENT DATABASE MODEL

The current schema deliberately uses a generic `StageResult.payload` JSON field because real partner output schemas are not known.

Relationship:

```text
Target
  │ 1
  │
  │ many
  ▼
Job
  │
  ├───────────────┐
  │               │
  ▼               ▼
Molecule      JobStageLog
  │
  │
  ▼
StageResult

Artifact
  └── references stored files
```

## Target

```text
id          UUID/string PK
name
source      upload | pdb_id
pdb_id
artifact_id
created_at
```

## Job

One pipeline execution.

```text
id
target_id
status
current_stage
failure_reason
params JSON
label
created_at
updated_at
```

Current status values include:

```text
SUBMITTED
SCREENING
SA_FILTER
ADMET
AFFINITY
KINETICS
COMPLETE
FAILED
```

## JobStageLog

Append-only stage execution log.

```text
id
job_id
stage
status
attempt
external_job_id
detail
started_at
finished_at
```

Current stage statuses include:

```text
STARTED
SUCCEEDED
FAILED
RETRIED
```

Retry is represented in the schema but automatic retry/backoff is NOT implemented yet.

## Molecule

Canonical candidate identity.

```text
id
job_id
display_id
smiles
inchikey
source_stage
created_at
```

Example display ID:

```text
MDP-0894-001
```

## StageResult

One result per molecule per stage.

```text
id
molecule_id
job_id
stage
payload JSON
is_mocked
created_at
```

Example:

```json
{
  "stage": "sa",
  "payload": {
    "sa_score": 2.96
  },
  "is_mocked": false
}
```

## Artifact

File pointer, not file bytes.

```text
id
job_id
molecule_id
stage
kind
storage_path
created_at
```

Kinds currently intended:

```text
pdb
sdf_input
sdf_output
csv_results
```

The production direction is object storage rather than putting file bytes in the database.

---

# 9. CURRENT API

Implemented endpoints:

```text
POST /api/jobs
GET  /api/jobs/{job_id}
GET  /api/jobs/{job_id}/candidates
GET  /api/candidates/{molecule_id}
GET  /api/candidates/{molecule_id}/sdf
```

## POST /api/jobs

Creates a target + job and starts the pipeline through FastAPI `BackgroundTasks`.

Current request shape:

```json
{
  "target_name": "KRAS G12D",
  "pdb_id": "8AZX",
  "run_tag": "0894",
  "library_limit": 10,
  "sa_threshold": 5.0
}
```

This is currently hardcoded by the UI.

## GET /api/jobs/{job_id}

Returns:

- job ID
- status
- current stage
- target name
- PDB ID
- seven stage cards
- retained candidate count

Stage card contains:

```text
stage_no
key
label
subtitle
count
count_label
badge
status
is_mocked
scale_note
```

## GET /api/jobs/{job_id}/candidates

Returns candidate rows with:

```text
molecule_id
display_id
rank
smiles
structure_svg
molecular_weight
sa_score
admet_profile
kd_nm
koff_per_s
residence_time_min
status
```

Candidates are currently ranked by:

```text
residence_time_min DESC
```

This is an MVP presentation rule, not a scientifically validated composite ranking.

## GET /api/candidates/{molecule_id}

Returns detailed molecule information plus provenance.

## GET /api/candidates/{molecule_id}/sdf

Returns a genuine RDKit-generated SDF file.

The structure is real, but any mocked stage properties embedded in the SDF are clearly named as mocked.

---

# 10. CURRENT PIPELINE EXECUTION MODEL

The current MVP deliberately does NOT use Celery/Redis/Kafka/Temporal/Airflow/Kubernetes.

Instead:

```text
POST /api/jobs
     ↓
FastAPI BackgroundTasks
     ↓
pipeline.run_pipeline()
     ↓
Sequential stage execution
```

This is intentional.

Reason:

- demo only
- 10-molecule seed set
- no real long-running partner computation
- adding a broker now would be over-engineering

Production direction:

```text
API
 ↓
Durable job/workflow engine
 ↓
Stage queues
 ├── CPU workers
 └── GPU workers
 ↓
External scientific APIs / containers
```

Potential production technologies could include Celery/Redis, Temporal, Prefect, or another durable workflow system, but do not add one merely for appearance.

---

# 11. CURRENT REAL VS MOCKED STATUS

| Stage | Current status |
|---|---|
| Target input | REAL — upload, PDB ID (RCSB), or bundled demo |
| PDB validation | REAL — structural checks only (backend/targets/structure.py) |
| PDB upload | REAL |
| Chemical-space screening | MOCK |
| SA scoring | REAL |
| Molecular weight | REAL |
| 2D structure depiction | REAL |
| ADMET | MOCK |
| Binding affinity | MOCK |
| Unbinding kinetics | MOCK |
| Candidate status classification | Real application logic, not scientific prediction |
| SDF generation | REAL RDKit I/O |
| SDF property values for mocked stages | MOCK |
| Database persistence | REAL |
| Job state machine | REAL MVP implementation |
| Stage logs | REAL MVP implementation |
| Async execution | FastAPI BackgroundTasks |
| Queue/broker | NOT implemented |
| Retry/backoff | NOT implemented |
| Authentication | NOT implemented |

This distinction is critical.

Never present mocked outputs as scientific predictions.

---

# 12. CURRENT UI / DESIGN SYSTEM

The UI was generated first as a premium enterprise scientific application.

Design direction:

**Scientific Minimalism**

The visual intent is:

> modern computational chemistry workstation + premium enterprise biotech product

NOT:

- generic SaaS
- Material Design
- generic admin dashboard
- excessive claymorphism
- excessive glassmorphism
- consumer AI interface

## Visual language

- warm off-white/light background
- deep navy/charcoal typography
- restrained teal/emerald accent
- thin borders
- subtle shadows
- generous whitespace
- strong typography hierarchy
- monospace typography for scientific IDs/numbers
- subtle molecular visual language
- analytical tables
- restrained iconography

Primary visual concepts:

```text
PRECISION
SCIENCE
COMPUTATION
TRUST
TRACEABILITY
```

## Current navigation

Top navigation:

```text
Molecular Discovery Platform
Discoveries
Projects
Molecules
Jobs
Knowledge
Search
Notifications
User
```

## Current main screen

The current screen is a **Discovery Run / Results workspace**, not the new-discovery empty state.

It contains:

1. Header / navigation
2. Run breadcrumb and active-stage indicator
3. Protein target header
4. Multi-stage funnel cascade
5. Candidate table
6. Candidate detail inspector
7. Provenance/activity information

## Pipeline visual centerpiece

The funnel is the key product visualization.

Current conceptual progression:

```text
TARGET
   ↓
CHEMICAL SPACE
   ↓
SYNTHETIC ACCESSIBILITY
   ↓
ADMET
   ↓
BINDING AFFINITY
   ↓
UNBINDING KINETICS
   ↓
CANDIDATES
```

The UI uses illustrative attrition numbers.

IMPORTANT:

`10T+` is a conceptual scale label, not the number of molecules actually processed by the demo.

The demo library has 40 molecules (runs use 10/25/40).

## Candidate table

Current columns:

```text
Rank
Molecule ID
2D Structure
MW
SA Score
ADMET
Binding Affinity
koff
Residence Time
Status
```

## Candidate inspector

Displays:

- molecule ID
- rank
- 2D structure
- Kd
- koff
- residence time
- SA score
- ADMET profile
- canonical SMILES
- SDF export
- provenance

## Mock transparency

The UI uses badges/banner/provenance to distinguish mocked stages.

This is important and should not be removed.

---

# 13. CURRENT FRONTEND BEHAVIOR

`frontend/dashboard.html` is a static HTML/Tailwind/JS interface.

It uses:

```text
Geist
Space Grotesk
JetBrains Mono
Material Symbols
Tailwind CDN
```

API base:

```javascript
const API_BASE = "http://127.0.0.1:8008";
```

The UI:

1. checks backend connectivity
2. allows Run Pipeline
3. POSTs `/api/jobs`
4. polls `/api/jobs/{id}` every ~1.2 seconds
5. updates stage cards
6. updates attrition counts
7. waits for COMPLETE/FAILED
8. fetches candidate list
9. selects first candidate
10. fetches candidate details
11. enables SDF download

The UI currently hardcodes:

```text
target_name = KRAS G12D
pdb_id = 8AZX
library_limit = 10
sa_threshold = 5.0
```

This should eventually become user-configurable.

---

# 14. IMPORTANT ENGINEERING DECISIONS ALREADY MADE

Do not casually undo these decisions.

## Decision 1 — Generic stage result JSON

Use:

```text
StageResult.payload JSON
```

because the real partner schemas are unknown.

Future production evolution:

```text
typed stage-specific tables
```

once real partner schemas are stable.

## Decision 2 — Molecule ID is the canonical identity

Do not join stages using SMILES alone.

Use:

```text
Molecule.id
```

as the stable internal identity.

SMILES / InChIKey are secondary molecular identifiers.

## Decision 3 — No fake composite scientific score

Do not invent:

```text
final_score = 0.3 * SA + 0.4 * affinity + ...
```

There is no scientific basis available for such weighting.

Keep scientific metrics separate.

Use simple status buckets only for UI:

```text
Nominated
Shortlisted
Review
```

These are presentation classifications, not validated scientific conclusions.

## Decision 4 — Mock transparency

Every mocked StageResult must have:

```text
is_mocked = True
```

and the UI should communicate that the value is simulated.

## Decision 5 — SDF export is genuine

Use RDKit to create valid SDF output.

Do not replace it with a text-only fake if it can remain real.

## Decision 6 — MVP should remain understandable

Do not introduce Kubernetes, microservices, distributed queues, event sourcing, or other enterprise infrastructure merely to look impressive.

The interview signal should come from clear engineering boundaries and reasoning.

---

# 15. CURRENT KNOWN GAPS

These are known and should be considered when deciding what to implement next.

### High-value MVP gaps

1. Actual PDB upload
2. PDB input validation
3. User-configurable target / PDB
4. User-configurable pipeline parameters
5. Better failure handling
6. Retry/backoff
7. More realistic stage adapter interfaces
8. Better run history / project organization
9. More explicit stage-level mock metadata

### Production gaps

- authentication / authorization
- PostgreSQL
- object storage
- real external APIs
- real CPU worker pool
- real GPU worker pool
- durable workflow orchestration
- secrets management
- observability
- structured logs
- retry policies
- timeouts
- idempotency
- artifact versioning
- reproducibility
- audit/provenance
- horizontal scaling

Start by implementing demo gaps. You can later go to production gaps.

---

# 16. RECOMMENDED NEXT DEVELOPMENT DIRECTION

When continuing this project, prioritize in this order.

## Phase A — Make the MVP input flow real

Implement:

```text
New Discovery
    ↓
Upload PDB
    OR
Enter PDB ID
    ↓
Validate target
    ↓
Create Target
    ↓
Start Job
```

The existing results workspace can remain as the run/results screen.

## Phase B — Improve stage abstraction

Introduce a clear conceptual adapter interface:

```text
ScreeningService
SAService
ADMETService
BindingAffinityService
UnbindingKineticsService
```

Mocks should implement the same conceptual contracts that real services would eventually implement.

For example:

```text
service.submit(...)
service.get_status(...)
service.get_result(...)
```

Do not force all services into identical behavior if their real contracts would differ; the goal is a clean integration boundary.


## Phase C — Improve artifact handling

Introduce a real artifact abstraction for:

```text
PDB input
screening SDF
SA output
ADMET results
affinity results
kinetics results
final candidate SDF
```

For MVP, local filesystem is acceptable.

Production direction: object storage.

## Phase D — Improve failure/retry semantics

Add:

- per-stage attempt
- retryable vs permanent failure
- backoff
- timeout
- stage-level error detail

The existing `JobStageLog` should remain the audit trail.

## Phase E — Only then consider queue/worker architecture

If the demo starts simulating long-running jobs, introduce:

```text
API
 ↓
Queue
 ↓
CPU worker
 ↓
GPU worker
```

But do not add it merely for architectural decoration.

---

# 17. WHAT NOT TO DO

Future agents must avoid the following.

### Do not claim:

- the demo actually searches 10 trillion compounds
- the demo uses Enamine REAL
- the demo uses FEP
- the demo uses a particular ADMET vendor
- the demo uses a particular Koffee implementation
- mocked Kd values are real predictions
- mocked koff values come from molecular dynamics
- the output is a validated drug

### Do not add unsupported scientific claims.

### Do not remove mock labels.

### Do not invent a scientifically unjustified final score.

### Do not over-engineer the MVP.

### Do not replace the existing visual design without a clear reason.

### Do not make the product look like a generic Material/SaaS dashboard.

### Do not make every scientific value look experimentally validated.

---

# 18. INTERVIEW STORY

The project should be explainable in approximately this form:

> "I treated the problem as an integration and orchestration system rather than trying to rebuild the underlying drug-discovery models. The platform accepts a protein target, creates a pipeline job, tracks the candidate molecules through each computational stage, persists stage outputs and artifacts, and exposes the results through a unified scientific UI. Since the proprietary partner engines weren't available, I implemented the SA stage with real RDKit chemistry and built deterministic, explicitly labelled mocks around it. I designed the data model so real partner integrations can replace those mocks without changing molecule identity, orchestration, or UI contracts."

Key interview concepts:

- containerization
- API integration
- async jobs
- state machines
- retries
- idempotency
- CPU/GPU separation
- PDB/SDF
- molecule lineage
- schema flexibility
- provenance
- real vs mocked computation
- reproducibility
- deployment

---

# 19. EXPECTED INPUT / OUTPUT SUMMARY

## System input

```text
Protein target
    ├── PDB file
    └── or PDB ID

Optional:
    ├── chain
    ├── pocket
    ├── screening parameters
    ├── SA threshold
    └── other stage parameters
```

## Internal pipeline inputs

```text
Stage 02:
PDB + configuration

Stage 03:
candidate SMILES/SDF

Stage 04:
candidate molecule structure

Stage 05:
PDB + ligand/candidate structure

Stage 06:
PDB + bound ligand/pose + relevant simulation inputs

Stage 07:
aggregated candidate results
```

## System output

```text
Ranked candidate molecules
    ├── molecule ID
    ├── structure
    ├── SMILES
    ├── SA score
    ├── ADMET summary
    ├── binding affinity
    ├── koff
    ├── residence time
    ├── provenance
    └── downloadable SDF
```

---

# 20. DEFINITION OF DONE FOR THE CURRENT TAKE-HOME MVP

A strong MVP should demonstrate:

### Functional

- [x] create a discovery job
- [x] execute a multi-stage pipeline
- [x] real SA scoring
- [x] deterministic mock screening
- [x] deterministic mock ADMET
- [x] deterministic mock affinity
- [x] deterministic mock kinetics
- [x] persist job/molecule/stage results
- [x] expose API
- [x] update UI from live API
- [x] show pipeline status
- [x] show candidates
- [x] show candidate details
- [x] generate real SDF
- [x] show provenance
- [x] distinguish real vs mocked data

### Strong next additions

- [x] real PDB upload
- [x] PDB validation
- [x] configurable target
- [x] configurable parameters
- [ ] retry/backoff
- [x] cleaner service adapter interfaces
- [x] artifact management (local filesystem, data/runs/)
- [ ] improved error states

### Production-only

- [ ] PostgreSQL
- [ ] object storage
- [ ] auth
- [ ] real partner APIs
- [ ] CPU/GPU worker pools
- [ ] durable workflow engine
- [ ] full observability
- [ ] secrets management
- [ ] deployment infrastructure

---

# 21. CODING AGENT OPERATING RULES

Before modifying the project:

1. Read this file.
2. Read the existing README.
3. Inspect the relevant existing implementation.
4. Preserve working behavior.
5. Prefer incremental changes.
6. Run a smoke test after meaningful backend changes.
7. Do not silently change scientific semantics.
8. Clearly mark anything that is mocked.
9. Do not invent partner behavior.
10. Keep interfaces replaceable.
11. Keep the UI scientifically credible.
12. Explain architectural trade-offs when introducing infrastructure.

When uncertain, prefer:

```text
explicit uncertainty
>
invented certainty
```

When choosing between:

```text
quick demo complexity
vs
production complexity
```

choose the smallest implementation that demonstrates the engineering principle, then document the production evolution.

---

# 22. CURRENT PRIMARY FILES TO READ FIRST

For coding tasks, start with:

```text
README.md
backend/models.py
backend/schemas.py
backend/pipeline.py
backend/main.py
backend/stages/sa_scoring.py
backend/stages/mocks.py
backend/stages/depiction.py
frontend/dashboard.html
```

The existing implementation is already a coherent MVP. Treat it as the baseline rather than rebuilding the project.

---

# 23. SOURCE OF TRUTH HIERARCHY

When information conflicts:

1. Explicit current project requirements / user instructions
2. This context document
3. Existing working code and README
4. Original workflow image / project discussion
5. General scientific knowledge
6. Speculation

If the real partner behavior is unknown, **do not manufacture it**.

The purpose of this context file is to allow a new coding agent to continue development without losing the architectural, scientific, UI, and MVP decisions already made.
