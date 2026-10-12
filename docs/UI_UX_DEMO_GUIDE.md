# Evaluator guide — what this platform does, and why you can rely on it

*For a non-technical reader. Technical detail: `docs/SCIENCE_NOTES.md`; evidence: `docs/ACCEPTANCE_REPORT.md`.*

## In one paragraph

You give the platform a protein and tell it where the "pocket" is. It fetches a few dozen to a few thousand
candidate molecules, predicts how well each one fits the pocket (**docking**), double-checks the best fits with a second,
physics-based opinion (**HYDE**), drops molecules that would be hard to make (**synthetic accessibility**) or look unsafe
for the body (**ADMET**), and gives you a ranked shortlist with 3D structures you can download. Every step runs real
scientific software, shows live progress, and survives restarts.

## The decisions that make the results trustworthy

| Decision | What you see | Why it matters |
|---|---|---|
| **Real tools only; placeholders are labelled** | Docking (AutoDock Vina, BioSolveIT FlexX), HYDE, RDKit and the ADMET-AI model are real. Binding affinity and unbinding kinetics (Koffee) are not connected: their columns say *placeholder* and never influence a result | A number you cannot trust is worse than a blank. Nothing simulated is passed off as science |
| **The docking method was validated first** | Re-docking the known drug-like ligand of the KRAS structure (7RPZ) put it back within **0.5 Å** of the experimental position (anything under 2 Å counts as success) | Shows the pipeline reproduces reality on a case where the answer is known |
| **No blended "super score"** | Docking, HYDE, synthetic accessibility and ADMET sit in separate, labelled columns; **one** named measure orders the list (default: HYDE) | Scores on different scales cannot be honestly added up. The ranking rule is printed on the page |
| **Two programs, two opinions, shown side by side** | FlexX and HYDE rank the same molecules differently (FlexX's best was HYDE's 7th in our test) | Disagreement is information. You can see it and choose the ranking measure instead of having it hidden |
| **HYDE is read the way the company reads it** | The HYDE score and its four components (water cost and interactions, protein and molecule side) follow the company's own HYDE SDF Extractor logic, checked on real output, with a kJ/mol ↔ kcal/mol switch | Your team's reference tool and this platform give the same numbers |
| **Every removal has a stated reason** | The *Removed by a rule* tab lists each molecule, the step that removed it and why ("SA score 5.8 above the limit of 5") | You can audit the funnel instead of trusting a black box |
| **A safety screen that is not trigger-happy** | A toxicity alert needs a likely prediction **and** a risk higher than 90% of approved drugs | A plain 50% cut-off flagged 93% of random molecules and over half of known medicines, so it would have rejected almost everything |
| **Existing vs. invented molecules are never mixed** | ZINC molecules are purchasable catalogue compounds. Optional FastGrow suggestions sit in their own tab, labelled *generated, not catalogue*, with a fit score that is explicitly not an affinity | Avoids over-claiming novelty or efficacy |
| **Work is never lost or repeated** | Long runs are split into batches saved as they finish. Kill the server mid-run and it resumes where it stopped; a failed batch can be inspected (reason, tool log) and retried alone | Matches how a shared server really behaves, and saves compute |
| **Reproducible sampling** | The *sampling seed* fixes which molecules are drawn, so a run can be repeated exactly | Auditable, comparable runs |
| **Offline, locked-down by design** | The interface needs no internet; vendor licences are read from a protected folder, never stored in the code or in images; tools run without a shell and with a minimal environment | Safe to host on the company server |
| **Plain-language honesty about limits** | Each screen explains terms and states uncertainty (e.g. HYDE shows an affinity *range*) | Results read as computational predictions to prioritise lab work, not proof that a molecule works |

## Where the molecules come from (and what "seed" means)

| Source | What it is | Use it for |
|---|---|---|
| **ZINC20 catalogue** | A public database of purchasable compounds. About 10 million molecules per downloaded file, 20 files | The realistic case: hits can be ordered and tested |
| **Built-in known drugs** | About 40 approved drugs and scaffolds shipped with the app | Offline demos and sanity checks |
| **My own list** | Molecules you paste (SMILES text) | An in-house or vendor library |

* **When are ZINC molecules downloaded?** At the start of a run, not in advance and not into the database. The platform
  fetches the first 3 MB of one ZINC file (roughly 280,000–400,000 molecules), keeps that file on disk as a cache, and
  reuses it later. Only the molecules of a campaign are stored in the database. The setup page shows what is already cached.
* **How many are available?** Each cached chunk gives about 300,000–400,000 molecules; this server has 2–3 chunks cached
  (about 0.7–1.0 million). All 20 chunks at the default size is roughly 7 million.
* **How to download more:** `python scripts/prefetch_zinc.py` (all 20 chunks) or `--mb 20 --chunks 1,2,3` for a deeper
  slice of each (about 2.7 million molecules per 20 MB). `ZINC_PREFIX_MB` sets the default depth.
* **The sample is convenient, not statistically random across all of ZINC:** the files are ordered by identifier, so the
  start of a file is a reproducible but not uniformly random slice.
* **Seed:** the molecules are a sample picked by a pseudo-random draw; the seed is the number that starts the draw.
  Same seed → same molecules; different seed → a different sample. It does not change how anything is scored.

## What is deliberately not claimed

Koffee binding affinity and unbinding kinetics are not integrated (placeholders). FlexX and HYDE need a valid BioSolveIT
licence (the development one expires 21 October 2026). Docking scores are predictions: Vina in particular
over-estimates for large molecules. FastGrow suggestions are unsynthesised ideas. The NVIDIA GPU is not used (only the
ADMET model could, and it is already fast on the CPU).

## Five-minute walkthrough

1. **New Discovery** → *Use demo target* (KRAS G12D, PDB 7RPZ) → **Validation** shows what the file was checked for.
2. **Set up the search**: pocket = the pre-selected ligand; pick *AutoDock Vina*, 20 molecules from ZINC; leave filters on;
   optionally switch on *Suggest new analogues*. Press **Start campaign**.
3. Watch **Progress by step**: each step shows tool, counts in → out and batches filling in live.
4. Open **Results**: ranked candidates with structures and separate scores; click a row for the 3D pose in the pocket,
   the HYDE breakdown, ADMET groups and how the molecule got there. Check *Removed by a rule* for reasons.
5. Download the **SDF/CSV**. Optional: stop the server during a run and restart it to see it resume.
