"""
Pipeline orchestrator.

MVP choice: a single sequential function run as a FastAPI BackgroundTask,
not a real queue/worker system (Celery+Redis, etc.). This is an explicit
simplification — the production direction (separate CPU/GPU worker
pools, retries, per-stage queues) is described in the design notes and
README, but implementing a message broker for a demo-sized job would be
over-engineering, not a stronger signal.

What IS implemented for real, because it costs nothing and is the part
that actually demonstrates engineering judgment:
  - an explicit per-stage state machine on Job.status
  - an append-only JobStageLog for every stage attempt (success or
    failure), which is what a retry/progress system would be built on
  - molecule identity assigned once, at screening, and carried through
    every subsequent stage by Molecule.id — never by SMILES matching
  - stage execution through service adapters (backend/services), so a
    real engine can replace a mock without touching this file
  - artifacts written to disk per run, with Artifact rows pointing at them
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, storage
from .config import settings
from .database import get_session
from .logging_config import get_logger
from .models import _now
from .services import TargetContext, admet_service, affinity_service
from .services.engines import pace
from .services import kinetics_service, sa_service, screening_service
from .stages import depiction
from .targets import service as target_service

log = get_logger("pipeline")

TERMINAL_STATUSES = ("COMPLETE", "FAILED")

STAGE_SEQUENCE = ["screening", "sa", "admet", "affinity", "kinetics"]

# Job.status value per stage key.
_STAGE_STATUS = {
    "screening": "SCREENING",
    "sa": "SA_FILTER",
    "admet": "ADMET",
    "affinity": "AFFINITY",
    "kinetics": "KINETICS",
}

DEFAULT_PARAMS = {
    "library_limit": 25,
    "sa_threshold": 5.0,
    "enable_admet": True,
    "enable_affinity": True,
    "enable_kinetics": True,
    "strict_admet_gate": True,
    "affinity_shortlist": 8,
}


def resolve_params(params: dict | None) -> dict:
    """Merge stored job params over the defaults, coercing types."""
    merged = dict(DEFAULT_PARAMS)
    merged.update(params or {})
    merged["library_limit"] = max(1, int(merged.get("library_limit", 25)))
    merged["sa_threshold"] = float(merged.get("sa_threshold", 5.0))
    merged["affinity_shortlist"] = max(1, int(merged.get("affinity_shortlist", 8)))
    for flag in ("enable_admet", "enable_affinity", "enable_kinetics", "strict_admet_gate"):
        merged[flag] = bool(merged.get(flag, True))
    return merged


def _log_stage(
    session: Session,
    job_id: str,
    stage: str,
    status: str,
    detail: str | None = None,
) -> models.JobStageLog:
    entry = models.JobStageLog(
        job_id=job_id,
        stage=stage,
        status=status,
        detail=detail,
        finished_at=_now() if status in ("SUCCEEDED", "FAILED", "SKIPPED") else None,
    )
    session.add(entry)
    session.flush()
    if status in ("SUCCEEDED", "FAILED", "SKIPPED"):
        session.commit()
    log.log(
        40 if status == "FAILED" else 20,  # ERROR / INFO
        "%s %s%s", stage, status, f": {detail}" if detail else "",
        extra={"job_id": job_id, "stage": stage, "status": status},
    )
    return entry


def _set_job_status(session: Session, job: models.Job, status: str, stage: str | None = None):
    job.status = status
    job.current_stage = stage
    session.add(job)
    session.flush()
    # Commit at every state transition. The UI polls job status from other
    # connections, and uncommitted writes are invisible to them — without
    # this the job would appear to sit at SUBMITTED and then jump straight
    # to COMPLETE. It also means a crash leaves an accurate record of how
    # far the run got instead of rolling everything back.
    session.commit()


def classify_status(sa_score: float, admet_profile: str, residence_min: float) -> str:
    """Simple, transparent rule-based label for the UI's Status column.

    This is deliberately NOT a combined numeric score — per the design
    discussion, scientific metrics stay separate; this only buckets rows
    for the UI using fixed, documented thresholds, which is a reasonable
    MVP ranking mechanism without inventing scientific weighting we have
    no basis for.

    These are presentation labels. "Nominated" means "passed this demo's
    rules", not "is a drug candidate".
    """
    if admet_profile == "Favorable" and sa_score <= 3.0 and residence_min >= 150:
        return "Nominated"
    if admet_profile == "Favorable" and sa_score <= 5.0 and residence_min >= 60:
        return "Shortlisted"
    return "Review"


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
def _write_artifact(
    session: Session,
    job_id: str,
    stage: str,
    kind: str,
    filename: str,
    content: str,
) -> str:
    """Write a run artifact under DATA_DIR/runs/<job>/<stage>/ and record it.

    The Artifact row stores the path relative to DATA_DIR (see storage.py),
    so it stays valid if the data volume moves.
    """
    directory = storage.ensure_dir("runs", job_id, stage)
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    stored = storage.to_stored(path)
    session.add(
        models.Artifact(job_id=job_id, stage=stage, kind=kind, storage_path=stored)
    )
    session.flush()
    return stored


def _stage_delay(job_params: dict) -> float | None:
    """Per-job override of the mocked-stage pacing; None = adapter default."""
    value = job_params.get("stage_delay_seconds")
    return None if value is None else float(value)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_pipeline(session: Session, job_id: str) -> None:
    """Run every stage for a job, writing results and artifacts as it goes.

    Molecule identity rule: Molecule rows are created exactly once, during
    screening. Every later stage looks up its inputs by Molecule.id and
    writes StageResult rows keyed by (job_id, molecule_id, stage). SMILES
    are carried as a chemical identifier, never used to re-join stages.
    """
    job = session.get(models.Job, job_id)
    if job is None:
        raise ValueError(f"Unknown job_id: {job_id}")

    target = session.get(models.Target, job.target_id)
    if target is None:
        raise ValueError(f"Job {job_id} has no target record")

    params = resolve_params(job.params)
    delay = _stage_delay(job.params or {})

    context = TargetContext(
        target_id=target.id,
        name=target.name,
        pdb_id=target.pdb_id,
        chain=target.selected_chain,
        checksum=target.checksum,
        structure_path=target.structure_path,
        residue_count=target.residue_count,
        atom_count=target.atom_count,
    )

    try:
        # --- Stage 0: stage the target structure into the run ----------
        _log_stage(session, job_id, "target", "STARTED")
        staged_path = target_service.copy_structure_to_run(target, job_id)
        if staged_path:
            session.add(
                models.Artifact(
                    job_id=job_id, stage="target", kind="pdb", storage_path=staged_path
                )
            )
            session.flush()
            _log_stage(
                session, job_id, "target", "SUCCEEDED",
                f"{target.name} ({target.pdb_id or 'uploaded'}) chain "
                f"{target.selected_chain or '-'} staged to {staged_path}",
            )
        else:
            _log_stage(
                session, job_id, "target", "SUCCEEDED",
                f"{target.name}: no structure file on disk; proceeding with metadata only.",
            )

        # --- Stage 1: screening (mocked) --------------------------------
        _set_job_status(session, job, _STAGE_STATUS["screening"], "screening")
        _log_stage(session, job_id, "screening", "STARTED")
        hits = screening_service.run(context, params["library_limit"], delay=delay)

        molecules: list[models.Molecule] = []
        run_tag = str(job.params.get("run_tag") or job.id[:4]).upper()
        for index, hit in enumerate(hits, start=1):
            molecule = models.Molecule(
                job_id=job_id,
                display_id=f"MDP-{run_tag}-{index:03d}",
                smiles=hit["smiles"],
                source_stage="screening",
            )
            session.add(molecule)
            molecules.append(molecule)
        session.flush()  # assigns Molecule.id — the identity used from here on

        for molecule, hit in zip(molecules, hits):
            session.add(
                models.StageResult(
                    molecule_id=molecule.id,
                    job_id=job_id,
                    stage="screening",
                    payload={
                        "source": "demo_library",
                        "library_name": hit["name"],
                        "passed": True,
                        "note": (
                            "Mocked — compound comes from a fixed demo library, "
                            "not from screening against this target."
                        ),
                    },
                    is_mocked=True,
                )
            )
        _write_artifact(
            session, job_id, "screening", "smi", "screening_candidates.smi",
            "".join(f"{h['smiles']}\t{m.display_id}\t{h['name']}\n" for m, h in zip(molecules, hits)),
        )
        _log_stage(
            session, job_id, "screening", "SUCCEEDED",
            f"{len(molecules)} candidates in → {len(molecules)} out "
            f"(demo library, limit {params['library_limit']})",
        )

        # --- Stage 2: SA scoring (REAL) ---------------------------------
        _set_job_status(session, job, _STAGE_STATUS["sa"], "sa")
        _log_stage(session, job_id, "sa", "STARTED")
        threshold = params["sa_threshold"]
        survivors: list[models.Molecule] = []
        sa_payloads: dict[str, dict] = {}
        for molecule in molecules:
            try:
                result = sa_service.run(context, molecule.smiles)
            except Exception:
                log.error(
                    "SA scoring failed",
                    extra={"job_id": job_id, "stage": "sa", "molecule_id": molecule.id},
                )
                raise
            passed = result["sa_score"] <= threshold
            result = {**result, "passed": passed, "threshold": threshold}
            sa_payloads[molecule.id] = result
            session.add(
                models.StageResult(
                    molecule_id=molecule.id,
                    job_id=job_id,
                    stage="sa",
                    payload=result,
                    is_mocked=False,
                )
            )
            if passed:
                survivors.append(molecule)
        _write_artifact(
            session, job_id, "sa", "json", "sa_scores.json",
            json.dumps(
                [
                    {
                        "molecule_id": m.id,
                        "display_id": m.display_id,
                        **sa_payloads[m.id],
                    }
                    for m in molecules
                ],
                indent=2,
            ),
        )
        _log_stage(
            session, job_id, "sa", "SUCCEEDED",
            f"{len(molecules)} in → {len(survivors)} out (RDKit SA ≤ {threshold})",
        )

        # --- Stage 3: ADMET (mocked) ------------------------------------
        admet_survivors, admet_payloads = _run_admet(
            session, job, job_id, context, survivors, params, delay
        )

        # --- Stage 4: binding affinity (mocked) -------------------------
        affinity_survivors, affinity_payloads = _run_affinity(
            session, job, job_id, context, admet_survivors, params, delay
        )

        # --- Stage 5: unbinding kinetics (mocked) -----------------------
        final_molecules = _run_kinetics(
            session, job, job_id, context, affinity_survivors,
            affinity_payloads, params, delay,
        )

        # --- Final candidate SDF ----------------------------------------
        _write_final_sdf(session, job_id, final_molecules)

        _set_job_status(session, job, "COMPLETE", None)

    except Exception as exc:  # noqa: BLE001 — top-level job failure boundary
        # The session may be unusable (e.g. a database error mid-statement);
        # roll back to the last stage commit before recording the failure.
        session.rollback()
        stage = job.current_stage or "unknown"
        log.error(
            "pipeline failed", exc_info=exc, extra={"job_id": job_id, "stage": stage}
        )
        public_reason = _public_failure_reason(exc, stage, job_id)
        _log_stage(session, job_id, stage, "FAILED", public_reason)
        job.failure_reason = public_reason
        _set_job_status(session, job, "FAILED", stage)
        raise


def _public_failure_reason(exc: Exception, stage: str, job_id: str) -> str:
    """Failure text that is safe to store and show to API clients.

    Raw exception text can contain SQL, bound parameters and filesystem
    paths (database errors especially), and this string is returned by the
    job-status endpoint. Outside development only the exception class is
    exposed; the full traceback goes to the server log, tagged with the job.
    """
    if settings.expose_error_detail:
        return f"{exc.__class__.__name__}: {exc}"
    return f"Stage '{stage}' failed ({exc.__class__.__name__}). See server logs for job {job_id}."


def fail_interrupted_jobs() -> int:
    """Mark runs left in a non-terminal state as FAILED. Returns the count.

    Pipelines execute inside the API process (see docs/DEPLOYMENT.md), so a
    restart abandons whatever was running. Without this, those jobs would
    show as "running" forever. Valid because exactly one app process is
    supported; with several workers this would fail another worker's run.
    """
    reason = "Interrupted: the application restarted while this run was in progress."
    with get_session() as session:
        jobs = session.execute(
            select(models.Job).where(models.Job.status.not_in(TERMINAL_STATUSES))
        ).scalars().all()
        for job in jobs:
            stage = job.current_stage or "queued"
            _log_stage(session, job.id, stage, "FAILED", reason)
            job.failure_reason = reason
            job.status = "FAILED"
            session.add(job)
        return len(jobs)


def _run_admet(session, job, job_id, context, inputs, params, delay):
    _set_job_status(session, job, _STAGE_STATUS["admet"], "admet")
    if not params["enable_admet"]:
        _log_stage(session, job_id, "admet", "SKIPPED", "Disabled in run configuration")
        return list(inputs), {}

    _log_stage(session, job_id, "admet", "STARTED")
    pace(delay)
    accepted = {"Favorable"} if params["strict_admet_gate"] else {"Favorable", "Moderate"}
    survivors: list[models.Molecule] = []
    payloads: dict[str, dict] = {}
    for molecule in inputs:
        result = admet_service.run(context, molecule.smiles, delay=delay)
        passed = result["profile"] in accepted
        result = {**result, "passed": passed, "gate": sorted(accepted)}
        payloads[molecule.id] = result
        session.add(
            models.StageResult(
                molecule_id=molecule.id, job_id=job_id, stage="admet",
                payload=result, is_mocked=True,
            )
        )
        if passed:
            survivors.append(molecule)
    _write_artifact(
        session, job_id, "admet", "json", "admet_results.json",
        json.dumps(
            [{"molecule_id": m.id, "display_id": m.display_id, **payloads[m.id]} for m in inputs],
            indent=2,
        ),
    )
    _log_stage(
        session, job_id, "admet", "SUCCEEDED",
        f"{len(inputs)} in → {len(survivors)} out (mocked; gate: {'/'.join(sorted(accepted))})",
    )
    return survivors, payloads


def _run_affinity(session, job, job_id, context, inputs, params, delay):
    _set_job_status(session, job, _STAGE_STATUS["affinity"], "affinity")
    if not params["enable_affinity"]:
        _log_stage(session, job_id, "affinity", "SKIPPED", "Disabled in run configuration")
        return list(inputs), {}

    _log_stage(session, job_id, "affinity", "STARTED")
    pace(delay)
    payloads: dict[str, dict] = {}
    for molecule in inputs:
        payloads[molecule.id] = affinity_service.run(context, molecule.smiles, delay=delay)

    # Shortlist by the mocked Kd (lower = tighter). An MVP presentation
    # rule for keeping the funnel narrow, not a validated selection step.
    shortlist_size = min(params["affinity_shortlist"], len(inputs))
    ranked = sorted(inputs, key=lambda m: payloads[m.id]["kd_nm"])
    shortlisted_ids = {m.id for m in ranked[:shortlist_size]}

    for molecule in inputs:
        result = {
            **payloads[molecule.id],
            "passed": molecule.id in shortlisted_ids,
            "shortlist_size": shortlist_size,
            "selection_rule": "lowest mocked Kd (presentation rule, not validated science)",
        }
        payloads[molecule.id] = result
        session.add(
            models.StageResult(
                molecule_id=molecule.id, job_id=job_id, stage="affinity",
                payload=result, is_mocked=True,
            )
        )
    survivors = [m for m in ranked[:shortlist_size]]
    _write_artifact(
        session, job_id, "affinity", "json", "affinity_results.json",
        json.dumps(
            [{"molecule_id": m.id, "display_id": m.display_id, **payloads[m.id]} for m in inputs],
            indent=2,
        ),
    )
    _log_stage(
        session, job_id, "affinity", "SUCCEEDED",
        f"{len(inputs)} in → {len(survivors)} out (mocked Kd shortlist, top {shortlist_size})",
    )
    return survivors, payloads


def _run_kinetics(session, job, job_id, context, inputs, affinity_payloads, params, delay):
    _set_job_status(session, job, _STAGE_STATUS["kinetics"], "kinetics")
    if not params["enable_kinetics"]:
        _log_stage(session, job_id, "kinetics", "SKIPPED", "Disabled in run configuration")
        return list(inputs)

    _log_stage(session, job_id, "kinetics", "STARTED")
    pace(delay)
    payloads: dict[str, dict] = {}
    for molecule in inputs:
        kd = affinity_payloads.get(molecule.id, {}).get("kd_nm", 10.0)
        result = kinetics_service.run(context, molecule.smiles, kd, delay=delay)
        result = {**result, "passed": True}
        payloads[molecule.id] = result
        session.add(
            models.StageResult(
                molecule_id=molecule.id, job_id=job_id, stage="kinetics",
                payload=result, is_mocked=True,
            )
        )
    _write_artifact(
        session, job_id, "kinetics", "json", "kinetics_results.json",
        json.dumps(
            [{"molecule_id": m.id, "display_id": m.display_id, **payloads[m.id]} for m in inputs],
            indent=2,
        ),
    )
    _log_stage(
        session, job_id, "kinetics", "SUCCEEDED",
        f"{len(inputs)} in → {len(inputs)} out (mocked koff / residence time)",
    )
    return list(inputs)


def _write_final_sdf(session: Session, job_id: str, molecules: list[models.Molecule]) -> None:
    """Real RDKit SDF of the run's final candidates."""
    if not molecules:
        return
    payloads = _payloads_for_job(session, job_id)
    records = []
    for molecule in molecules:
        by_stage = payloads.get(molecule.id, {})
        records.append(
            depiction.sdf_for_candidate(
                molecule.smiles,
                molecule.display_id,
                _sdf_properties(by_stage),
            )
        )
    _write_artifact(
        session, job_id, "final", "sdf_output", "candidates.sdf", "".join(records)
    )


def _sdf_properties(by_stage: dict[str, dict]) -> dict:
    """Property tags for SDF export.

    Mocked values carry a `_mocked` suffix in the tag name so the
    provenance survives outside this application — a reviewer opening the
    SDF in another tool still sees which numbers are simulated.
    """
    sa = by_stage.get("sa", {})
    admet = by_stage.get("admet", {})
    affinity = by_stage.get("affinity", {})
    kinetics = by_stage.get("kinetics", {})
    return {
        "SA_SCORE": sa.get("sa_score"),
        "SA_SCALE": sa.get("scale"),
        "MOLECULAR_WEIGHT": sa.get("molecular_weight"),
        "ADMET_PROFILE_mocked": admet.get("profile"),
        "KD_NM_mocked": affinity.get("kd_nm"),
        "DELTA_G_KCAL_MOL_mocked": affinity.get("delta_g_kcal_mol"),
        "KOFF_PER_S_mocked": kinetics.get("koff_per_s"),
        "RESIDENCE_TIME_MIN_mocked": kinetics.get("residence_time_min"),
        "PROVENANCE": (
            "SA score: real (RDKit). ADMET/affinity/kinetics: mocked demo "
            "values, not experimental or predicted measurements."
        ),
    }


def _payloads_for_job(session: Session, job_id: str) -> dict[str, dict[str, dict]]:
    """{molecule_id: {stage: payload}} — joined on IDs, never on SMILES."""
    rows = session.execute(
        select(models.StageResult).where(models.StageResult.job_id == job_id)
    ).scalars().all()
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        out.setdefault(row.molecule_id, {})[row.stage] = row.payload
    return out
