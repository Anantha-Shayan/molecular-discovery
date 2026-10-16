# Implementation checklist

Legend: ✅ done and evidenced · 🟡 done, not validated against the real vendor tool (none left) · ⛔ blocked · ➖ out of scope this round

## Phase 0 — audit
- ✅ Repository, docs, licence header, local environment inspected (see ACCEPTANCE_REPORT §1)
- ✅ Findings: `flexx` download was a duplicate of FastGrow; Koffee not supplied; the company extractor later arrived as `HYDE_SCORER.html` and is now used

## Phase 1 — real-tool proof (local)
- ✅ HYDE 2.5.3 runs, licence valid (`--license-info`), scored the 7RPZ crystal complex and Vina poses
- ✅ Vina 1.2.7 re-docking of 6IC into 7RPZ: 0.51 Å RMSD
- ✅ ADMET-AI loads and predicts offline on CPU
- ✅ FlexX 6.5.3 (second download is genuine): licence valid, real docking run · ➖ Conformator not needed (RDKit supplies 3D)

## Phase 2 — workflow foundation
- ✅ Campaign / stage / batch / molecule state in DB (Alembic 0002)
- ✅ Bounded concurrency, timeouts, cancel, automatic bounded retry, selective retry, resume
- ✅ Duplicate-execution prevention (conditional claim), orphan-process cleanup, real `kill -9` recovery test
- ✅ Structured logging with job/stage context; per-batch tool logs as artifacts

## Phase 3 — scientific integrations
- ✅ Vina · ✅ HYDE · ✅ RDKit SA · ✅ ADMET-AI (+ calibrated alert rule)
- ✅ FlexX adapter (real binary; parser corrected from real output) · ✅ ZINC20 retrieval
- ✅ BioSolveIT HPSee replaces local FlexX: remote FlexX docking + HYDE scoring (real campaign evidenced)
- 🟡 HPSee Chemical Space Docking with anchor review: built and tested against a fake HPSee; ⛔ real run needs SeeSAR `.flexx`/`.hydescorer` + a `.space`
- ✅ FastGrow analogue generation (optional stage) · ➖ Koffee affinity / kinetics: placeholders, deferred by decision

## Phase 4 — UI
- ✅ Guided site selection, engine choice, library, filters, ranking choice, honest time estimate
- ✅ Live per-step and per-batch progress, failure reasons + tool logs, retry/resume/cancel
- ✅ Ranked candidates, removed/failed molecules with reasons, 2D + 3D pose, provenance, SDF/CSV downloads
- ✅ Fully offline front end (compiled Tailwind, self-hosted fonts and 3Dmol)

## Phase 5/6 — testing and acceptance
- ✅ 121 automated tests + 5 real-tool integration tests (Vina, HYDE, FlexX, ADMET, licence) (workflow with fakes; units; `-m real` integration against real tools)
- ✅ Benchmarks (ACCEPTANCE_REPORT §4) · 🟡 Docker image build (see report)
- ➖ Koffee

## Phase 5+ — environment
- ✅ `scripts/install_tools.sh` (tools + licence + pinned Vina, vendor diagnostics), default discovery paths
- ✅ Compose: base + prod / vps / dev (+ gpu) overrides; PostgreSQL path verified in containers
- ⛔ GPU: NVIDIA driver needs your sudo + reboot + MOK enrollment (`docs/GPU.md`); only ADMET-AI could use it

## Phase 7 — server day
- ✅ Runbook `docs/DEPLOYMENT.md` · ⛔ everything marked [SERVER ONLY] there
