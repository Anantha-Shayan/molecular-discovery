"""Campaign ("job") API: create, monitor, control, inspect and download."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from . import models, schemas, storage
from .campaign import params as P
from .campaign import results
from .campaign import runner as rn
from .config import settings
from .database import get_session
from .science import admet as admet_model
from .science import site as sitelib
from .stages import depiction
from .tools import registry

router = APIRouter()


def _iso(v):
    return v.isoformat() if v else None


def _job(s, job_id: str) -> models.Job:
    job = s.get(models.Job, job_id)
    if job is None:
        raise HTTPException(404, "Campaign not found")
    return job


def _target(s, target_id: str) -> models.Target:
    t = s.get(models.Target, target_id)
    if t is None:
        raise HTTPException(404, "Target not found")
    return t


# ---------------------------------------------------------------- tools ----
@router.get("/api/tools")
def tools():
    """Honest inventory of every scientific engine and whether it can run here."""
    st = {k: v.public() for k, v in registry.tool_statuses().items()}
    return {
        "tools": st,
        "models": [{"key": "admet", "name": admet_model.MODEL_NAME, "available": True}],
        "mocked": [
            {"key": "affinity", "name": "Binding affinity (Koffee)", "reason": "Koffee integration pending; placeholder values only."},
            {"key": "kinetics", "name": "Unbinding kinetics (Koffee)", "reason": "Koffee integration pending; placeholder values only."},
        ],
        "limits": {"max_library": P.MAX_LIBRARY, "batch_size": settings.batch_size,
                   "max_parallel_batches": settings.max_parallel_batches, "cpu_budget": settings.cpu_budget},
    }


@router.get("/api/library/status")
def library_status():
    """How the ZINC source works and what is cached locally (nothing is stored in the database)."""
    from .science import library as lib
    return lib.zinc_cache_status()


# ----------------------------------------------------------- binding site ---
def _structure_pdb(target: models.Target) -> Path:
    if not target.structure_path:
        raise HTTPException(409, "This target has no structure file.")
    src = storage.resolve(target.structure_path)
    if target.structure_format == "mmcif":
        import gemmi
        out = storage.ensure_dir("targets", target.id) / "as_pdb.pdb"
        if not out.exists():
            st = gemmi.read_structure(str(src))
            st.setup_entities()
            st.write_pdb(str(out))
        return out
    return src


@router.get("/api/targets/{target_id}/sites")
def target_sites(target_id: str, padding: float = Query(sitelib.DEFAULT_PADDING, ge=0, le=15)):
    with get_session() as s:
        t = _target(s, target_id)
        pdb = _structure_pdb(t)
        sites = sitelib.list_ligand_sites(pdb)
        return {
            "target_id": t.id,
            "chains": t.chains or [],
            "sites": [{**x.as_dict(), "box": sitelib.box_from_ligand(x, padding)} for x in sites],
            "box_limits": {"min": sitelib.BOX_MIN, "max": sitelib.BOX_MAX},
        }


class _BoxReq(BaseModel):
    center: list[float]
    size: list[float]


@router.post("/api/targets/{target_id}/sites/validate-box")
def validate_box(target_id: str, req: _BoxReq):
    with get_session() as s:
        t = _target(s, target_id)
        try:
            return sitelib.validate_manual_box(req.center, req.size, _structure_pdb(t))
        except sitelib.SiteError as exc:
            raise HTTPException(422, str(exc)) from exc


# -------------------------------------------------------------- create ------
@router.post("/api/jobs", status_code=201)
def create_job(req: schemas.CreateJobRequest):
    with get_session() as s:
        t = _target(s, req.target_id)
        if t.validation_status != "PASSED":
            raise HTTPException(422, "The structure did not pass validation, so it cannot be docked.")
        pdb = _structure_pdb(t)
        try:
            if req.site.mode == "ligand":
                if not req.site.reference:
                    raise sitelib.SiteError("Choose the co-crystallised ligand that marks the pocket.")
                site = sitelib.box_from_ligand(sitelib.find_site(pdb, req.site.reference), req.site.padding)
            elif req.site.mode == "manual":
                site = sitelib.validate_manual_box(req.site.center or [], req.site.size or [], pdb)
            else:
                raise sitelib.SiteError("Site mode must be 'ligand' or 'manual'.")
            params = req.model_dump(exclude={"target_id", "label", "site"})
            params["site"] = site
            params["chain"] = req.chain or t.selected_chain
            tool = registry.get(req.engine) if req.engine in ("vina", "flexx") else None
            if tool is None:
                raise P.ParamError("Unknown docking engine.")
            if not tool.available:
                raise P.ParamError(f"{tool.name} is not available on this server: {tool.reason}")
            if req.enable_hyde and not registry.get("hydescorer").available:
                raise P.ParamError("HYDE is not available on this server; turn HYDE scoring off.")
            job = rn.create_campaign(s, t, params, req.label or f"{t.pdb_id or t.name} · {req.engine}")
        except (sitelib.SiteError, P.ParamError) as exc:
            raise HTTPException(422, str(exc)) from exc
        s.flush()
        return {"job_id": job.id}


# ---------------------------------------------------------------- list ------
@router.get("/api/jobs")
def list_jobs(limit: int = Query(50, ge=1, le=200)):
    with get_session() as s:
        jobs = s.execute(select(models.Job).order_by(models.Job.created_at.desc()).limit(limit)).scalars().all()
        out = []
        for j in jobs:
            t = s.get(models.Target, j.target_id)
            p = P.resolve(j.params)
            n_total = s.execute(select(func.count()).select_from(models.Molecule).where(
                models.Molecule.job_id == j.id, models.Molecule.outcome != "generated")).scalar()
            n_active = s.execute(select(func.count()).select_from(models.Molecule).where(
                models.Molecule.job_id == j.id, models.Molecule.outcome == "active")).scalar()
            out.append({
                "job_id": j.id, "label": j.label, "status": j.status, "current_stage": j.current_stage,
                "target_name": t.name if t else "—", "pdb_id": t.pdb_id if t else None,
                "engine": p["engine"], "library_source": p["library_source"],
                "molecules": n_total, "candidate_count": n_active if j.status == "COMPLETE" else None,
                "created_at": _iso(j.created_at), "finished_at": _iso(j.finished_at),
            })
        return {"jobs": out}


# -------------------------------------------------------------- detail ------
@router.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    with get_session() as s:
        j = _job(s, job_id)
        t = s.get(models.Target, j.target_id)
        p = P.resolve(j.params)
        stages = s.execute(select(models.JobStage).where(models.JobStage.job_id == j.id)
                           .order_by(models.JobStage.position)).scalars().all()
        batch_rows = s.execute(select(models.Batch).where(models.Batch.job_id == j.id)
                               .order_by(models.Batch.stage, models.Batch.index)).scalars().all()
        by_stage: dict[str, dict] = {}
        for b in batch_rows:
            d = by_stage.setdefault(b.stage, {"total": 0, "pending": 0, "running": 0, "succeeded": 0,
                                              "failed": 0, "retrying": 0, "cancelled": 0, "skipped": 0})
            d["total"] += 1
            d[b.status] = d.get(b.status, 0) + 1
        outcome = dict(s.execute(select(models.Molecule.outcome, func.count()).where(
            models.Molecule.job_id == j.id).group_by(models.Molecule.outcome)).all())
        stage_cards = []
        for st in stages:
            meta = P.STAGES[st.stage]
            stage_cards.append({
                "key": st.stage, "label": meta["label"], "blurb": meta["blurb"], "status": st.status,
                "mocked": meta["mocked"], "batched": meta["batched"], "tool": st.tool, "detail": st.detail,
                "count_in": st.count_in, "count_out": st.count_out, "count_failed": st.count_failed,
                "batches": by_stage.get(st.stage), "started_at": _iso(st.started_at), "finished_at": _iso(st.finished_at),
            })
        events = s.execute(select(models.JobStageLog).where(models.JobStageLog.job_id == j.id)
                           .order_by(models.JobStageLog.started_at.desc()).limit(40)).scalars().all()
        public_params = {k: v for k, v in p.items() if k != "library_upload"}
        return {
            "job_id": j.id, "label": j.label, "status": j.status, "current_stage": j.current_stage,
            "failure_reason": j.failure_reason, "cancel_requested": j.cancel_requested,
            "created_at": _iso(j.created_at), "started_at": _iso(j.started_at), "finished_at": _iso(j.finished_at),
            "updated_at": _iso(j.updated_at),
            "target": {"target_id": t.id, "name": t.name, "pdb_id": t.pdb_id, "chain": p["chain"]} if t else None,
            "config": public_params, "rank_policy": P.rank_policy_text(p),
            "stages": stage_cards, "molecule_outcomes": outcome,
            "events": [{"stage": e.stage, "status": e.status, "detail": e.detail, "at": _iso(e.started_at)} for e in events],
            "can_resume": j.status in ("FAILED", "CANCELLED"),
            "can_cancel": j.status in ("QUEUED", "RUNNING") and not j.cancel_requested,
        }


@router.get("/api/jobs/{job_id}/batches")
def job_batches(job_id: str, stage: str | None = None):
    with get_session() as s:
        _job(s, job_id)
        q = select(models.Batch).where(models.Batch.job_id == job_id).order_by(models.Batch.stage, models.Batch.index)
        if stage:
            q = q.where(models.Batch.stage == stage)
        return {"batches": [{
            "batch_id": b.id, "stage": b.stage, "index": b.index, "status": b.status, "attempt": b.attempt,
            "size": len(b.molecule_ids), "ok": b.ok_count, "failed": b.failed_count, "error": b.error,
            "seconds": b.seconds, "started_at": _iso(b.started_at), "finished_at": _iso(b.finished_at),
            "has_log": bool(b.log_path),
        } for b in s.execute(q).scalars()]}


@router.get("/api/jobs/{job_id}/batches/{batch_id}/log")
def batch_log(job_id: str, batch_id: str, tail_kb: int = Query(32, ge=1, le=256)):
    with get_session() as s:
        _job(s, job_id)
        b = s.get(models.Batch, batch_id)
        if b is None or b.job_id != job_id:
            raise HTTPException(404, "Batch not found")
        if not b.log_path:
            return Response("(no log yet)", media_type="text/plain")
        try:
            path = storage.resolve(b.log_path)
        except storage.StoragePathError:
            raise HTTPException(404, "Log not found")
        if not path.exists():
            return Response("(log file not found)", media_type="text/plain")
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - tail_kb * 1024))
            text = fh.read().decode("utf-8", errors="replace")
        # Never leak the host layout through logs.
        text = text.replace(str(settings.data_dir), "<data>")
        return Response(text, media_type="text/plain; charset=utf-8")


@router.get("/api/jobs/{job_id}/molecules")
def job_molecules(job_id: str, outcome: str | None = None, limit: int = Query(500, ge=1, le=5000)):
    """Every molecule with its funnel outcome: where and why it left the campaign."""
    with get_session() as s:
        _job(s, job_id)
        q = select(models.Molecule).where(models.Molecule.job_id == job_id).order_by(models.Molecule.display_id).limit(limit)
        if outcome:
            q = q.where(models.Molecule.outcome == outcome)
        return {"molecules": [{
            "molecule_id": m.id, "display_id": m.display_id, "smiles": m.smiles, "source": m.source,
            "source_id": m.external_id, "outcome": m.outcome, "exit_stage": m.exit_stage, "exit_reason": m.exit_reason,
        } for m in s.execute(q).scalars()]}


# ---------------------------------------------------------- candidates ------
def _candidate_row(m: models.Molecule, by: dict, rank: int | None, svg: bool = True) -> dict:
    return {
        "molecule_id": m.id, "display_id": m.display_id, "rank": rank, "smiles": m.smiles,
        "source": m.source, "source_id": m.external_id,
        "structure_svg": depiction.svg_for_smiles(m.smiles) if svg else None,
        **results.flat_metrics(by),
    }


@router.get("/api/jobs/{job_id}/candidates")
def candidates(job_id: str):
    with get_session() as s:
        j = _job(s, job_id)
        items = results.ranked_candidates(s, j)
        return {
            "job_id": job_id, "status": j.status, "rank_policy": P.rank_policy_text(P.resolve(j.params)),
            "provisional": j.status != "COMPLETE",
            "candidates": [_candidate_row(m, by, i) for i, (m, by) in enumerate(items, start=1)],
        }


@router.get("/api/jobs/{job_id}/analogues")
def analogues(job_id: str):
    """FastGrow-generated suggestions. These are NOT catalogue compounds and NOT part of the ranked list."""
    with get_session() as s:
        _job(s, job_id)
        rows = s.execute(select(models.StageResult, models.Molecule).join(
            models.Molecule, models.Molecule.id == models.StageResult.molecule_id).where(
            models.StageResult.job_id == job_id, models.StageResult.stage == "grow")).all()
        items = sorted(rows, key=lambda r: r[0].payload.get("fastgrow_score", 0.0))
        return {"job_id": job_id, "origin": "generated by FastGrow; not catalogue compounds; not synthesised or validated",
                "analogues": [{
                    "molecule_id": m.id, "display_id": m.display_id, "smiles": m.smiles,
                    "structure_svg": depiction.svg_for_smiles(m.smiles),
                    "parent": r.payload.get("parent_display_id"), "fastgrow_score": r.payload.get("fastgrow_score"),
                    "sa_score": r.payload.get("sa_score"), "heavy_atoms": r.payload.get("heavy_atoms"),
                    "exit_vector": r.payload.get("exit_vector"), "fragment_source": r.payload.get("fragment_source"),
                } for r, m in items]}


@router.get("/api/jobs/{job_id}/analogues.sdf")
def analogues_sdf(job_id: str):
    with get_session() as s:
        _job(s, job_id)
        p = storage.ensure_dir("runs", job_id, "grow") / "generated_analogues.sdf"
        if not p.exists():
            raise HTTPException(404, "No generated analogues for this campaign")
        return _sdf_response(p.read_text(), f"{job_id[:8]}-generated-analogues.sdf")


@router.get("/api/candidates/{molecule_id}")
def candidate_detail(molecule_id: str):
    with get_session() as s:
        m = s.get(models.Molecule, molecule_id)
        if m is None:
            raise HTTPException(404, "Molecule not found")
        j = s.get(models.Job, m.job_id)
        by = results.stage_payloads(s, m.job_id, [m.id]).get(m.id, {})
        errors = {r.stage: r.error for r in s.execute(select(models.StageResult).where(
            models.StageResult.molecule_id == m.id, models.StageResult.status == "failed")).scalars()}
        stages = s.execute(select(models.JobStage).where(models.JobStage.job_id == m.job_id)
                           .order_by(models.JobStage.position)).scalars().all()
        prov = []
        for st in stages:
            meta = P.STAGES[st.stage]
            if st.status == "skipped" or st.stage in ("prepare", "library", "rank"):
                continue
            prov.append({"stage": st.stage, "label": meta["label"], "mocked": meta["mocked"], "tool": st.tool,
                         "has_result": st.stage in by, "error": errors.get(st.stage)})
        rank = by.get("rank", {}).get("rank")
        return {
            **_candidate_row(m, by, rank), "svg_large": depiction.svg_for_smiles(m.smiles, 320, 220),
            "outcome": m.outcome, "exit_stage": m.exit_stage, "exit_reason": m.exit_reason,
            "job_id": m.job_id, "target": {"pdb_id": (s.get(models.Target, j.target_id).pdb_id)},
            "stage_payloads": {k: v for k, v in by.items() if k not in ("rank",)},
            "admet_endpoints": {k: {"group": v[0], "label": v[1], "kind": v[2], "unit": v[3]}
                                for k, v in admet_model.ENDPOINTS.items()},
            "provenance": prov, "has_pose": bool(by.get("docking", {}).get("pose_path")),
        }


@router.get("/api/candidates/{molecule_id}/complex")
def candidate_complex(molecule_id: str):
    """Pose (SDF) + protein (PDB) text for the 3D viewer."""
    with get_session() as s:
        m = s.get(models.Molecule, molecule_id)
        if m is None:
            raise HTTPException(404, "Molecule not found")
        by = results.stage_payloads(s, m.job_id, [m.id]).get(m.id, {})
        rel = by.get("hyde", {}).get("pose_path") or by.get("docking", {}).get("pose_path")
        if not rel:
            raise HTTPException(404, "No 3D pose for this molecule")
        job_dir = storage.ensure_dir("runs", m.job_id)
        pose = (job_dir / rel).read_text()
        prot = (job_dir / "prepare" / "protein.pdb").read_text()
        return {"pose_sdf": pose, "protein_pdb": prot,
                "pose_source": "HYDE-optimised" if by.get("hyde", {}).get("pose_path") else "docking"}


def _sdf_response(text: str, name: str) -> Response:
    return Response(text, media_type="chemical/x-mdl-sdfile",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/candidates/{molecule_id}/sdf")
def candidate_sdf(molecule_id: str):
    with get_session() as s:
        m = s.get(models.Molecule, molecule_id)
        if m is None:
            raise HTTPException(404, "Molecule not found")
        by = results.stage_payloads(s, m.job_id, [m.id]).get(m.id, {})
        text = results.sdf_record(storage.ensure_dir("runs", m.job_id), m, by, by.get("rank", {}).get("rank"))
        return _sdf_response(text, f"{m.display_id}.sdf")


@router.get("/api/jobs/{job_id}/candidates.sdf")
def candidates_sdf(job_id: str):
    with get_session() as s:
        j = _job(s, job_id)
        items = results.ranked_candidates(s, j)
        if not items:
            raise HTTPException(404, "No candidates yet")
        jd = storage.ensure_dir("runs", job_id)
        text = "".join(results.sdf_record(jd, m, by, i) for i, (m, by) in enumerate(items, start=1))
        return _sdf_response(text, f"{job_id[:8]}-ranked-candidates.sdf")


@router.get("/api/jobs/{job_id}/candidates.csv")
def candidates_csv(job_id: str):
    with get_session() as s:
        j = _job(s, job_id)
        text = results.results_csv(results.ranked_candidates(s, j))
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{job_id[:8]}-ranked-candidates.csv"'})


@router.get("/api/jobs/{job_id}/artifacts")
def artifacts(job_id: str):
    with get_session() as s:
        _job(s, job_id)
        rows = s.execute(select(models.Artifact).where(models.Artifact.job_id == job_id)
                         .order_by(models.Artifact.created_at)).scalars().all()
        out = []
        for a in rows:
            try:
                p = storage.resolve(a.storage_path)
                size = p.stat().st_size if p.exists() else None
            except storage.StoragePathError:
                continue
            out.append({"artifact_id": a.id, "stage": a.stage, "kind": a.kind,
                        "name": Path(a.storage_path).name, "size_bytes": size})
        return {"artifacts": out}


@router.get("/api/artifacts/{artifact_id}")
def artifact_download(artifact_id: str):
    with get_session() as s:
        a = s.get(models.Artifact, artifact_id)
        if a is None:
            raise HTTPException(404, "Artifact not found")
        try:
            p = storage.resolve(a.storage_path)
        except storage.StoragePathError:
            raise HTTPException(404, "Artifact not found")
        if not p.is_file():
            raise HTTPException(404, "Artifact file is missing")
        from fastapi.responses import FileResponse
        return FileResponse(p, filename=p.name)


# -------------------------------------------------------------- control -----
def _control(job_id: str, fn, *args, **kw):
    with get_session() as s:
        j = _job(s, job_id)
        try:
            return fn(s, j, *args, **kw)
        except rn.ControlError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    _control(job_id, rn.request_cancel)
    return {"ok": True}


@router.post("/api/jobs/{job_id}/resume")
def resume(job_id: str):
    _control(job_id, rn.resume)
    return {"ok": True}


@router.post("/api/jobs/{job_id}/retry")
def retry(job_id: str, req: schemas.RetryRequest):
    n = _control(job_id, rn.retry_batches, req.batch_ids, req.failed_molecules)
    return {"ok": True, "queued_batches": n}
