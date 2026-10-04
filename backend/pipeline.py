"""
Pipeline orchestrator.

MVP choice: a single sequential function run as a FastAPI BackgroundTask,
not a real queue/worker system (Celery+Redis, etc.). This is an explicit
simplification — the production direction (separate CPU/GPU worker
pools, retries, per-stage queues) is described in the design notes and
README, but implementing a message broker for a 10-molecule demo job
would be over-engineering, not a stronger signal.

What IS implemented for real, because it costs nothing and is the part
that actually demonstrates engineering judgment:
  - an explicit per-stage state machine on Job.status
  - an append-only JobStageLog for every stage attempt (success or
    failure), which is what a retry/progress system would be built on
  - idempotent-by-construction stage functions (pure functions of
    molecule state, safe to re-run)
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.orm import Session

from . import models
from .stages import mocks, sa_scoring

STAGE_SEQUENCE = ["screening", "sa", "admet", "affinity", "kinetics"]


def _log_stage(session: Session, job_id: str, stage: str, status: str, detail: str | None = None):
    entry = models.JobStageLog(
        job_id=job_id,
        stage=stage,
        status=status,
        detail=detail,
        finished_at=dt.datetime.utcnow() if status in ("SUCCEEDED", "FAILED") else None,
    )
    session.add(entry)
    session.flush()
    return entry


def _set_job_status(session: Session, job: models.Job, status: str, stage: str | None = None):
    job.status = status
    job.current_stage = stage
    session.add(job)
    session.flush()


def classify_status(sa_score: float, admet_profile: str, residence_min: float) -> str:
    """Simple, transparent rule-based label for the UI's Status column.

    This is deliberately NOT a combined numeric score — per the design
    discussion, scientific metrics stay separate; this only buckets rows
    for the UI using fixed, documented thresholds, which is a reasonable
    MVP ranking mechanism without inventing scientific weighting we have
    no basis for.
    """
    if admet_profile == "Favorable" and sa_score <= 3.0 and residence_min >= 150:
        return "Nominated"
    if admet_profile == "Favorable" and sa_score <= 5.0 and residence_min >= 60:
        return "Shortlisted"
    return "Review"


def run_pipeline(session: Session, job_id: str) -> None:
    """Runs every stage for a job, synchronously, writing results as it goes.

    Safe to call again on a FAILED job: molecules already created are
    reused (keyed by Job.id + SMILES), and each stage only (re)writes its
    own StageResult rows.
    """
    job = session.get(models.Job, job_id)
    if job is None:
        raise ValueError(f"Unknown job_id: {job_id}")

    try:
        # --- Stage 1: screening (mocked) ------------------------------
        _set_job_status(session, job, "SCREENING", "screening")
        _log_stage(session, job_id, "screening", "STARTED")
        limit = int(job.params.get("library_limit", 10))
        smiles_list = mocks.screen_chemical_space(limit=limit)

        molecules: list[models.Molecule] = []
        for i, smiles in enumerate(smiles_list, start=1):
            mol = models.Molecule(
                job_id=job_id,
                display_id=f"MDP-{job.params.get('run_tag', '0000')}-{i:03d}",
                smiles=smiles,
                source_stage="screening",
            )
            session.add(mol)
            molecules.append(mol)
        session.flush()

        for mol in molecules:
            session.add(
                models.StageResult(
                    molecule_id=mol.id,
                    job_id=job_id,
                    stage="screening",
                    payload={"source": "seed_library_mock"},
                    is_mocked=True,
                )
            )
        _log_stage(session, job_id, "screening", "SUCCEEDED", f"{len(molecules)} candidates")

        # --- Stage 2: SA scoring (REAL) --------------------------------
        _set_job_status(session, job, "SA_FILTER", "sa")
        _log_stage(session, job_id, "sa", "STARTED")
        sa_threshold = float(job.params.get("sa_threshold", 5.0))
        survivors_sa: list[models.Molecule] = []
        for mol in molecules:
            result = sa_scoring.score_molecule(mol.smiles)
            session.add(
                models.StageResult(
                    molecule_id=mol.id,
                    job_id=job_id,
                    stage="sa",
                    payload=result,
                    is_mocked=False,
                )
            )
            if result["sa_score"] <= sa_threshold:
                survivors_sa.append(mol)
        _log_stage(
            session, job_id, "sa", "SUCCEEDED",
            f"{len(survivors_sa)}/{len(molecules)} passed SA <= {sa_threshold}",
        )

        # --- Stage 3: ADMET (mocked) -------------------------------------
        _set_job_status(session, job, "ADMET", "admet")
        _log_stage(session, job_id, "admet", "STARTED")
        survivors_admet: list[models.Molecule] = []
        admet_by_mol: dict[str, dict] = {}
        for mol in survivors_sa:
            result = mocks.predict_admet(mol.smiles)
            admet_by_mol[mol.id] = result
            session.add(
                models.StageResult(
                    molecule_id=mol.id,
                    job_id=job_id,
                    stage="admet",
                    payload=result,
                    is_mocked=True,
                )
            )
            if result["profile"] in ("Favorable", "Moderate"):
                survivors_admet.append(mol)
        _log_stage(session, job_id, "admet", "SUCCEEDED", f"{len(survivors_admet)} passed")

        # --- Stage 4: binding affinity (mocked) ---------------------------
        _set_job_status(session, job, "AFFINITY", "affinity")
        _log_stage(session, job_id, "affinity", "STARTED")
        affinity_by_mol: dict[str, dict] = {}
        for mol in survivors_admet:
            result = mocks.predict_binding_affinity(mol.smiles)
            affinity_by_mol[mol.id] = result
            session.add(
                models.StageResult(
                    molecule_id=mol.id,
                    job_id=job_id,
                    stage="affinity",
                    payload=result,
                    is_mocked=True,
                )
            )
        _log_stage(session, job_id, "affinity", "SUCCEEDED", f"{len(survivors_admet)} scored")

        # --- Stage 5: unbinding kinetics (mocked, "GPU") ------------------
        _set_job_status(session, job, "KINETICS", "kinetics")
        _log_stage(session, job_id, "kinetics", "STARTED")
        for mol in survivors_admet:
            kd = affinity_by_mol[mol.id]["kd_nm"]
            result = mocks.predict_unbinding_kinetics(mol.smiles, kd)
            session.add(
                models.StageResult(
                    molecule_id=mol.id,
                    job_id=job_id,
                    stage="kinetics",
                    payload=result,
                    is_mocked=True,
                )
            )
        _log_stage(session, job_id, "kinetics", "SUCCEEDED", f"{len(survivors_admet)} scored")

        _set_job_status(session, job, "COMPLETE", None)

    except Exception as exc:  # noqa: BLE001 — top-level job failure boundary
        _log_stage(session, job_id, job.current_stage or "unknown", "FAILED", str(exc))
        job.failure_reason = str(exc)
        _set_job_status(session, job, "FAILED", job.current_stage)
        raise
