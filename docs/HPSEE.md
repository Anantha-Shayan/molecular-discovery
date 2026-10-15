# BioSolveIT HPSee integration

HPSee is BioSolveIT's compute server (REST API + workers + its own PostgreSQL, run with Docker). This
platform uses it as its BioSolveIT docking engine. It **replaces the local FlexX workflow** for new
campaigns. AutoDock Vina is unchanged and remains the other choice.

| Engine choice | What runs where | Needs |
|---|---|---|
| AutoDock Vina | Vina + optional local HYDE on this server (unchanged) | local tools |
| HPSee, *dock a list of molecules* | ZINC / built-in / uploaded library → **FlexX on HPSee** → **HYDE on HPSee** → local SA, ADMET | HPSee `Docking` (+ `Scoring`) licence |
| HPSee, *Chemical Space Docking* | **Anchoring on HPSee** → **your review** → **Extension on HPSee** (1–3 rounds) → local SA, ADMET | HPSee `ChemicalSpaceDocking*` licence, a `.space` file on HPSee, two SeeSAR files |

A server running HPSee needs no local FlexX/HYDE installation. Campaigns created earlier with local FlexX
still open, and can be resumed if FlexX is installed (`backend/science/engines/flexx.py`, legacy).

## Configure

```
HPSEE_URL=http://hpsee-host:8080        # base URL, no /api suffix
HPSEE_USERNAME=mdp-service              # a dedicated HPSee user
HPSEE_PASSWORD=...
```

* Create a dedicated **non-admin** user in the HPSee admin dashboard (*Users* page). Then **delete or
  re-password the default `admin`/`admin` account**: HPSee ships with it and it is valid until changed.
* HPSee on the same machine as the app's Docker stack: `HPSEE_URL=http://host.docker.internal:8080`. The
  base compose file maps `host.docker.internal` to the host.
* HTTPS with an internal self-signed certificate: `HPSEE_VERIFY_TLS=false` (only then).
* Other knobs (defaults in `.env.example`): poll interval, workflow deadline (72 h), result entries
  downloaded per result library (5000), definition-file upload limit.

Leaving `HPSEE_URL` empty is safe. The UI then shows HPSee as **not configured** and offers Vina only.
Nothing is faked.

**Licence.** HPSee has its own licence store. Upload the `.lic` on the *Licenses* page of the HPSee
dashboard, or via `POST /api/v1/Licenses`. `/api/hpsee/status` and the campaign setup page show HPSee's
own per-workflow answer (`GET /api/v1/Licenses`). Campaign creation is refused for anything HPSee does not
report as licensed. The licence installed on 2026-10-15 (`BioSolveIT_TotM_25thAnniversary_SeeSAR.lic`)
covers every HPSee workflow and **expires 2026-11-15**.

## Check it

```bash
curl -s localhost:8008/api/hpsee/status | python3 -m json.tool       # configured/reachable/authenticated/licences
python scripts/verify_tools.py                                        # "HPSee docking + HYDE": a real 2-molecule run
pytest -m real tests/integration/test_real_hpsee.py -v                     # the same, plus result-format assertions
```

## Chemical Space Docking, step by step

1. **An HPSee admin uploads the chemical space** (`.space`) on HPSee's *Spaces* page. HPSee prepares it,
   which needs the `ChemicalSpacePrep` licence. Only spaces HPSee reports as prepared can be chosen.
2. **SeeSAR definition files.** In SeeSAR's Docking mode, load the same protein, define the pocket (and
   any pharmacophore constraints), and export the **docking definition (`.flexx`)** and the
   **scoring definition (`.hydescorer`)**. HPSee's anchoring/extension API requires both. They also carry
   the protein: HPSee docks into the protein inside these files, not the structure chosen in this app (that
   one is used for display and analogue growing). The format is BioSolveIT's and undocumented. This
   platform stores and forwards the files byte for byte and never generates or edits them.
3. **Set up** (*Configure & Review*): choose *BioSolveIT HPSee* → *Chemical Space Docking*, the space,
   upload both files. Optional anchoring/extension filters are passed to HPSee verbatim (HPSee syntax, e.g.
   `Properties.BIOSOLVEIT.NUMBER_OF_HEAVY_ATOMS <= 20`; the property list comes from HPSee itself).
4. **Anchoring** runs on HPSee. The campaign page shows HPSee's own per-task status. The best entries
   (server-side sort by `BIOSOLVEIT.DOCKING_SCORE`) are downloaded, then ranked by HYDE's estimated
   affinity.
5. **Review (the campaign pauses: `AWAITING_REVIEW`).** Real anchors with 2D structures, open attachment
   points (`BIOSOLVEIT.NUMBER_OF_LINK_ATOMS`; drawn as U/Np/Pu/Am), docking score, HYDE range, ligand
   efficiency, clash, and a downloadable pose. Select up to 500 (HPSee's limit), then **confirm explicitly**.
   The API rejects unconfirmed requests, ids that HPSee did not return, and complete molecules.
6. **Extension** runs on HPSee with exactly the confirmed ids. If results still carry open attachment points
   (three-component spaces), the campaign pauses again for the next round (at most 3), or *Finish* takes
   the complete molecules found so far.
7. **Products** (complete, readable, de-duplicated by InChIKey; best pose kept) up to *Best products to
   evaluate* enter the normal funnel: SA → ADMET → ranking → optional FastGrow. They are labelled as
   members of the chemical space (make-on-demand), not catalogue stock.

## Reliability

* Every workflow is recorded in `hpsee_runs` **before** it is submitted. Its HPSee description carries
  `[mdp:<run id>]`. After a crash between submit and save, the workflow is found again by that marker
  (C-S-D also via HPSee's own `anchoringWorkflow`/`extensionWorkflows`), so it is never submitted twice.
* App restart: running workflows are re-attached and polled, not resubmitted. HPSee keeps working
  while the app is down.
* **Cancel** deletes the HPSee workflow. HPSee drops queued jobs, but **jobs already on a worker run to
  completion**: HPSee has no hard kill. The UI says so.
* **Failures.** HPSee's error list (category + message) is stored and shown. On *Resume*, a failed C-S-D
  workflow is **requeued on HPSee** (`Errors/Retry`), with no duplicate work. Licence, input and access
  errors are marked not retryable. A failed library batch is retried like any batch (new workflow).
* HPSee unreachable while polling: tolerated for 15 minutes, then the step fails with the reason.
* Deadline: `HPSEE_WORKFLOW_TIMEOUT_SECONDS` from the stored submission time. On expiry the workflow is
  cancelled on HPSee.

## What was verified, and what was not

Verified against a licensed HPSee 2.4.0 on 2026-10-15 (real records kept in `tests/fixtures/hpsee_*.json`):
login, licences, multipart submission, docking (tasks *Docking* + *Annotation*, 10 poses per molecule
titled `<name>_NN`, `BIOSOLVEIT.DOCKING_SCORE`, server-side sorting), HYDE scoring, annotation tags
(`USMILES`, `NUMBER_OF_LINK_ATOMS`), and a full campaign (8 molecules → HPSee docking ~80 s → HPSee HYDE
~30 s → SA → ADMET → 3 ranked candidates).

Two findings from real output that shape the design:
* **Progress is per task and coarse.** HPSee reported 0 % until a task finished, then 100 %. The UI
  shows that figure as is and says so.
* **HPSee's HYDE scoring writes no per-atom scores** (`BIOSOLVEIT.HYDE_ATOM_SCORES`). The "cumulative HYDE
  score" (the company extractor's per-atom total) is therefore unavailable for HPSee-scored poses.
  HPSee campaigns rank by **HYDE's estimated affinity, lower bound** (stated in the ranking policy).
  The integration test fails if HPSee ever starts writing atom scores.

**Not verified: Chemical Space Docking output.** No `.space` file or SeeSAR-exported definition files
were available, so anchoring/extension never ran for real. The flow is covered by tests against a fake
HPSee that serves records in the verified format (`tests/test_hpsee.py`), and the request shapes follow
HPSee's OpenAPI document. Before relying on it, run one small C-S-D campaign on a real space and check:
anchors show open attachment points, products show none, and scores/HYDE ranges are populated.

## API (this platform)

`GET /api/hpsee/status` · `GET /api/hpsee/spaces` · `GET /api/hpsee/filter-properties` ·
`POST /api/hpsee/definitions` (multipart `kind`=`flexx`|`hydescorer`, `file`) ·
`GET /api/jobs/{id}/hpsee` (workflows + pending review) ·
`GET /api/jobs/{id}/hpsee/runs/{run}/entries[?page&size]` · `GET …/entries/{entry_id}/sdf` ·
`POST /api/jobs/{id}/hpsee/review` `{run_id, action: "extend"|"finish", entry_ids, confirm: true}`.
Campaigns: `POST /api/jobs` with `engine: "hpsee"`, `hpsee_mode`, and for C-S-D `hpsee_space_id`,
`hpsee_docking_definition`, `hpsee_scoring_definition`, optional `hpsee_anchor_filters`,
`hpsee_extension_filters`, `hpsee_max_products`.
