# Local acceptance report

Date of runs: 2026-10-10 · Machine: Fedora, 16 threads, 15 GB RAM, RTX 4050 (no NVIDIA driver installed; unused).
Tools: AutoDock Vina 1.2.7, BioSolveIT FlexX 6.5.3, HYDE 2.5.3 and FastGrow 1.6.3 (licence valid to 2026-10-21), RDKit 2026.03.6, ADMET-AI 2.0.1.

## 1. Verified with genuine scientific execution

| Item | Evidence |
|---|---|
| Vina docking pipeline (receptor prep → ligand prep → dock → pose) | 7RPZ ligand 6IC re-docked: best pose **0.51 Å RMSD** to the crystal pose (`tests/integration`, `-m real`) |
| HYDE on real poses | scored the 7RPZ crystal complex (cumulative < −50 kJ/mol, upper affinity bound < 10 nM, no clashes) and every Vina pose of three campaigns; cumulative score cross-checked against the vendor's affinity range |
| Licence diagnostics | `hydescorer --license-info` parsed: "Valid key, expires 21 October 2026"; same inside the hardened Docker container |
| ZINC20 retrieval | 150-molecule samples streamed and standardised (seeded, cached) |
| SA scoring, ADMET-AI | run inside every campaign; ADMET sanity test (aspirin well absorbed, caffeine crosses the BBB) |
| Full campaigns | (a) 16 molecules → 7 ranked candidates, 95 s; (b) 24 molecules, killed with `kill -9` mid-docking and resumed; (c) 60 molecules, section 4 |
| **FlexX docking** | Real FlexX 6.5.3 campaign: 12 ZINC molecules → FlexX (2 min 33 s) → 9 kept → HYDE → SA → ADMET → 8 ranked candidates; `tests/integration` FlexX test passes. First real run exposed a wrong assumption in the fixture-built parser (poses are named `<name>_<n>`); fixed. FlexX and HYDE rank differently (shown side by side, ranked by HYDE) |
| **FastGrow** | Campaign with analogue generation: 16 molecules → 8 candidates → 3 parents → **86 generated analogues** (1,184-fragment DB built by FastGrowDBCreator), all valid 3D SDF records; `tests/integration` FastGrow test and `scripts/verify_tools.py` pass (natively and in the container). Resume skipped completed stages |
| Persistence / recovery | DB-backed state; real SIGKILL: finished batches 1–2 not repeated, interrupted 3–4 re-run (attempt 2), orphan tool processes removed, campaign COMPLETE |
| Natural molecule-level failure | MDP-F5F7-0027: "3D conformer generation failed" → recorded, campaign continued, shown under *Could not be processed* |
| SDF output | opens in RDKit, 3D poses (HYDE-optimised pose preferred), separate labelled tags, placeholders suffixed `_MOCKED` |
| Container | image builds; non-root, read-only filesystem, `cap_drop ALL`; Vina, ADMET-AI and HYDE (with licence mounted) work inside it |

## 2. Tested only with fixtures or fakes

* FlexX output parser unit test (synthetic scores, record naming taken from the real run); the real binary is covered in section 1.
* Workflow behaviour (batching, retries, cancel, timeout, restart simulation, claim races, concurrency bound):
  12 tests with scripted fake executors on the real runner/DB/API. They prove orchestration, not science.

## 3. Blocked — not validated

| Item | Why | What unblocks it |
|---|---|---|
| **Koffee** (affinity, kinetics) | Deferred by decision. Stages stay **placeholders**, labelled everywhere, excluded from filtering/ranking. | Koffee package/API + docs |
| `HYDE_SCORER.html` (company extractor) | **Used**: component definitions ported; totals verified against the extractor's own JavaScript on real hydescorer output | — |
| FastGrow analogue quality | Integration works; scientific value not assessed (generated molecules unsynthesised, novelty unchecked; BioSolveIT's 120k library download is login-gated, so a self-derived fragment DB is used) | Download the 120k library and set `FASTGROW_DB` |
| **Whole pipeline** | Not fully validated: Koffee affinity and kinetics (2 of 6 requested stages) have not run; docking (Vina + FlexX), HYDE, SA and ADMET have | — |
| Licence validity | Dev licence expires **2026-10-21** | New licence for submission day |

## 4. Benchmark (60 ZINC molecules, Vina exhaustiveness 8, batch 10, 2 parallel batches × 4 threads)

| Stage | Wall time | In → out |
|---|---|---|
| Prepare + library | 4 s | 60 → 60 |
| Docking (6 batches) | **8 min 16 s** (≈ 8.3 s/molecule wall; ≈ 33 CPU-s/molecule) | 60 → 30 (1 failed: conformer generation; 29 cut by the "best 50%" rule) |
| HYDE | 12 s | 30 → 30 |
| SA + ADMET + placeholders + ranking | 3 s | 30 → 24 |
| **Total** | **8 min 35 s** | **24 candidates** |

(Vina benchmark.) Machine CPU averaged 42% (max 57%) during the run (other desktop applications included); system RAM used 9.8–11.0 GB of 15.7 GB
(≈ 1 GB above idle). Disk: 2 MB of artifacts per 60-molecule campaign. Extrapolation (not measured): 1,000 molecules ≈ 2.3 h at
this effort; Vina dominates, so use "Quick" effort or a larger `CPU_BUDGET` on the server. No tool uses the GPU; the NVIDIA driver is not installed on this machine (`docs/GPU.md`).

## 5. Ready for the server

Runbook `docs/DEPLOYMENT.md` (prerequisites, tool and licence install, compose mounts incl. SELinux `:z`, health checks,
validation campaigns, backup, rollback). Known server-day actions: run the `real` tests, run the two
acceptance campaigns, replace the licence. Known gaps: no authentication on the API (put it behind SSO/proxy); one campaign
at a time by design; the 3D viewer needs the browser to run WebGL; sampling of ZINC is reproducible but not uniform.
