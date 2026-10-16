"""
Persistent campaign runner.

Model
  Campaign (models.Job) -> stages (models.JobStage) -> batches (models.Batch)
  -> molecules (models.Molecule + one StageResult per molecule per stage).

Everything the UI shows comes from these tables, written at every state
transition, so a refresh, an API restart or a crash loses nothing:

  * startup recovery: batches/jobs interrupted mid-run are put back to
    `pending`/`QUEUED`; completed batches are never repeated (resume);
  * a batch is claimed with a conditional UPDATE, so two workers (or a double
    click on Retry) can never execute it twice;
  * concurrency is bounded (MAX_PARALLEL_BATCHES) and CPU threads are split
    from CPU_BUDGET, so the machine is not exhausted;
  * each batch has a hard timeout and is cancellable; its subprocess group is
    killed (tools/proc.py);
  * a batch that fails is retried automatically up to MAX_BATCH_ATTEMPTS, then
    left `failed` for the user to inspect and retry selectively.

One campaign executes at a time (docking saturates the CPU); others queue.

A Chemical Space Docking campaign (engine=hpsee, hpsee_mode=chemical_space)
can also PAUSE in status AWAITING_REVIEW: the runner stops working on it until
a person confirms which anchors to extend (see campaign/hpsee_flow.py). A
paused campaign holds no CPU and does not block the queue.
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time
import traceback
from pathlib import Path

from sqlalchemy import select, update, delete, func
from sqlalchemy.orm import Session

from .. import models, storage
from ..config import settings
from ..database import get_session
from ..logging_config import get_logger
from ..models import _now
from ..science import library as libsrc
from ..science import receptor as recep
from ..science import site as sitelib
from ..services import TargetContext
from ..targets import service as target_service
from ..tools import proc
from . import executors as ex
from . import params as P
from . import results

log = get_logger("campaign")

TERMINAL = ("COMPLETE", "FAILED", "CANCELLED")
WAITING = "AWAITING_REVIEW"
ACTIVE_BATCH = ("pending", "retrying")
RETRY_BACKOFF_SECONDS = 2.0  # multiplied by the attempt number, capped at 10 s


# ---------------------------------------------------------------------------
# Creation and events
# ---------------------------------------------------------------------------
def event(session: Session, job_id: str, stage: str, status: str, detail: str | None = None) -> None:
    session.add(models.JobStageLog(
        job_id=job_id, stage=stage, status=status, detail=detail,
        finished_at=_now() if status in ("SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED") else None,
    ))
    log.log(40 if status == "FAILED" else 20, "%s %s%s", stage, status, f": {detail}" if detail else "",
            extra={"job_id": job_id, "stage": stage, "status": status})


def create_campaign(session: Session, target: models.Target, params: dict, label: str | None) -> models.Job:
    p = P.resolve(params)
    if p["engine"] not in P.ENGINES:
        raise P.ParamError(f"engine must be one of {P.ENGINES}")
    has_ref = bool((p.get("site") or {}).get("reference"))
    if p["engine"] == "hpsee" and not P.is_csd(p) and not has_ref:
        raise P.ParamError("HPSee docking needs a reference ligand to define the binding site.")
    if P.is_csd(p):
        from . import hpsee_flow
        if not p.get("hpsee_space_id"):
            raise P.ParamError("Choose the HPSee Chemical Space to search.")
        hpsee_flow.definition_path(p.get("hpsee_docking_definition"), "flexx")
        hpsee_flow.definition_path(p.get("hpsee_scoring_definition"), "hydescorer")
    if p["enable_hyde"] and not has_ref:
        raise P.ParamError(
            "HYDE scoring needs a reference ligand; define the site from a co-crystallised ligand "
            "or turn HYDE scoring off."
        )
    if not p.get("site"):
        raise P.ParamError("A binding site is required.")
    if p["enable_grow"]:
        from ..tools import registry as _r
        if not _r.get("fastgrow").available:
            raise P.ParamError("Analogue generation needs BioSolveIT FastGrow, which is not available on this server.")
        if not settings.fastgrow_db and not _r.get("fastgrow_db_creator").available:
            raise P.ParamError("Analogue generation needs FastGrowDBCreator (or FASTGROW_DB pointing at a fragment database).")
    job = models.Job(target_id=target.id, status="QUEUED", label=label, params=p)
    session.add(job)
    session.flush()
    for pos, (stage, enabled) in enumerate(P.enabled_stages(p)):
        session.add(models.JobStage(
            job_id=job.id, stage=stage, position=pos,
            status="pending" if enabled else "skipped",
            detail=None if enabled else "Turned off in the campaign settings",
        ))
    event(session, job.id, "campaign", "QUEUED",
          f"engine={p['engine']} chemical_space={p['hpsee_space_name'] or p['hpsee_space_id']}" if P.is_csd(p)
          else f"engine={p['engine']} library={p['library_source']}:{p['library_count']}")
    return job


def _job_dir(job_id: str) -> Path:
    return storage.ensure_dir("runs", job_id)


def _safe_error(exc: BaseException, job_dir: Path | None = None) -> str:
    msg = f"{exc.__class__.__name__}: {exc}" if not isinstance(exc, proc.ToolError) else str(exc)
    if job_dir:
        msg = msg.replace(str(job_dir), "<run>")
    msg = msg.replace(str(settings.data_dir), "<data>")
    return msg[:1000]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel: dict[str, threading.Event] = {}
        self._active: set[str] = set()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        recovered = recover_interrupted()
        if recovered:
            log.warning("resuming %d interrupted campaign(s)", recovered)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="campaign-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for ev in list(self._cancel.values()):
            ev.set()  # unblocks running batches so their subprocesses are killed
        if self._thread:
            self._thread.join(timeout=15)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.error("runner tick failed: %s", traceback.format_exc())
            self._stop.wait(1.0)

    def _tick(self) -> None:
        with get_session() as s:
            flagged = s.execute(
                select(models.Job.id).where(models.Job.cancel_requested.is_(True), models.Job.status == "RUNNING")
            ).scalars().all()
            for jid in flagged:
                if jid in self._cancel:
                    self._cancel[jid].set()
            if self._active:
                return  # one campaign at a time
            job_id = s.execute(
                select(models.Job.id).where(models.Job.status == "QUEUED").order_by(models.Job.created_at).limit(1)
            ).scalar_one_or_none()
        if job_id is None:
            return
        with self._lock:
            self._active.add(job_id)
            self._cancel[job_id] = threading.Event()
        threading.Thread(target=self._run_job, args=(job_id,), name=f"campaign-{job_id[:6]}", daemon=True).start()

    # -- one campaign --------------------------------------------------------
    def _run_job(self, job_id: str) -> None:
        cancel = self._cancel[job_id]
        try:
            run_campaign(job_id, cancel, self._stop)
        except Exception as exc:  # noqa: BLE001
            log.error("campaign crashed: %s", traceback.format_exc(), extra={"job_id": job_id})
            with get_session() as s:
                job = s.get(models.Job, job_id)
                if job and job.status not in TERMINAL:
                    job.status = "FAILED"
                    job.failure_reason = _safe_error(exc)
                    job.finished_at = _now()
        finally:
            with self._lock:
                self._active.discard(job_id)
                self._cancel.pop(job_id, None)


runner = Runner()


def recover_interrupted() -> int:
    """After a crash/restart: nothing stays 'running' forever; finished work is kept."""
    proc.kill_orphans(str(settings.data_dir / "runs"))
    with get_session() as s:
        n = s.execute(
            update(models.Batch).where(models.Batch.status.in_(("running", "retrying")))
            .values(status="pending", error="Interrupted by an application restart; resuming.")
        ).rowcount
        jobs = s.execute(select(models.Job).where(models.Job.status == "RUNNING")).scalars().all()
        for job in jobs:
            job.status = "QUEUED"
            for st in s.execute(select(models.JobStage).where(
                models.JobStage.job_id == job.id, models.JobStage.status == "running")).scalars():
                st.status = "pending"
            event(s, job.id, "campaign", "RESUMED", "Application restarted; continuing from saved progress")
        return len(jobs) + (1 if n and not jobs else 0)


# ---------------------------------------------------------------------------
# Campaign orchestration
# ---------------------------------------------------------------------------
def run_campaign(job_id: str, cancel: threading.Event, shutdown: threading.Event) -> None:
    with get_session() as s:
        job = s.get(models.Job, job_id)
        job.status = "RUNNING"
        job.started_at = job.started_at or _now()
        job.cancel_requested = False
        job.failure_reason = None
        job.current_stage = None
        event(s, job_id, "campaign", "STARTED")
        stages = [(st.stage, st.status) for st in s.execute(
            select(models.JobStage).where(models.JobStage.job_id == job_id).order_by(models.JobStage.position)
        ).scalars()]

    for stage, status in stages:
        if status in ("succeeded", "skipped"):
            continue
        if cancel.is_set():
            return _finish(job_id, "CANCELLED", None)
        _set_stage(job_id, stage, "running", started=True)
        with get_session() as s:
            s.get(models.Job, job_id).current_stage = stage
        try:
            if stage == "prepare":
                ok = _stage_prepare(job_id)
            elif stage == "library":
                ok = _stage_library(job_id)
            elif stage == "rank":
                ok = _stage_rank(job_id)
            elif stage == "grow":
                ok = _stage_grow(job_id, cancel)
            elif stage == "csd_anchoring":
                from . import hpsee_flow
                ok = hpsee_flow.stage_anchoring(job_id, cancel, shutdown)
            elif stage == "csd_extension":
                from . import hpsee_flow
                ok = hpsee_flow.stage_extension(job_id, cancel, shutdown)
            else:
                ok = _stage_batched(job_id, stage, cancel, shutdown)
        except proc.ToolCancelled:
            if shutdown.is_set():
                return  # remote HPSee work keeps running; recovery re-attaches to it
            ok = "cancelled"
        except Exception as exc:  # noqa: BLE001 - stage-level failure
            from .hpsee_flow import Paused
            if isinstance(exc, Paused):
                return _pause(job_id, stage)
            log.error("stage %s failed: %s", stage, traceback.format_exc(), extra={"job_id": job_id, "stage": stage})
            reason = _safe_error(exc, _job_dir(job_id))
            _set_stage(job_id, stage, "failed", detail=reason, finish=True)
            return _finish(job_id, "FAILED", f"{P.STAGES[stage]['label']}: {reason}")
        if shutdown.is_set():
            return  # app stopping: leave state as-is; recovery will resume
        if ok == "cancelled":
            return _finish(job_id, "CANCELLED", None)
        if ok == "failed":
            return _finish(job_id, "FAILED", f"{P.STAGES[stage]['label']}: one or more batches failed. Inspect and retry them.")
    _finish(job_id, "COMPLETE", None)


def _pause(job_id: str, stage: str) -> None:
    with get_session() as s:
        job = s.get(models.Job, job_id)
        job.status, job.current_stage = WAITING, stage
        event(s, job_id, "campaign", WAITING, "Waiting for a person to review HPSee results and confirm the next step")


def _finish(job_id: str, status: str, reason: str | None) -> None:
    with get_session() as s:
        job = s.get(models.Job, job_id)
        job.status = status
        job.failure_reason = reason
        job.finished_at = _now()
        job.current_stage = job.current_stage if status != "COMPLETE" else None
        if status == "CANCELLED":
            for st in s.execute(select(models.JobStage).where(models.JobStage.job_id == job_id)).scalars():
                if st.status in ("running", "pending", "awaiting_review"):
                    st.status = "cancelled"
            s.execute(update(models.Batch).where(
                models.Batch.job_id == job_id, models.Batch.status.in_(("pending", "retrying", "running"))
            ).values(status="cancelled"))
        event(s, job_id, "campaign", status if status != "FAILED" else "FAILED", reason)


def _set_stage(job_id: str, stage: str, status: str, *, detail: str | None = None, started=False,
               finish=False, counts: tuple[int, int, int] | None = None, tool: str | None = None) -> None:
    with get_session() as s:
        st = s.execute(select(models.JobStage).where(
            models.JobStage.job_id == job_id, models.JobStage.stage == stage)).scalar_one()
        st.status = status
        if started:
            st.started_at = st.started_at or _now()
            st.finished_at = None
        if finish:
            st.finished_at = _now()
        if detail is not None:
            st.detail = detail
        if tool is not None:
            st.tool = tool
        if counts:
            st.count_in, st.count_out, st.count_failed = counts
        event(s, job_id, stage, {"running": "STARTED", "succeeded": "SUCCEEDED", "failed": "FAILED",
                                 "cancelled": "CANCELLED"}.get(status, status.upper()), detail)


# ---------------------------------------------------------------------------
# Stages without batches
# ---------------------------------------------------------------------------
def _stage_prepare(job_id: str) -> str:
    with get_session() as s:
        job = s.get(models.Job, job_id)
        p = P.resolve(job.params)
        target = s.get(models.Target, job.target_id)
        src = storage.resolve(target.structure_path) if target.structure_path else None
        fmt, chain = target.structure_format, p["chain"] or target.selected_chain
    if src is None or not src.exists():
        raise proc.ToolError("This target has no structure file on disk, so it cannot be docked.")
    prep = _job_dir(job_id) / "prepare"
    prep.mkdir(parents=True, exist_ok=True)
    pdb = prep / "input.pdb"
    if fmt == "mmcif":
        import gemmi
        st = gemmi.read_structure(str(src))
        st.setup_entities()
        st.write_pdb(str(pdb))
    else:
        pdb.write_bytes(src.read_bytes())
    site = p["site"]
    ref = sitelib.find_site(pdb, site["reference"]) if site.get("reference") else None
    n = recep.write_chain_pdb(pdb, prep / "protein.pdb", chain, ref)
    notes = [f"protein chain {chain or 'all'}: {n} atom records"]
    if ref:
        recep.write_reference_ligand(pdb, prep / "ref_ligand.sdf", ref)
        notes.append(f"reference ligand {ref.het_name} ({ref.heavy_atoms} heavy atoms)")
    tool = None
    if P.is_csd(p):
        from . import hpsee_flow
        flexx_def, hyde_def = hpsee_flow._csd_files(job_id)
        flexx_def.write_bytes(hpsee_flow.definition_path(p["hpsee_docking_definition"], "flexx").read_bytes())
        hyde_def.write_bytes(hpsee_flow.definition_path(p["hpsee_scoring_definition"], "hydescorer").read_bytes())
        notes.append("SeeSAR docking + scoring definitions copied with the campaign (they define HPSee's protein and pocket)")
    if p["engine"] == "vina":
        recep.prepare_vina_receptor(prep / "protein.pdb", prep, site["center"], site["size"])
        notes.append("Vina receptor: pH 7.4 hydrogens, heteroatoms removed, rigid")
        tool = "PDBFixer + Meeko"
    with get_session() as s:
        for f in prep.glob("*"):
            if f.name != "input.pdb" and f.suffix in (".pdb", ".sdf", ".pdbqt", ".flexx", ".hydescorer"):
                _artifact(s, job_id, "prepare", f)
    _set_stage(job_id, "prepare", "succeeded", detail="; ".join(notes), finish=True, tool=tool, counts=(1, 1, 0))
    return "ok"


def _artifact(s: Session, job_id: str, stage: str, path: Path, kind: str | None = None) -> None:
    stored = storage.to_stored(path)
    exists = s.execute(select(models.Artifact.id).where(
        models.Artifact.job_id == job_id, models.Artifact.storage_path == stored)).first()
    if not exists:
        s.add(models.Artifact(job_id=job_id, stage=stage, kind=kind or path.suffix.lstrip("."), storage_path=stored))


def _stage_library(job_id: str) -> str:
    with get_session() as s:
        job = s.get(models.Job, job_id)
        p = P.resolve(job.params)
        have = s.execute(select(func.count()).select_from(models.Molecule).where(
            models.Molecule.job_id == job_id, models.Molecule.outcome != "generated")).scalar()
        run_tag = (job.params.get("run_tag") or job.id[:4]).upper()
    if have:  # resume: library already fixed for this campaign
        _set_stage(job_id, "library", "succeeded", counts=(have, have, 0), finish=True)
        return "ok"
    filters = libsrc.Filters.from_params(p)
    n = p["library_count"]
    if p["library_source"] == "zinc20":
        mols, meta = libsrc.fetch_zinc20(n, p["library_seed"], filters)
    elif p["library_source"] == "demo":
        mols, meta = libsrc.demo_library(n, filters)
    else:
        mols, meta = libsrc.parse_upload(p.get("library_upload") or "", n, filters)
    d = _job_dir(job_id) / "library"
    d.mkdir(exist_ok=True)
    with get_session() as s:
        for i, rec in enumerate(mols, start=1):
            s.add(models.Molecule(
                job_id=job_id, display_id=f"MDP-{run_tag}-{i:04d}", smiles=rec["smiles"],
                inchikey=rec["inchikey"], source=rec["source"], external_id=rec["external_id"],
                source_stage="library",
            ))
        s.flush()
        (d / "library.smi").write_text("".join(f"{r['smiles']}\t{r['external_id']}\n" for r in mols))
        import json
        (d / "library_meta.json").write_text(json.dumps(meta, indent=2))
        _artifact(s, job_id, "library", d / "library.smi", "smi")
        _artifact(s, job_id, "library", d / "library_meta.json", "json")
    rej = meta["rejected"]
    detail = (
        f"{meta['kept']} molecules kept from {meta['scanned']} read ({meta['source']}); "
        f"{rej['invalid_or_filtered']} filtered out, {rej['duplicate']} duplicates. Filters: {meta['filters']}."
    )
    if meta["source"] == "zinc20":
        detail += " ZINC entries are catalogue compounds, not novel molecules."
    _set_stage(job_id, "library", "succeeded", detail=detail, counts=(meta["scanned"], meta["kept"], 0), finish=True,
               tool="ZINC20" if meta["source"] == "zinc20" else meta["source"])
    return "ok"


def _stage_rank(job_id: str) -> str:
    with get_session() as s:
        job = s.get(models.Job, job_id)
        out = results.write_run_outputs(s, job)
        n = s.execute(select(func.count()).select_from(models.Molecule).where(
            models.Molecule.job_id == job_id, models.Molecule.outcome == "active")).scalar()
    _set_stage(job_id, "rank", "succeeded", detail=f"{n} ranked candidate(s)", counts=(n, n, 0), finish=True)
    return "ok"


def _stage_grow(job_id: str, cancel: threading.Event) -> str:
    """FastGrow: grow new analogues from the top-ranked candidates' poses (generated, not catalogue)."""
    from rdkit import Chem
    from rdkit.Chem.MolStandardize import rdMolStandardize
    from ..science.engines import fastgrow as fg

    with get_session() as s:
        job = s.get(models.Job, job_id)
        p = P.resolve(job.params)
        ranked = results.ranked_candidates(s, job)[: p["grow_parents"]]
        parents = []
        for mol, by in ranked:
            rel = by.get("hyde", {}).get("pose_path") or by.get("docking", {}).get("pose_path")
            if rel:
                parents.append((mol.id, mol.display_id, mol.smiles, rel))
        library = [m for (m,) in s.execute(select(models.Molecule.smiles).where(
            models.Molecule.job_id == job_id, models.Molecule.outcome != "generated"))]
    if not parents:
        _set_stage(job_id, "grow", "succeeded", finish=True, counts=(0, 0, 0),
                   detail="No ranked candidate with a 3D pose to grow from.")
        return "ok"
    job_dir = _job_dir(job_id)
    gdir = job_dir / "grow"
    (gdir / "poses").mkdir(parents=True, exist_ok=True)
    protein = job_dir / "prepare" / "protein.pdb"
    threads = settings.cpu_budget
    if settings.fastgrow_db:
        db, n_frag, db_note = Path(settings.fastgrow_db), None, "supplied fragment database"
    else:
        pool, pool_note = fg.fragment_pool(library)
        db, n_frag = fg.build_fragment_db(pool, settings.data_dir / "fastgrow_cache", gdir / "dbcreator.log", threads)
        db_note = f"{n_frag} BRICS fragments from {pool_note}"
    uncharger = rdMolStandardize.Uncharger()
    library_keys = {Chem.MolToInchiKey(Chem.MolFromSmiles(x)) for x in library if Chem.MolFromSmiles(x)}
    seen: set[str] = set(library_keys)
    made = 0
    notes: list[str] = []
    for pid, pname, psmi, rel in parents:
        if cancel.is_set():
            return "cancelled"
        pose_rec = (job_dir / rel).read_text()
        from ..science import sdf as sdflib
        recs = sdflib.parse_sdf(pose_rec)
        pm = Chem.MolFromMolBlock(recs[0].molblock, removeHs=False)
        if pm is None:
            notes.append(f"{pname}: pose unreadable")
            continue
        pm = Chem.AddHs(pm, addCoords=True)
        vectors = fg.choose_exit_vectors(pm, protein, n=2)
        if not vectors:
            notes.append(f"{pname}: no open growth position found")
            continue
        for vi, ev in enumerate(vectors, start=1):
            core = fg.core_with_linker(pm, ev)
            core_path = gdir / f"{pname}_core{vi}.sdf"
            w = Chem.SDWriter(str(core_path)); w.write(core); w.close()
            try:
                grown = fg.grow(core_path, protein, db, gdir / f"{pname}_grown{vi}.sdf", gdir / f"{pname}_fastgrow.log",
                                p["grow_per_parent"], threads, float(settings.batch_timeout_seconds), cancel)
            except proc.ToolCancelled:
                return "cancelled"
            except proc.ToolError as exc:
                notes.append(f"{pname} vector {vi}: {exc}")
                continue
            if not grown:
                notes.append(f"{pname} vector {vi}: FastGrow found no fitting fragment")
            with get_session() as s:
                for g in grown:
                    m = Chem.MolFromSmiles(g.smiles)
                    if m is None:
                        continue
                    m = uncharger.uncharge(m)
                    key = Chem.MolToInchiKey(m)
                    if key in seen:
                        continue
                    seen.add(key)
                    made += 1
                    did = f"{pname}-G{made:03d}"
                    smi = Chem.MolToSmiles(m)
                    from ..stages import sa_scoring
                    sa = sa_scoring.score_molecule(smi)
                    pose_path = gdir / "poses" / f"{did}.sdf"
                    grec = sdflib.parse_sdf(g.pose_text)[0]
                    block = did + grec.molblock[grec.molblock.index("\n"):]   # name the record after the molecule
                    pose_path.write_text(sdflib.with_props(block, {
                        "MOLECULE_ID": did, "ORIGIN": "GENERATED analogue (FastGrow), not a catalogue compound",
                        "PARENT_ID": pname, "FASTGROW_OPTIMIZED_SCORE": g.score, "SA_SCORE": sa["sa_score"],
                        "SMILES": smi}))
                    mol = models.Molecule(job_id=job_id, display_id=did, smiles=smi, inchikey=key, source="fastgrow",
                                          external_id=pname, source_stage="grow", outcome="generated", exit_stage="grow")
                    s.add(mol); s.flush()
                    s.add(models.StageResult(molecule_id=mol.id, job_id=job_id, stage="grow", payload={
                        "parent_id": pid, "parent_display_id": pname, "parent_smiles": psmi,
                        "fastgrow_score": g.score, "heavy_atoms": g.heavy_atoms, "sa_score": sa["sa_score"],
                        "exit_vector": {"hydrogen_index": ev.h_index, "attached_to": ev.parent_index,
                                        "protein_contacts_6A": ev.contacts_6A, "free_space_A": round(ev.nearest_A, 2)},
                        "pose_path": pose_path.relative_to(job_dir).as_posix(), "fragment_source": db_note,
                        "note": "Generated suggestion: not synthesised, not validated; score is FastGrow's, not an affinity."}))
    with get_session() as s:
        all_poses = sorted((gdir / "poses").glob("*.sdf"))
        if all_poses:
            out = gdir / "generated_analogues.sdf"
            out.write_text("".join(f.read_text() for f in all_poses))
            _artifact(s, job_id, "grow", out, "sdf_output")
    detail = f"{made} new analogue(s) from {len(parents)} parent(s) using {db_note}." + (" " + "; ".join(notes) if notes else "")
    _set_stage(job_id, "grow", "succeeded", detail=detail, counts=(len(parents), made, 0), finish=True,
               tool="BioSolveIT FastGrow")
    return "ok"


# ---------------------------------------------------------------------------
# Batched stages
# ---------------------------------------------------------------------------
def _stage_input_ids(s: Session, job_id: str) -> list[str]:
    return list(s.execute(
        select(models.Molecule.id).where(models.Molecule.job_id == job_id, models.Molecule.outcome == "active")
        .order_by(models.Molecule.display_id)
    ).scalars())


def _stage_batched(job_id: str, stage: str, cancel: threading.Event, shutdown: threading.Event) -> str:
    with get_session() as s:
        job = s.get(models.Job, job_id)
        p = P.resolve(job.params)
        existing = s.execute(select(func.count()).select_from(models.Batch).where(
            models.Batch.job_id == job_id, models.Batch.stage == stage)).scalar()
        empty = False
        if not existing:
            ids = _stage_input_ids(s, job_id)
            if not ids:
                empty = True
            size = p["batch_size"]
            if _remote(p, stage):
                # One HPSee workflow per batch: HPSee parallelises internally, and FlexX returns up to
                # 10 poses per molecule, so a batch is bounded by how many entries we may download.
                size = max(1, settings.hpsee_max_fetch_entries // 10)
            for i in range(0, len(ids), size):
                s.add(models.Batch(job_id=job_id, stage=stage, index=i // size + 1, molecule_ids=ids[i:i + size]))
            event(s, job_id, stage, "PLANNED", f"{len(ids)} molecules in {-(-len(ids) // size)} batch(es) of up to {size}")
    if empty:
        _set_stage(job_id, stage, "succeeded", finish=True, counts=(0, 0, 0),
                   detail="No molecules reached this stage: earlier filters removed them all.")
        return "ok"
    tool = _tool_label(p, stage)
    _set_stage(job_id, stage, "running", started=True, tool=tool)

    parallel = 1 if stage == "admet" or _remote(p, stage) else settings.max_parallel_batches
    while True:
        with get_session() as s:
            todo = list(s.execute(select(models.Batch.id).where(
                models.Batch.job_id == job_id, models.Batch.stage == stage,
                models.Batch.status.in_(ACTIVE_BATCH)).order_by(models.Batch.index)).scalars())
        if not todo or cancel.is_set() or shutdown.is_set():
            break
        with cf.ThreadPoolExecutor(max_workers=parallel, thread_name_prefix=f"batch-{stage}") as pool:
            futs = [pool.submit(_execute_batch, job_id, stage, bid, p, parallel, cancel, shutdown) for bid in todo]
            for f in futs:
                f.result()
        with get_session() as s:
            retrying = s.execute(select(func.max(models.Batch.attempt)).where(
                models.Batch.job_id == job_id, models.Batch.stage == stage, models.Batch.status == "retrying")).scalar()
        if retrying:
            time.sleep(min(RETRY_BACKOFF_SECONDS * retrying, 10.0))

    with get_session() as s:
        statuses = [b for b in s.execute(select(models.Batch.status).where(
            models.Batch.job_id == job_id, models.Batch.stage == stage)).scalars()]
    if shutdown.is_set():
        return "ok"
    if cancel.is_set() or "cancelled" in statuses:
        return "cancelled"
    if any(x in ("failed", "pending", "retrying", "running") for x in statuses):
        _refresh_stage_counts(job_id, stage, "failed")
        return "failed"
    summary = _apply_filter(job_id, stage, p)
    if stage == "hyde":
        _write_hyde_extractor_sdf(job_id)
    _refresh_stage_counts(job_id, stage, "succeeded", detail=summary)
    return "ok"


def _write_hyde_extractor_sdf(job_id: str) -> None:
    """One SDF of every HYDE-optimised pose, exactly as hydescorer wrote it (atom scores included), so it
    can be opened in the company's HYDE SDF Extractor (HYDE_SCORER.html)."""
    poses = sorted((_job_dir(job_id) / "hyde" / "poses").glob("*.sdf"))
    if not poses:
        return
    out = _job_dir(job_id) / "hyde" / "hyde_scored_all.sdf"
    out.write_text("".join(f.read_text() for f in poses))
    with get_session() as s:
        _artifact(s, job_id, "hyde", out, "sdf_output")


def _remote(p: dict, stage: str) -> bool:
    """Is this batched stage executed by HPSee rather than on this machine?"""
    return p.get("engine") == "hpsee" and stage in ("docking", "hyde")


def _batch_timeout(p: dict, stage: str) -> int:
    # HPSee enforces its own workflow deadline (hpsee_flow.wait); the batch watchdog sits just above it.
    return settings.hpsee_workflow_timeout_seconds + 300 if _remote(p, stage) else settings.batch_timeout_seconds


def _tool_label(p: dict, stage: str) -> str | None:
    from ..tools import registry
    if _remote(p, stage):
        from ..science.engines import hpsee as H
        v = H.status().version or ""
        return f"BioSolveIT {'FlexX' if stage == 'docking' else 'HYDE'} on HPSee {v}".strip()
    if stage == "docking":
        st = registry.get(p["engine"])
        return f"{st.name} {st.version or ''}".strip()
    if stage == "hyde":
        st = registry.get("hydescorer")
        return f"{st.name} {st.version or ''}".strip()
    if stage == "sa":
        from ..stages import sa_scoring
        return sa_scoring.active_method()
    if stage == "admet":
        from ..science import admet
        return admet.MODEL_NAME
    return "mocked placeholder" if P.STAGES[stage]["mocked"] else None


def _refresh_stage_counts(job_id: str, stage: str, status: str, detail: str | None = None) -> None:
    with get_session() as s:
        total = s.execute(select(models.Batch.molecule_ids).where(
            models.Batch.job_id == job_id, models.Batch.stage == stage)).scalars().all()
        n_in = sum(len(x) for x in total)
        failed = s.execute(select(func.count()).select_from(models.StageResult).where(
            models.StageResult.job_id == job_id, models.StageResult.stage == stage,
            models.StageResult.status == "failed")).scalar()
        out = s.execute(select(func.count()).select_from(models.Molecule).where(
            models.Molecule.job_id == job_id, models.Molecule.outcome == "active")).scalar()
    _set_stage(job_id, stage, status, counts=(n_in, out, failed), finish=status != "running", detail=detail)


def _execute_batch(job_id: str, stage: str, batch_id: str, p: dict, parallel: int,
                   job_cancel: threading.Event, shutdown: threading.Event) -> None:
    # Claim: only one executor can move a batch to 'running'.
    with get_session() as s:
        claimed = s.execute(update(models.Batch).where(
            models.Batch.id == batch_id, models.Batch.status.in_(ACTIVE_BATCH)
        ).values(status="running", attempt=models.Batch.attempt + 1, started_at=_now(), finished_at=None,
                 error=None, ok_count=0, failed_count=0)).rowcount
        if not claimed:
            return
        b = s.get(models.Batch, batch_id)
        attempt, index, mol_ids = b.attempt, b.index, list(b.molecule_ids)
        mols = [ex.MolRef(m.id, m.display_id, m.smiles) for m in s.execute(
            select(models.Molecule).where(models.Molecule.id.in_(mol_ids)).order_by(models.Molecule.display_id)).scalars()]
        prior = results.stage_payloads(s, job_id, mol_ids)
        target = s.get(models.Target, s.get(models.Job, job_id).target_id)
        tctx = TargetContext(target.id, target.name, target.pdb_id, target.selected_chain, target.checksum,
                             target.structure_path, target.residue_count, target.atom_count)
        event(s, job_id, stage, "BATCH_STARTED", f"batch {index} attempt {attempt}")
    job_dir = _job_dir(job_id)
    batch_dir = job_dir / stage / f"batch_{index:03d}"
    log_path = batch_dir / f"attempt_{attempt}.log"
    batch_dir.mkdir(parents=True, exist_ok=True)
    with get_session() as s:
        s.get(models.Batch, batch_id).log_path = storage.to_stored(log_path)

    cpu = max(1, settings.cpu_budget // max(1, parallel))
    ctx = ex.Ctx(job_id, p, job_dir, tctx, prior, cpu, batch_id=batch_id, shutdown=shutdown)
    timeout_s = _batch_timeout(p, stage)
    local_cancel = threading.Event()
    timed_out = threading.Event()
    done = threading.Event()

    def watch():
        deadline = time.monotonic() + timeout_s
        while not done.wait(0.5):
            if job_cancel.is_set() or shutdown.is_set():
                local_cancel.set()
                return
            if time.monotonic() > deadline:
                timed_out.set()
                local_cancel.set()
                return
    threading.Thread(target=watch, daemon=True).start()

    def progress(ok: int, failed: int) -> None:
        with get_session() as s:
            b = s.get(models.Batch, batch_id)
            b.ok_count, b.failed_count = ok, failed

    started = time.monotonic()
    try:
        outcomes = ex.call(stage, ctx, mols, batch_dir, log_path, local_cancel, progress, float(timeout_s))
        _persist_outcomes(job_id, stage, batch_id, mol_ids, outcomes, time.monotonic() - started)
    except proc.ToolCancelled:
        if shutdown.is_set():  # application stopping: resume this batch on next start
            _end_batch(batch_id, "pending", "Interrupted by application shutdown; will resume.",
                       time.monotonic() - started, attempt, job_id, stage, index)
            return
        _end_batch(batch_id, "failed" if timed_out.is_set() else "cancelled",
                   f"Timed out after {timeout_s}s" if timed_out.is_set() else "Cancelled",
                   time.monotonic() - started, attempt, job_id, stage, index)
    except proc.ToolTimeout as exc:
        _end_batch(batch_id, "retrying" if attempt < settings.max_batch_attempts else "failed",
                   _safe_error(exc, job_dir), time.monotonic() - started, attempt, job_id, stage, index)
    except Exception as exc:  # noqa: BLE001 - batch-level failure
        log.error("batch failed: %s", traceback.format_exc(), extra={"job_id": job_id, "stage": stage})
        tail = getattr(exc, "tail", "") or ""
        msg = _safe_error(exc, job_dir) + (f" | last output: {tail.strip().splitlines()[-1][:200]}" if tail.strip() else "")
        _end_batch(batch_id, "retrying" if attempt < settings.max_batch_attempts else "failed",
                   msg, time.monotonic() - started, attempt, job_id, stage, index)
    finally:
        done.set()


def _end_batch(batch_id, status, error, seconds, attempt, job_id, stage, index) -> None:
    with get_session() as s:
        b = s.get(models.Batch, batch_id)
        b.status, b.error, b.seconds, b.finished_at = status, error, round(seconds, 2), _now()
        event(s, job_id, stage, "BATCH_" + status.upper(), f"batch {index} attempt {attempt}: {error}")


def _persist_outcomes(job_id: str, stage: str, batch_id: str, mol_ids: list[str],
                      outcomes: list[ex.MolOutcome], seconds: float) -> None:
    mocked = P.STAGES[stage]["mocked"]
    with get_session() as s:
        s.execute(delete(models.StageResult).where(
            models.StageResult.job_id == job_id, models.StageResult.stage == stage,
            models.StageResult.molecule_id.in_(mol_ids)))
        for o in outcomes:
            s.add(models.StageResult(
                molecule_id=o.molecule_id, job_id=job_id, stage=stage, payload=o.payload,
                is_mocked=mocked, status="succeeded" if o.ok else "failed", error=o.error, batch_id=batch_id))
        b = s.get(models.Batch, batch_id)
        b.ok_count, b.failed_count = sum(o.ok for o in outcomes), sum(not o.ok for o in outcomes)
        b.status, b.error, b.seconds, b.finished_at = "succeeded", None, round(seconds, 2), _now()
        event(s, job_id, stage, "BATCH_SUCCEEDED",
              f"batch {b.index}: {b.ok_count} ok, {b.failed_count} failed in {seconds:.1f}s")


# ---------------------------------------------------------------------------
# Funnel filters (explicit, recomputed from scratch so they are idempotent)
# ---------------------------------------------------------------------------
def _apply_filter(job_id: str, stage: str, p: dict) -> str:
    with get_session() as s:
        mols = {m.id: m for m in s.execute(select(models.Molecule).where(models.Molecule.job_id == job_id)).scalars()}
        # Re-open everyone this stage may have excluded earlier, then decide again.
        pool = [m for m in mols.values() if m.outcome == "active" or m.exit_stage == stage]
        for m in pool:
            m.outcome, m.exit_stage, m.exit_reason = "active", None, None
        rows = s.execute(select(models.StageResult).where(
            models.StageResult.job_id == job_id, models.StageResult.stage == stage,
            models.StageResult.molecule_id.in_([m.id for m in pool]))).scalars().all()
        res = {r.molecule_id: r for r in rows}
        drop: dict[str, str] = {}
        for m in pool:
            r = res.get(m.id)
            if r is None or r.status == "failed":
                drop[m.id] = f"{P.STAGES[stage]['label']} failed: {(r.error if r else 'no result') or 'unknown error'}"
        ok_ids = [m.id for m in pool if m.id not in drop]
        summary = ""
        if stage == "docking":
            order = sorted(ok_ids, key=lambda i: (res[i].payload["score"], mols[i].display_id))
            keep_n = max(1, round(len(order) * p["dock_keep_fraction"])) if order else 0
            for rank, i in enumerate(order, start=1):
                if rank > keep_n:
                    drop[i] = f"Docking score ranked {rank} of {len(order)}; only the best {keep_n} ({p['dock_keep_fraction']:.0%}) continue"
            summary = f"Kept the best {keep_n} of {len(order)} docked molecules ({p['dock_keep_fraction']:.0%} rule)."
        elif stage == "sa":
            for i in ok_ids:
                sc = res[i].payload["sa_score"]
                res[i].payload = {**res[i].payload, "passed": sc <= p["sa_threshold"], "threshold": p["sa_threshold"]}
                if sc > p["sa_threshold"]:
                    drop[i] = f"SA score {sc} is above the limit of {p['sa_threshold']} (harder to make)"
            summary = f"SA score must be at most {p['sa_threshold']}."
        elif stage == "admet":
            limit = 0 if p["strict_admet_gate"] else 1
            for i in ok_ids:
                nf = res[i].payload["n_flags"]
                res[i].payload = {**res[i].payload, "passed": nf <= limit, "gate_limit": limit}
                if nf > limit:
                    drop[i] = f"{nf} toxicity alert(s): {', '.join(res[i].payload['tox_flags'])} (allowed: {limit})"
            summary = f"Allowed at most {limit} predicted toxicity alert(s) (AMES, hERG, DILI, ClinTox, carcinogenicity; probability ≥ 0.5 and riskier than 90% of approved drugs)."
        for m in pool:
            if m.id in drop:
                failed = res.get(m.id) is None or res[m.id].status == "failed"
                m.outcome = "failed" if failed else "filtered"
                m.exit_stage, m.exit_reason = stage, drop[m.id]
        event(s, job_id, stage, "FILTERED", f"{len(pool)} in -> {len(pool) - len(drop)} out")
    return summary


# ---------------------------------------------------------------------------
# User controls: cancel, retry, resume
# ---------------------------------------------------------------------------
class ControlError(ValueError):
    pass


def request_cancel(session: Session, job: models.Job) -> None:
    if job.status in TERMINAL:
        raise ControlError("This campaign has already finished.")
    if job.status in ("QUEUED", WAITING):
        job.status = "CANCELLED"
        job.finished_at = _now()
        for st in session.execute(select(models.JobStage).where(models.JobStage.job_id == job.id)).scalars():
            if st.status == "pending":
                st.status = "cancelled"
        for st in session.execute(select(models.JobStage).where(
                models.JobStage.job_id == job.id, models.JobStage.status == "awaiting_review")).scalars():
            st.status = "cancelled"
        event(session, job.id, "campaign", "CANCELLED",
              "Cancelled while waiting for review" if job.current_stage == "csd_extension" else "Cancelled before starting")
    else:
        job.cancel_requested = True
        event(session, job.id, "campaign", "CANCEL_REQUESTED")


def resume(session: Session, job: models.Job) -> None:
    """Continue a failed or cancelled campaign; succeeded work is kept."""
    if job.status not in ("FAILED", "CANCELLED"):
        raise ControlError("Only failed or cancelled campaigns can be resumed.")
    for st in session.execute(select(models.JobStage).where(models.JobStage.job_id == job.id)).scalars():
        if st.status in ("failed", "cancelled"):
            st.status = "pending"
    session.execute(update(models.Batch).where(
        models.Batch.job_id == job.id, models.Batch.status == "cancelled").values(status="pending", attempt=0))
    job.status, job.cancel_requested, job.failure_reason, job.finished_at = "QUEUED", False, None, None
    event(session, job.id, "campaign", "RESUMED", "Resumed by user")


def retry_batches(session: Session, job: models.Job, batch_ids: list[str] | None = None,
                  failed_molecules: bool = False) -> int:
    """Re-run failed batches (selected or all) and/or molecules that failed inside batches.

    Downstream stages are reset, since their inputs change; stages upstream
    and unrelated batches are untouched. Returns the number of batches queued.
    """
    if job.status in ("RUNNING", "QUEUED", WAITING):
        raise ControlError("Wait for the campaign to stop (or cancel it) before retrying.")
    order = {s.stage: s.position for s in session.execute(
        select(models.JobStage).where(models.JobStage.job_id == job.id)).scalars()}
    q = select(models.Batch).where(models.Batch.job_id == job.id)
    if batch_ids:
        q = q.where(models.Batch.id.in_(batch_ids))
    batches = session.execute(q).scalars().all()
    if batch_ids and len(batches) != len(set(batch_ids)):
        raise ControlError("One or more selected batches do not belong to this campaign.")
    touched: list[models.Batch] = []
    for b in batches:
        if b.status in ("failed", "cancelled"):
            b.status, b.attempt, b.error = "pending", 0, None
            touched.append(b)
    new_batches: list[models.Batch] = []
    if failed_molecules:
        by_stage: dict[str, list[str]] = {}
        for r in session.execute(select(models.StageResult).where(
                models.StageResult.job_id == job.id, models.StageResult.status == "failed")).scalars():
            if not batch_ids or r.batch_id in batch_ids:
                by_stage.setdefault(r.stage, []).append(r.molecule_id)
        for stage, ids in by_stage.items():
            last = session.execute(select(func.max(models.Batch.index)).where(
                models.Batch.job_id == job.id, models.Batch.stage == stage)).scalar() or 0
            nb = models.Batch(job_id=job.id, stage=stage, index=last + 1, molecule_ids=ids)
            session.add(nb)
            new_batches.append(nb)
            for m in session.execute(select(models.Molecule).where(models.Molecule.id.in_(ids))).scalars():
                m.outcome, m.exit_stage, m.exit_reason = "active", None, None
    affected = {b.stage for b in touched} | {b.stage for b in new_batches}
    if not affected:
        raise ControlError("Nothing to retry: there are no failed batches or molecules.")
    first = min(order[s] for s in affected)
    for st in session.execute(select(models.JobStage).where(models.JobStage.job_id == job.id)).scalars():
        if st.position > first and st.status != "skipped":
            _reset_stage(session, job.id, st)
        elif st.stage in affected:
            st.status = "pending"
    job.status, job.cancel_requested, job.failure_reason, job.finished_at = "QUEUED", False, None, None
    event(session, job.id, "campaign", "RETRY", f"{len(touched) + len(new_batches)} batch(es) queued for retry")
    return len(touched) + len(new_batches)


def _reset_stage(session: Session, job_id: str, st: models.JobStage) -> None:
    """Discard a downstream stage's work so it recomputes from fresh inputs."""
    if st.stage in ("prepare", "library"):
        return
    if st.stage == "grow":  # generated molecules are products of the stage: delete, never "reactivate"
        gen = [m.id for m in session.execute(select(models.Molecule).where(
            models.Molecule.job_id == job_id, models.Molecule.outcome == "generated")).scalars()]
        if gen:
            session.execute(delete(models.StageResult).where(models.StageResult.molecule_id.in_(gen)))
            session.execute(delete(models.Molecule).where(models.Molecule.id.in_(gen)))
        st.status, st.count_in, st.count_out, st.count_failed, st.finished_at = "pending", 0, 0, 0, None
        return
    session.execute(delete(models.StageResult).where(
        models.StageResult.job_id == job_id, models.StageResult.stage == st.stage))
    session.execute(delete(models.Batch).where(models.Batch.job_id == job_id, models.Batch.stage == st.stage))
    for m in session.execute(select(models.Molecule).where(
            models.Molecule.job_id == job_id, models.Molecule.exit_stage == st.stage)).scalars():
        m.outcome, m.exit_stage, m.exit_reason = "active", None, None
    st.status, st.count_in, st.count_out, st.count_failed, st.finished_at = "pending", 0, 0, 0, None
