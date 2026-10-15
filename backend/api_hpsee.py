"""BioSolveIT HPSee API: connection state, chemical spaces, definition uploads, workflow monitor, anchor review."""
from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import models, storage
from .campaign import hpsee_flow
from .campaign import params as P
from .config import settings
from .database import get_session
from .science.engines import hpsee as H
from .stages import depiction

router = APIRouter()

DEFINITION_KINDS = {"flexx": ".flexx", "hydescorer": ".hydescorer"}


def _iso(v):
    return v.isoformat() if v else None


def _http(exc: H.HPSeeError) -> HTTPException:
    code = {"not_configured": 503, "unreachable": 502, "auth": 502, "license": 409, "not_found": 404}.get(exc.category, 422)
    return HTTPException(code, {"message": str(exc), "category": exc.category})


# ------------------------------------------------------------- connection ---
@router.get("/api/hpsee/status")
def hpsee_status(refresh: bool = False):
    """Live connection, login and per-workflow licence state (HPSee's own /Licenses answer)."""
    return H.status(refresh=refresh).public()


@router.get("/api/hpsee/spaces")
def hpsee_spaces():
    """Chemical Spaces uploaded to HPSee. Only what HPSee returns; nothing is listed otherwise."""
    try:
        spaces = H.HPSeeClient().chemical_spaces()
    except H.HPSeeError as exc:
        raise _http(exc) from exc
    out = []
    for sp in spaces:
        prep = sp.get("preparationWorkflowInfo") or {}
        pst = prep.get("status")
        ready = pst is None or pst in (H.SUCCESSFUL, H.FINISHED_WITH_ERRORS)
        out.append({
            "space_id": sp.get("spaceId"), "name": sp.get("spaceName"), "description": sp.get("description"),
            "synthons": sp.get("nofSynthons"), "info": sp.get("spaceInformation"), "created_at": sp.get("createdAt"),
            "ready": ready, "preparation_status": prep.get("statusDescription") if prep else None,
        })
    return {"spaces": out}


@router.get("/api/hpsee/filter-properties")
def hpsee_filter_properties():
    """Property names HPSee accepts in anchoring/extension filters (its own list)."""
    try:
        return {"properties": H.HPSeeClient().entry_properties()}
    except H.HPSeeError as exc:
        raise _http(exc) from exc


# ------------------------------------------------- SeeSAR definition files ---
@router.post("/api/hpsee/definitions", status_code=201)
async def upload_definition(kind: str = Form(...), file: UploadFile = File(...)):
    """Store a SeeSAR-exported .flexx (docking) or .hydescorer (scoring) definition, unchanged.

    The format is undocumented, so the file is never parsed or modified here; HPSee validates
    it when a workflow starts. Only the extension, size and non-emptiness are checked.
    """
    if kind not in DEFINITION_KINDS:
        raise HTTPException(422, "kind must be 'flexx' or 'hydescorer'")
    ext = DEFINITION_KINDS[kind]
    name = (file.filename or "").strip()
    if not name.lower().endswith(ext):
        raise HTTPException(422, f"Expected a SeeSAR {ext} file (exported from SeeSAR's Docking mode), got '{name}'.")
    limit = settings.hpsee_max_definition_mb * 1024 * 1024
    chunks, size = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"File exceeds the {settings.hpsee_max_definition_mb} MB limit.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data.strip():
        raise HTTPException(422, "The file is empty.")
    digest = hashlib.sha256(data).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120]
    d = storage.ensure_dir("hpsee", "definitions", digest[:16])
    path = d / safe
    path.write_bytes(data)
    with get_session() as s:
        a = models.Artifact(job_id=None, stage="hpsee_definition", kind=kind, storage_path=storage.to_stored(path))
        s.add(a)
        s.flush()
        return {"definition_id": a.id, "kind": kind, "filename": name, "size_bytes": size, "sha256": digest}


# -------------------------------------------------------- campaign monitor ---
def _run_out(r: models.HPSeeRun) -> dict:
    return {
        "run_id": r.id, "kind": r.kind, "round": r.round, "batch_id": r.batch_id, "status": r.status,
        "workflow_id": r.workflow_id, "csd_id": r.csd_id, "hpsee_status": r.hpsee_status,
        "hpsee_status_text": r.hpsee_status_text, "tasks": r.tasks or [], "result_size": r.result_size,
        "fetched": r.fetched_count, "error": r.error, "error_category": r.error_category, "retryable": r.retryable,
        "selected_count": len(r.selected_entry_ids or []), "decision": r.decision, "summary": r.summary,
        "created_at": _iso(r.created_at), "submitted_at": _iso(r.submitted_at), "finished_at": _iso(r.finished_at),
    }


@router.get("/api/jobs/{job_id}/hpsee")
def job_hpsee(job_id: str):
    with get_session() as s:
        job = s.get(models.Job, job_id)
        if job is None:
            raise HTTPException(404, "Campaign not found")
        p = P.resolve(job.params)
        runs = s.execute(select(models.HPSeeRun).where(models.HPSeeRun.job_id == job_id)
                         .order_by(models.HPSeeRun.created_at)).scalars().all()
        waiting = hpsee_flow.awaiting_run(s, job_id) if job.status == "AWAITING_REVIEW" else None
        review = None
        if waiting is not None:
            n = s.execute(select(func.count()).select_from(models.HPSeeEntry).where(
                models.HPSeeEntry.run_id == waiting.id)).scalar()
            review = {"run_id": waiting.id, "kind": waiting.kind, "round": waiting.round, "entries": n,
                      "summary": waiting.summary, "max_selection": H.MAX_EXTENSION_SELECTION,
                      "can_finish": waiting.kind == "extension" and bool((waiting.summary or {}).get("complete")),
                      "max_rounds": P.HPSEE_MAX_ROUNDS}
        return {"engine": p["engine"], "mode": p["hpsee_mode"] if p["engine"] == "hpsee" else None,
                "space": {"id": p["hpsee_space_id"], "name": p["hpsee_space_name"]} if P.is_csd(p) else None,
                "runs": [_run_out(r) for r in runs], "review": review}


@router.get("/api/jobs/{job_id}/hpsee/runs/{run_id}/entries")
def run_entries(job_id: str, run_id: str, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                open_only: bool = False):
    """Downloaded HPSee result entries of one run, best first, with 2D depictions."""
    with get_session() as s:
        run = s.get(models.HPSeeRun, run_id)
        if run is None or run.job_id != job_id:
            raise HTTPException(404, "HPSee run not found")
        q = select(models.HPSeeEntry).where(models.HPSeeEntry.run_id == run_id)
        if open_only:
            q = q.where(models.HPSeeEntry.n_linkers > 0)
        total = s.execute(select(func.count()).select_from(q.subquery())).scalar()
        rows = s.execute(q.order_by(models.HPSeeEntry.rank).offset((page - 1) * size).limit(size)).scalars().all()
        items = []
        for e in rows:
            svg = None
            if e.smiles:
                try:
                    svg = depiction.svg_for_smiles(e.smiles)
                except Exception:  # noqa: BLE001 - a depiction failure must not hide the entry
                    svg = None
            hy = e.hyde or {}
            items.append({
                "entry_id": e.entry_id, "rank": e.rank, "name": e.name, "smiles": e.smiles, "structure_svg": svg,
                "open_attachment_points": e.n_linkers, "complete": e.n_linkers == 0 and bool(e.smiles),
                "docking_score": e.docking_score, "hyde_affinity_lower_nm": hy.get("affinity_lower_nm"),
                "hyde_affinity_upper_nm": hy.get("affinity_upper_nm"), "hyde_ligand_efficiency": hy.get("ligand_efficiency"),
                "hyde_inter_clash": hy.get("inter_clash"), "note": e.note,
            })
        return {"run_id": run_id, "kind": run.kind, "round": run.round, "total": total, "page": page, "size": size,
                "items": items}


@router.get("/api/jobs/{job_id}/hpsee/runs/{run_id}/entries/{entry_id}/sdf")
def run_entry_sdf(job_id: str, run_id: str, entry_id: int):
    """The entry's docked pose, exactly as HPSee returned it (for the 3D viewer or download)."""
    with get_session() as s:
        e = s.execute(select(models.HPSeeEntry).where(models.HPSeeEntry.run_id == run_id,
                                                      models.HPSeeEntry.job_id == job_id,
                                                      models.HPSeeEntry.entry_id == entry_id)).scalar_one_or_none()
        if e is None or not e.pose_path:
            raise HTTPException(404, "Entry not found")
        path = storage.ensure_dir("runs", job_id) / e.pose_path
        if not path.is_file():
            raise HTTPException(404, "Entry file is missing")
        return Response(path.read_text(), media_type="chemical/x-mdl-sdfile",
                        headers={"Content-Disposition": f'attachment; filename="hpsee-entry-{entry_id}.sdf"'})


class ReviewRequest(BaseModel):
    run_id: str
    action: str = "extend"                       # extend | finish
    entry_ids: list[int] = Field(default_factory=list, max_length=H.MAX_EXTENSION_SELECTION)
    confirm: bool = False


@router.post("/api/jobs/{job_id}/hpsee/review")
def submit_review(job_id: str, req: ReviewRequest):
    with get_session() as s:
        job = s.get(models.Job, job_id)
        if job is None:
            raise HTTPException(404, "Campaign not found")
        try:
            return hpsee_flow.submit_review(s, job, req.run_id, req.action, req.entry_ids, req.confirm)
        except hpsee_flow.ReviewError as exc:
            raise HTTPException(409, str(exc)) from exc
