from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from . import models, pipeline, schemas
from .database import get_session, init_db
from .stages import depiction, sa_scoring

app = FastAPI(title="Molecular Discovery Platform — Integration MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP only — would be locked down in production
    allow_methods=["*"],
    allow_headers=["*"],
)

STAGE_INDEX = {None: 0, "screening": 1, "sa": 2, "admet": 3, "affinity": 4, "kinetics": 5}
STAGE_META = {
    "screening": ("02", "CHEMICAL SPACE", "Ultra-Large Screening", "hits", True),
    "sa": ("03", "SYNTHETIC ACCESSIBILITY", "SA Score Filter", "feasible", False),
    "admet": ("04", "ADMET", "ADMET Prediction", "passed", True),
    "affinity": ("05", "BINDING AFFINITY", "Kd & ΔG Ranking", "shortlisted", True),
    "kinetics": ("06", "UNBINDING KINETICS", "koff & Residence Time τ", "active", True),
}


@app.on_event("startup")
def _startup():
    init_db()


def _job_or_404(session, job_id: str) -> models.Job:
    job = session.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _stage_counts(session, job_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for stage in pipeline.STAGE_SEQUENCE:
        rows = session.execute(
            select(models.StageResult).where(
                models.StageResult.job_id == job_id, models.StageResult.stage == stage
            )
        ).scalars().all()
        counts[stage] = len(rows)
    return counts


@app.post("/api/jobs", response_model=schemas.JobStatusResponse)
def create_job(req: schemas.CreateJobRequest, background_tasks: BackgroundTasks):
    with get_session() as session:
        target = models.Target(name=req.target_name, source="pdb_id", pdb_id=req.pdb_id)
        session.add(target)
        session.flush()

        job = models.Job(
            target_id=target.id,
            status="SUBMITTED",
            label=f"RUN-{req.run_tag}",
            params={
                "run_tag": req.run_tag,
                "library_limit": req.library_limit,
                "sa_threshold": req.sa_threshold,
            },
        )
        session.add(job)
        session.flush()
        job_id = job.id

    def _run(job_id: str = job_id):
        with get_session() as bg_session:
            pipeline.run_pipeline(bg_session, job_id)

    background_tasks.add_task(_run)
    return get_job_status(job_id)


@app.get("/api/jobs/{job_id}", response_model=schemas.JobStatusResponse)
def get_job_status(job_id: str):
    with get_session() as session:
        job = _job_or_404(session, job_id)
        target = session.get(models.Target, job.target_id)
        counts = _stage_counts(session, job_id)
        sa_threshold = float(job.params.get("sa_threshold", 5.0))

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
            )
        ]

        for stage_key in pipeline.STAGE_SEQUENCE:
            no, label, subtitle, count_label, is_mocked = STAGE_META[stage_key]
            idx = STAGE_INDEX[stage_key]
            if job.status == "FAILED" and job.current_stage == stage_key:
                status = "failed"
            elif idx < current_idx:
                status = "done"
            elif idx == current_idx and job.status != "COMPLETE":
                status = "active"
            else:
                status = "done" if job.status == "COMPLETE" else "pending"

            scale_note = (
                "Conceptual library scale: 10T+ (this demo screens a small seed set — "
                "see README)"
                if stage_key == "screening"
                else None
            )
            stages.append(
                schemas.StageCard(
                    stage_no=int(no),
                    key=stage_key,
                    label=label,
                    subtitle=subtitle,
                    count=counts.get(stage_key),
                    count_label=count_label,
                    badge="GPU / Docker (mocked)" if stage_key == "kinetics"
                    else "CPU / Docker (mocked)" if stage_key == "affinity"
                    else "220+ props (mocked)" if stage_key == "admet"
                    else "RDKit — real" if stage_key == "sa"
                    else "Mocked seed set",
                    status=status,
                    is_mocked=is_mocked,
                    scale_note=scale_note,
                )
            )

        retained = counts.get("kinetics", 0)
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
            target_name=target.name,
            pdb_id=target.pdb_id,
            stages=stages,
            retained=retained,
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

        provenance = [
            schemas.ProvenanceStep(label=f"Protein Target", is_mocked=False),
            schemas.ProvenanceStep(label="Ultra-Large Chemical Space (seed-set mock)", is_mocked=True),
            schemas.ProvenanceStep(label=f"SA Filter (RDKit, real) — score {sa.get('sa_score', '?')}", is_mocked=False),
            schemas.ProvenanceStep(label="ADMET Prediction (mocked)", is_mocked=True),
            schemas.ProvenanceStep(label="Binding Affinity (mocked)", is_mocked=True),
            schemas.ProvenanceStep(label="Unbinding Kinetics (mocked)", is_mocked=True),
            schemas.ProvenanceStep(label=f"Nominated Candidate {mol.display_id}", is_mocked=False),
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
        properties = {
            "SA_SCORE": by_stage.get("sa", {}).get("sa_score"),
            "MOLECULAR_WEIGHT": by_stage.get("sa", {}).get("molecular_weight"),
            "ADMET_PROFILE_mocked": by_stage.get("admet", {}).get("profile"),
            "KD_NM_mocked": by_stage.get("affinity", {}).get("kd_nm"),
            "KOFF_PER_S_mocked": by_stage.get("kinetics", {}).get("koff_per_s"),
            "RESIDENCE_TIME_MIN_mocked": by_stage.get("kinetics", {}).get("residence_time_min"),
        }
        sdf_text = depiction.sdf_for_candidate(mol.smiles, mol.display_id, properties)
        return Response(
            content=sdf_text,
            media_type="chemical/x-mdl-sdfile",
            headers={
                "Content-Disposition": f'attachment; filename="{mol.display_id}.sdf"'
            },
        )


def _latest_payloads(session, molecule_id: str) -> dict[str, dict]:
    rows = session.execute(
        select(models.StageResult).where(models.StageResult.molecule_id == molecule_id)
    ).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        out[r.stage] = r.payload
    return out
