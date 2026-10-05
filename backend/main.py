from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import models, pipeline, schemas, services
from .database import get_session, init_db
from .stages import depiction
from .targets import service as target_service

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Molecular Discovery Platform — Integration MVP", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP only — would be locked down in production
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(target_service.REPO_ROOT, "frontend")

STAGE_INDEX = {None: 0, "screening": 1, "sa": 2, "admet": 3, "affinity": 4, "kinetics": 5}
STAGE_META = {
    "screening": ("02", "CHEMICAL SPACE", "Demo Library Screening", "hits", True),
    "sa": ("03", "SYNTHETIC ACCESSIBILITY", "SA Score Filter", "feasible", False),
    "admet": ("04", "ADMET", "ADMET Prediction", "passed", True),
    "affinity": ("05", "BINDING AFFINITY", "Kd & ΔG Ranking", "shortlisted", True),
    "kinetics": ("06", "UNBINDING KINETICS", "koff & Residence Time τ", "active", True),
}
STAGE_BADGE = {
    "screening": "demo library (mocked)",
    "sa": "RDKit — real",
    "admet": "220+ props (mocked)",
    "affinity": "CPU / Docker (mocked)",
    "kinetics": "GPU / Docker (mocked)",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _job_or_404(session, job_id: str) -> models.Job:
    job = session.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _target_or_404(session, target_id: str) -> models.Target:
    target = session.get(models.Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")
    return target


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _target_response(target: models.Target) -> schemas.TargetResponse:
    return schemas.TargetResponse(
        target_id=target.id,
        name=target.name,
        source=target.source,
        pdb_id=target.pdb_id,
        original_filename=target.original_filename,
        structure_format=target.structure_format,
        checksum=target.checksum,
        file_size_bytes=target.file_size_bytes,
        title=target.title,
        experiment_method=target.experiment_method,
        resolution_a=target.resolution_a,
        chains=[schemas.ChainOut(**chain) for chain in (target.chains or [])],
        selected_chain=target.selected_chain,
        residue_count=target.residue_count,
        atom_count=target.atom_count,
        ligands=target.ligands or [],
        validation_status=target.validation_status,
        validation_checks=[
            schemas.ValidationCheckOut(**check) for check in (target.validation_checks or [])
        ],
        is_demo=bool(target.is_demo),
        created_at=_iso(target.created_at),
    )


def _stage_counts(session, job_id: str) -> dict[str, dict[str, int]]:
    """Per-stage {in, out} counts, read from StageResult.payload['passed']."""
    counts: dict[str, dict[str, int]] = {}
    for stage in pipeline.STAGE_SEQUENCE:
        rows = session.execute(
            select(models.StageResult).where(
                models.StageResult.job_id == job_id,
                models.StageResult.stage == stage,
            )
        ).scalars().all()
        passed = sum(1 for row in rows if (row.payload or {}).get("passed", True))
        counts[stage] = {"in": len(rows), "out": passed}
    return counts


def _latest_payloads(session, molecule_id: str) -> dict[str, dict]:
    rows = session.execute(
        select(models.StageResult).where(models.StageResult.molecule_id == molecule_id)
    ).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        out[r.stage] = r.payload
    return out


def _stage_details(session, job_id: str) -> dict[str, str]:
    rows = session.execute(
        select(models.JobStageLog).where(models.JobStageLog.job_id == job_id)
    ).scalars().all()
    details: dict[str, str] = {}
    for row in rows:
        if row.detail and row.status in ("SUCCEEDED", "SKIPPED", "FAILED"):
            details[row.stage] = row.detail
    return details


def _skipped_stages(config: dict) -> set[str]:
    skipped = set()
    if not config.get("enable_admet", True):
        skipped.add("admet")
    if not config.get("enable_affinity", True):
        skipped.add("affinity")
    if not config.get("enable_kinetics", True):
        skipped.add("kinetics")
    return skipped


# ---------------------------------------------------------------------------
# Target intake
# ---------------------------------------------------------------------------
@app.post("/api/targets/upload", response_model=schemas.TargetResponse, status_code=201)
async def upload_target(file: UploadFile = File(...), name: str | None = None):
    raw = await file.read()
    with get_session() as session:
        try:
            target = target_service.create_target(
                session, raw=raw, filename=file.filename, source="upload", name=name
            )
        except target_service.TargetIntakeError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": exc.message, "checks": exc.checks},
            ) from exc
        return _target_response(target)


@app.post("/api/targets/pdb-id", response_model=schemas.TargetResponse, status_code=201)
def create_target_from_pdb_id(req: schemas.PdbIdRequest):
    with get_session() as session:
        try:
            raw = target_service.fetch_pdb_by_id(req.pdb_id)
            target = target_service.create_target(
                session,
                raw=raw,
                filename=f"{req.pdb_id.strip().upper()}.pdb",
                source="pdb_id",
                name=req.name,
                pdb_id=req.pdb_id.strip().upper(),
            )
        except target_service.TargetIntakeError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": exc.message, "checks": exc.checks},
            ) from exc
        return _target_response(target)


@app.post("/api/targets/demo", response_model=schemas.TargetResponse, status_code=201)
def create_demo_target():
    """The bundled demo fixture. Deterministic and works offline."""
    with get_session() as session:
        try:
            target = target_service.create_demo_target(session)
        except target_service.TargetIntakeError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": exc.message, "checks": exc.checks},
            ) from exc
        return _target_response(target)


@app.get("/api/targets/{target_id}", response_model=schemas.TargetResponse)
def get_target(target_id: str):
    with get_session() as session:
        return _target_response(_target_or_404(session, target_id))


@app.get("/api/targets/{target_id}/structure")
def get_target_structure(target_id: str):
    """Raw structure text — powers the 3D viewer and 'View source'."""
    with get_session() as session:
        target = _target_or_404(session, target_id)
        try:
            text = target_service.read_structure_text(target)
        except target_service.TargetIntakeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        return Response(content=text, media_type="chemical/x-pdb")


@app.get("/api/engines", response_model=list[schemas.EngineInfo])
def list_engines():
    """Which stage engines are real and which are demo adapters."""
    return services.describe_all()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.post("/api/jobs", response_model=schemas.JobStatusResponse)
def create_job(req: schemas.CreateJobRequest, background_tasks: BackgroundTasks):
    with get_session() as session:
        if req.target_id:
            target = _target_or_404(session, req.target_id)
            if req.selected_chain:
                target.selected_chain = req.selected_chain
                session.flush()
        else:
            # Legacy contract: name/PDB-ID only, no structure file.
            target = models.Target(
                name=req.target_name or "Unnamed target",
                source="pdb_id" if req.pdb_id else "upload",
                pdb_id=req.pdb_id,
                validation_status="NOT_VALIDATED",
                validation_checks=[],
            )
            session.add(target)
            session.flush()

        run_tag = req.run_tag or target.id[:4].upper()
        params = {
            "run_tag": run_tag,
            "library_limit": req.library_limit,
            "sa_threshold": req.sa_threshold,
            "enable_admet": req.enable_admet,
            "enable_affinity": req.enable_affinity,
            "enable_kinetics": req.enable_kinetics,
            "strict_admet_gate": req.strict_admet_gate,
            "affinity_shortlist": req.affinity_shortlist,
        }
        if req.stage_delay_seconds is not None:
            params["stage_delay_seconds"] = req.stage_delay_seconds

        job = models.Job(
            target_id=target.id,
            status="SUBMITTED",
            label=req.label or f"RUN-{run_tag}",
            params=params,
        )
        session.add(job)
        session.flush()
        job_id = job.id

    def _run(job_id: str = job_id):
        with get_session() as bg_session:
            pipeline.run_pipeline(bg_session, job_id)

    # Returns immediately; the pipeline runs after the response is sent.
    background_tasks.add_task(_run)
    return get_job_status(job_id)


@app.get("/api/jobs", response_model=schemas.JobListResponse)
def list_jobs(limit: int = 50):
    with get_session() as session:
        jobs = session.execute(
            select(models.Job).order_by(models.Job.created_at.desc()).limit(limit)
        ).scalars().all()

        summaries: list[schemas.JobSummary] = []
        for job in jobs:
            target = session.get(models.Target, job.target_id)
            kinetics = session.execute(
                select(models.StageResult).where(
                    models.StageResult.job_id == job.id,
                    models.StageResult.stage == "kinetics",
                )
            ).scalars().all()
            summaries.append(
                schemas.JobSummary(
                    job_id=job.id,
                    label=job.label,
                    status=job.status,
                    current_stage=job.current_stage,
                    target_name=target.name if target else "—",
                    pdb_id=target.pdb_id if target else None,
                    target_source=target.source if target else None,
                    candidate_count=len(kinetics),
                    created_at=_iso(job.created_at),
                )
            )
        return schemas.JobListResponse(jobs=summaries)


@app.get("/api/jobs/{job_id}", response_model=schemas.JobStatusResponse)
def get_job_status(job_id: str):
    with get_session() as session:
        job = _job_or_404(session, job_id)
        target = session.get(models.Target, job.target_id)
        counts = _stage_counts(session, job_id)
        details = _stage_details(session, job_id)
        config = pipeline.resolve_params(job.params)
        skipped = _skipped_stages(config)

        current_idx = (
            6 if job.status == "COMPLETE" else STAGE_INDEX.get(job.current_stage, 0)
        )

        stages: list[schemas.StageCard] = [
            schemas.StageCard(
                stage_no=1,
                key="target",
                label="TARGET",
                subtitle="Protein / PDB",
                count=1,
                count_label="Target",
                badge="Input",
                status="done",
                is_mocked=False,
                detail=details.get("target"),
            )
        ]

        for stage_key in pipeline.STAGE_SEQUENCE:
            no, label, subtitle, count_label, is_mocked = STAGE_META[stage_key]
            idx = STAGE_INDEX[stage_key]
            if job.status == "FAILED" and job.current_stage == stage_key:
                status = "failed"
            elif stage_key in skipped:
                status = "skipped"
            elif idx < current_idx:
                status = "done"
            elif idx == current_idx and job.status != "COMPLETE":
                status = "active"
            else:
                status = "done" if job.status == "COMPLETE" else "pending"

            scale_note = (
                f"Conceptual chemical space: 10T+. This run screens a "
                f"{config['library_limit']}-compound demo library."
                if stage_key == "screening"
                else None
            )
            stage_counts = counts.get(stage_key, {"in": 0, "out": 0})
            stages.append(
                schemas.StageCard(
                    stage_no=int(no),
                    key=stage_key,
                    label=label,
                    subtitle=subtitle,
                    count=stage_counts["out"] or None if status == "pending" else stage_counts["out"],
                    count_label=count_label,
                    badge=STAGE_BADGE[stage_key],
                    status=status,
                    is_mocked=is_mocked,
                    scale_note=scale_note,
                    count_in=stage_counts["in"],
                    count_out=stage_counts["out"],
                    detail=details.get(stage_key),
                )
            )

        retained = counts.get("kinetics", {}).get("out", 0)
        stages.append(
            schemas.StageCard(
                stage_no=7,
                key="candidates",
                label="CANDIDATES",
                subtitle="Ranked Output",
                count=retained if job.status == "COMPLETE" else None,
                count_label="leads",
                badge="Output",
                status="done" if job.status == "COMPLETE" else "pending",
                is_mocked=False,
            )
        )

        return schemas.JobStatusResponse(
            job_id=job.id,
            status=job.status,
            current_stage=job.current_stage,
            target_name=target.name if target else "—",
            pdb_id=target.pdb_id if target else None,
            stages=stages,
            retained=retained,
            target=_target_response(target) if target else None,
            config=config,
            engines=services.describe_all(),
            label=job.label,
            created_at=_iso(job.created_at),
            updated_at=_iso(job.updated_at),
            failure_reason=job.failure_reason,
        )


@app.get("/api/jobs/{job_id}/candidates", response_model=schemas.CandidateListResponse)
def list_candidates(job_id: str):
    with get_session() as session:
        _job_or_404(session, job_id)
        molecules = session.execute(
            select(models.Molecule).where(models.Molecule.job_id == job_id)
        ).scalars().all()

        rows: list[schemas.CandidateRow] = []
        for mol in molecules:
            by_stage = _latest_payloads(session, mol.id)
            if "kinetics" not in by_stage:
                continue  # only show molecules that made it through the full pipeline

            sa = by_stage.get("sa", {})
            admet = by_stage.get("admet", {})
            aff = by_stage.get("affinity", {})
            kin = by_stage.get("kinetics", {})

            status = pipeline.classify_status(
                sa.get("sa_score", 10),
                admet.get("profile", "Moderate"),
                kin.get("residence_time_min", 0),
            )
            rows.append(
                schemas.CandidateRow(
                    molecule_id=mol.id,
                    display_id=mol.display_id,
                    rank=0,  # assigned after sort
                    smiles=mol.smiles,
                    structure_svg=depiction.svg_for_smiles(mol.smiles),
                    molecular_weight=sa.get("molecular_weight", 0.0),
                    sa_score=sa.get("sa_score", 0.0),
                    admet_profile=admet.get("profile", "Unknown"),
                    kd_nm=aff.get("kd_nm", 0.0),
                    koff_per_s=kin.get("koff_per_s", 0.0),
                    residence_time_min=kin.get("residence_time_min", 0.0),
                    status=status,
                )
            )

        rows.sort(key=lambda r: r.residence_time_min, reverse=True)
        for i, row in enumerate(rows, start=1):
            row.rank = i

        return schemas.CandidateListResponse(job_id=job_id, candidates=rows)


@app.get("/api/jobs/{job_id}/candidates.sdf")
def export_job_sdf(job_id: str):
    """All final candidates for a run as one real RDKit-generated SDF."""
    with get_session() as session:
        _job_or_404(session, job_id)
        molecules = session.execute(
            select(models.Molecule).where(models.Molecule.job_id == job_id)
        ).scalars().all()

        records = []
        for mol in molecules:
            by_stage = _latest_payloads(session, mol.id)
            if "kinetics" not in by_stage:
                continue
            records.append(
                depiction.sdf_for_candidate(
                    mol.smiles, mol.display_id, pipeline._sdf_properties(by_stage)
                )
            )
        if not records:
            raise HTTPException(status_code=404, detail="No final candidates for this job yet")

        return Response(
            content="".join(records),
            media_type="chemical/x-mdl-sdfile",
            headers={
                "Content-Disposition": f'attachment; filename="{job_id[:8]}-candidates.sdf"'
            },
        )


@app.get("/api/candidates/{molecule_id}", response_model=schemas.CandidateDetailResponse)
def get_candidate_detail(molecule_id: str):
    with get_session() as session:
        mol = session.get(models.Molecule, molecule_id)
        if mol is None:
            raise HTTPException(status_code=404, detail="Molecule not found")

        by_stage = _latest_payloads(session, molecule_id)
        sa = by_stage.get("sa", {})
        admet = by_stage.get("admet", {})
        aff = by_stage.get("affinity", {})
        kin = by_stage.get("kinetics", {})

        status = pipeline.classify_status(
            sa.get("sa_score", 10), admet.get("profile", "Moderate"),
            kin.get("residence_time_min", 0),
        )

        job = session.get(models.Job, mol.job_id)
        target = session.get(models.Target, job.target_id) if job else None
        target_label = target.name if target else "Protein target"
        if target and target.pdb_id:
            target_label = f"{target.name} ({target.pdb_id})"

        provenance = [
            schemas.ProvenanceStep(label=f"Protein Target — {target_label}", is_mocked=False),
            schemas.ProvenanceStep(label="Chemical Space (demo library, mocked)", is_mocked=True),
            schemas.ProvenanceStep(
                label=f"SA Filter (RDKit, real) — score {sa.get('sa_score', '?')}", is_mocked=False
            ),
            schemas.ProvenanceStep(label="ADMET Prediction (mocked)", is_mocked=True),
            schemas.ProvenanceStep(label="Binding Affinity (mocked)", is_mocked=True),
            schemas.ProvenanceStep(label="Unbinding Kinetics (mocked)", is_mocked=True),
            schemas.ProvenanceStep(label=f"Candidate {mol.display_id} — {status}", is_mocked=False),
        ]

        return schemas.CandidateDetailResponse(
            molecule_id=mol.id,
            display_id=mol.display_id,
            smiles=mol.smiles,
            structure_svg=depiction.svg_for_smiles(mol.smiles, width=230, height=150),
            molecular_weight=sa.get("molecular_weight", 0.0),
            sa_score=sa.get("sa_score", 0.0),
            admet_profile=admet.get("profile", "Unknown"),
            admet_property_count=admet.get("property_count", 0),
            kd_nm=aff.get("kd_nm", 0.0),
            delta_g_kcal_mol=aff.get("delta_g_kcal_mol", 0.0),
            koff_per_s=kin.get("koff_per_s", 0.0),
            residence_time_min=kin.get("residence_time_min", 0.0),
            status=status,
            provenance=provenance,
        )


@app.get("/api/candidates/{molecule_id}/sdf")
def export_candidate_sdf(molecule_id: str):
    with get_session() as session:
        mol = session.get(models.Molecule, molecule_id)
        if mol is None:
            raise HTTPException(status_code=404, detail="Molecule not found")
        by_stage = _latest_payloads(session, molecule_id)
        sdf_text = depiction.sdf_for_candidate(
            mol.smiles, mol.display_id, pipeline._sdf_properties(by_stage)
        )
        return Response(
            content=sdf_text,
            media_type="chemical/x-mdl-sdfile",
            headers={
                "Content-Disposition": f'attachment; filename="{mol.display_id}.sdf"'
            },
        )


# ---------------------------------------------------------------------------
# Frontend (served by the API so the UI and API share an origin)
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/app/discoveries.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    path = os.path.join(FRONTEND_DIR, "favicon.ico")
    if os.path.exists(path):
        return FileResponse(path)
    return Response(status_code=204)


if os.path.isdir(FRONTEND_DIR):
    app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
