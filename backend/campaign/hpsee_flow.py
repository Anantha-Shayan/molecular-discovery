"""
BioSolveIT HPSee inside the campaign engine.

Two uses, both selected with engine="hpsee":

* hpsee_mode="library" - remote replacement for local FlexX + HYDE. The docking
  and HYDE stages stay ordinary batched stages; each batch becomes ONE HPSee
  workflow (Dockings/StandardInput, then Scorings/StandardInput).

* hpsee_mode="chemical_space" - Chemical Space Docking (C-S-D):
      csd_anchoring  create the C-S-D project, run Anchoring, store the docked anchors
      csd_extension  PAUSE until a person reviews the anchors and confirms which to
                     extend; run Extension; if the results still carry open attachment
                     points (3-component spaces) pause again for the next round; finally
                     import the complete, docked and scored products as campaign molecules.

Durability (same guarantees as local batches):
  * an HPSeeRun row is written BEFORE each submission and carries a marker
    `[mdp:<run id>]` in the HPSee workflow description; after a crash between
    "submit" and "store id" the workflow is found again by that marker, so it is
    never submitted twice;
  * after a restart a running workflow is re-attached and polled, not resubmitted;
  * user cancel deletes the HPSee workflow (HPSee drops queued jobs; jobs already
    running on a worker finish - HPSee has no hard kill); an application shutdown
    leaves it running so it can be re-attached;
  * a workflow deadline (HPSEE_WORKFLOW_TIMEOUT_SECONDS) is measured from the
    stored submission time, so restarts do not extend it.

Nothing here fabricates progress: per-task status/progress is copied from HPSee.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from typing import Callable

from sqlalchemy import func, select

from .. import models, storage
from ..config import settings
from ..database import get_session
from ..logging_config import get_logger
from ..models import _now
from ..science import sdf as sdflib
from ..science.engines import hpsee as H
from ..science.engines.common import DockInput, DockResult, DockSettings, write_3d_library
from ..science.engines.flexx import parse_flexx_output
from ..science.engines.hyde import HydeScore, parse_hyde_output
from ..tools import proc
from . import params as P

log = get_logger("hpsee")

UNREACHABLE_GRACE_SECONDS = 15 * 60   # tolerate an HPSee outage this long while polling
# Run states after which the remote workflow is finished and its results are stored locally.
DONE_STATES = ("succeeded", "awaiting_review", "reviewed", "extended")


class Paused(Exception):
    """The stage is waiting for a person (anchor review)."""


# ---------------------------------------------------------------------------
# Run rows and remote workflow lifecycle
# ---------------------------------------------------------------------------
def _marker(run_id: str) -> str:
    return f"[mdp:{run_id}]"


def _client() -> H.HPSeeClient:
    return H.HPSeeClient()


def _log_line(log_path: Path | None, text: str) -> None:
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")


def _update_run(run_id: str, **values) -> None:
    with get_session() as s:
        run = s.get(models.HPSeeRun, run_id)
        for k, v in values.items():
            setattr(run, k, v)


def _run_snapshot(run_id: str) -> models.HPSeeRun:
    with get_session() as s:
        run = s.get(models.HPSeeRun, run_id)
        s.expunge(run)
        return run


def new_run(job_id: str, kind: str, *, batch_id: str | None = None, round: int = 0, csd_id: str | None = None,
            selected: list[int] | None = None) -> str:
    with get_session() as s:
        run = models.HPSeeRun(job_id=job_id, kind=kind, batch_id=batch_id, round=round, csd_id=csd_id,
                              status="submitting", selected_entry_ids=selected)
        s.add(run)
        s.flush()
        return run.id


def submit_or_adopt(run_id: str, client: H.HPSeeClient, submit: Callable[[str], dict], label: str,
                    log_path: Path | None = None, known_workflow: Callable[[], str | None] | None = None) -> str:
    """Return the HPSee workflow id of this run, submitting it only if HPSee has never seen it."""
    run = _run_snapshot(run_id)
    if run.workflow_id:
        return run.workflow_id
    marker = _marker(run_id)
    adopted = known_workflow() if known_workflow else None
    if not adopted:
        for wf in client.recent_workflows(200):
            if marker in (wf.get("description") or ""):
                adopted = wf["workflowId"]
                break
    if adopted:
        _log_line(log_path, f"re-attached to HPSee workflow {adopted} (submitted before a restart)")
        _update_run(run_id, workflow_id=adopted, status="running", submitted_at=run.submitted_at or _now())
        return adopted
    info = submit(f"{label} {marker}")
    wid = info["workflowId"]
    _update_run(run_id, workflow_id=wid, status="running", submitted_at=_now(), hpsee_status=info.get("status"),
                hpsee_status_text=info.get("statusDescription"), tasks=H.summarise_tasks(info))
    _log_line(log_path, f"submitted HPSee workflow {wid} ({label})")
    return wid


def wait(run_id: str, client: H.HPSeeClient, cancel: threading.Event, shutdown: threading.Event | None,
         log_path: Path | None = None) -> dict:
    """Poll until the workflow is terminal. Raises ToolCancelled on cancel/shutdown/deadline."""
    run = _run_snapshot(run_id)
    wid = run.workflow_id
    started = (run.submitted_at or _now())
    deadline = started + dt.timedelta(seconds=settings.hpsee_workflow_timeout_seconds)
    last_seen, unreachable_since = None, None
    while True:
        if cancel.is_set():
            if shutdown is not None and shutdown.is_set():
                _log_line(log_path, "application stopping; HPSee keeps running and will be re-attached")
                raise proc.ToolCancelled("Interrupted by application shutdown")
            _cancel_remote(run_id, client, wid, "Cancelled by user", log_path)
            raise proc.ToolCancelled("Cancelled by user")
        if _now() > deadline:
            _cancel_remote(run_id, client, wid, f"No result after {settings.hpsee_workflow_timeout_seconds}s "
                           "(HPSEE_WORKFLOW_TIMEOUT_SECONDS)", log_path)
            raise proc.ToolTimeout(f"HPSee workflow {wid} did not finish within "
                                   f"{settings.hpsee_workflow_timeout_seconds}s")
        try:
            info = client.workflow(wid)
            unreachable_since = None
        except H.HPSeeError as exc:
            if exc.category != "unreachable":
                raise
            unreachable_since = unreachable_since or time.monotonic()
            _update_run(run_id, error=f"Polling paused - {exc}")
            if time.monotonic() - unreachable_since > UNREACHABLE_GRACE_SECONDS:
                raise
            _sleep(cancel, settings.hpsee_poll_interval_seconds)
            continue
        tasks = H.summarise_tasks(info)
        snap = (info.get("status"), [(t["type"], t["status"], t["progress"]) for t in tasks])
        if snap != last_seen:
            last_seen = snap
            _update_run(run_id, hpsee_status=info.get("status"), hpsee_status_text=info.get("statusDescription"),
                        tasks=tasks, error=None)
            _log_line(log_path, f"workflow {wid}: {info.get('statusDescription')} | " + ", ".join(
                f"{t['type']} {t['status_text']} {t['progress']}%" for t in tasks))
        if info.get("status") in H.TERMINAL:
            return info
        _sleep(cancel, settings.hpsee_poll_interval_seconds)


def _sleep(cancel: threading.Event, seconds: float) -> None:
    cancel.wait(seconds)


def _cancel_remote(run_id: str, client: H.HPSeeClient, wid: str, reason: str, log_path: Path | None) -> None:
    try:
        client.cancel_workflow(wid)
        note = "HPSee workflow cancelled (queued jobs removed; jobs already running on a worker finish)"
    except H.HPSeeError as exc:
        note = f"could not cancel the HPSee workflow: {exc}"
    _log_line(log_path, f"{reason}: {note}")
    _update_run(run_id, status="cancelled", finished_at=_now(), error=f"{reason}. {note}")


def finish(run_id: str, client: H.HPSeeClient, info: dict) -> dict:
    """Record a terminal workflow. Raises HPSeeError when there is nothing usable."""
    status = info.get("status")
    result = info.get("result") or {}
    err_text, retryable = "", True
    if status not in (H.SUCCESSFUL, H.SUCCESSFUL_NO_RESULT):
        try:
            err_text, retryable = H.describe_errors(client.workflow_errors(info["workflowId"]))
        except H.HPSeeError as exc:
            err_text = f"(error details unavailable: {exc})"
    ok = status in H.HAS_RESULTS or status == H.SUCCESSFUL_NO_RESULT
    _update_run(run_id, status="succeeded" if ok else "failed", finished_at=_now(),
                hpsee_status=status, hpsee_status_text=info.get("statusDescription"),
                tasks=H.summarise_tasks(info), result_library_id=result.get("libraryId"),
                result_size=result.get("librarySize"), error=err_text or None,
                error_category=None if ok else "workflow", retryable=retryable)
    if not ok:
        raise H.HPSeeError(f"HPSee workflow {info['workflowId']} {H.STATUS_TEXT.get(status, status).lower()}"
                           + (f": {err_text}" if err_text else ""), category="workflow")
    return result


def fail_run(run_id: str, exc: BaseException) -> None:
    cat = getattr(exc, "category", None)
    _update_run(run_id, status="failed", finished_at=_now(), error=str(exc)[:1500], error_category=cat,
                retryable=cat not in ("license", "auth", "not_configured", "rejected"))


def fetch_records(client: H.HPSeeClient, library_id: str, limit: int) -> tuple[list[tuple[int, str]], str]:
    """(entry id, SDF record) pairs, best docking score first when the library has that property."""
    sorting = ""
    try:
        sorting = H.result_sorting(client.library(library_id).get("properties"))
    except H.HPSeeError:
        pass
    out = []
    for e in client.library_entries(library_id, limit=limit, sorting=sorting):
        rec = e["entry"]
        if not rec.rstrip().endswith("$$$$"):
            rec = rec.rstrip("\n") + "\n$$$$\n"
        out.append((int(e["entryId"]), rec))
    return out, sorting


# ---------------------------------------------------------------------------
# Library mode: docking and HYDE scoring batches
# ---------------------------------------------------------------------------
def _batch_run(job_id: str, batch_id: str, kind: str) -> str:
    """Reuse this batch's live/finished run (restart, re-claim); otherwise start a new attempt."""
    with get_session() as s:
        run = s.execute(select(models.HPSeeRun).where(
            models.HPSeeRun.job_id == job_id, models.HPSeeRun.batch_id == batch_id, models.HPSeeRun.kind == kind)
            .order_by(models.HPSeeRun.created_at.desc()).limit(1)).scalar_one_or_none()
        if run is not None and run.status in ("submitting", "running", "succeeded"):
            return run.id
    return new_run(job_id, kind, batch_id=batch_id)


def _engine_label(st: H.HPSeeStatus | None = None) -> str:
    st = st or H.status()
    return f"BioSolveIT HPSee {st.version or ''}".strip()


class HPSeeDockingEngine:
    """FlexX docking on HPSee; same contract as FlexxEngine.dock_batch."""

    key = "hpsee"
    name = "BioSolveIT HPSee (FlexX)"
    score_unit = "FlexX score"

    def __init__(self, job_id: str, batch_id: str, shutdown: threading.Event | None) -> None:
        self.status = H.require_status(H.WF_DOCKING)
        self.job_id, self.batch_id, self.shutdown = job_id, batch_id, shutdown

    @property
    def version(self) -> str:
        return self.status.version or "unknown"

    def dock_batch(self, items: list[DockInput], *, protein_pdb: Path, ref_ligand_sdf: Path, workdir: Path,
                   log_path: Path, cfg: DockSettings, timeout: float) -> list[DockResult]:
        workdir.mkdir(parents=True, exist_ok=True)
        results: dict[str, DockResult] = {}
        lib = workdir / "ligands3d.sdf"
        name_to_id, failed = write_3d_library(items, lib, cfg.seed)
        for mid, err in failed.items():
            results[mid] = DockResult(mid, False, error=err)
        if name_to_id:
            client = _client()
            run_id = _batch_run(self.job_id, self.batch_id, "docking")
            try:
                if _run_snapshot(run_id).status != "succeeded":
                    submit_or_adopt(run_id, client, lambda d: client.start_docking(
                        protein_pdb, ref_ligand_sdf, lib, description=d), f"MDP docking {self.job_id[:8]}", log_path)
                    info = wait(run_id, client, cfg.cancel_event or threading.Event(), self.shutdown, log_path)
                    finish(run_id, client, info)
                run = _run_snapshot(run_id)
                records = []
                if run.result_library_id and run.result_size:
                    records, _ = fetch_records(client, run.result_library_id, min(
                        settings.hpsee_max_fetch_entries, max(10, run.result_size)))
                _update_run(run_id, fetched_count=len(records))
                text = "".join(r for _, r in records)
                (workdir / "hpsee_poses.sdf").write_text(text)
                _log_line(log_path, f"fetched {len(records)} of {run.result_size or 0} result entries")
                results.update(parse_flexx_output(text, name_to_id, f"BioSolveIT FlexX on HPSee {self.version}"))
            except proc.ToolCancelled:
                raise
            except Exception as exc:
                fail_run(run_id, exc)
                raise
            note = f" ({run.error})" if run.error else ""
            for mid in name_to_id.values():
                results.setdefault(mid, DockResult(mid, False, error="HPSee docking produced no pose for this molecule" + note))
        return [results[it.molecule_id] for it in items]


class HPSeeHydeEngine:
    """HYDE scoring on HPSee; same contract as HydeEngine.score."""

    key = "hyde"
    name = "BioSolveIT HYDE on HPSee"

    def __init__(self, job_id: str, batch_id: str, shutdown: threading.Event | None) -> None:
        self.status = H.require_status(H.WF_SCORING)
        self.job_id, self.batch_id, self.shutdown = job_id, batch_id, shutdown

    @property
    def version(self) -> str:
        return f"HPSee {self.status.version or 'unknown'}"

    def score(self, poses_sdf: Path, *, protein_pdb: Path, ref_ligand_sdf: Path, workdir: Path, log_path: Path,
              threads: int, timeout: float, cancel_event=None) -> dict[str, HydeScore]:
        client = _client()
        run_id = _batch_run(self.job_id, self.batch_id, "scoring")
        try:
            if _run_snapshot(run_id).status != "succeeded":
                submit_or_adopt(run_id, client, lambda d: client.start_scoring(
                    protein_pdb, ref_ligand_sdf, poses_sdf, description=d), f"MDP HYDE {self.job_id[:8]}", log_path)
                info = wait(run_id, client, cancel_event or threading.Event(), self.shutdown, log_path)
                finish(run_id, client, info)
            run = _run_snapshot(run_id)
            records = []
            if run.result_library_id and run.result_size:
                records, _ = fetch_records(client, run.result_library_id,
                                           min(settings.hpsee_max_fetch_entries, max(10, run.result_size)))
            _update_run(run_id, fetched_count=len(records))
            text = "".join(r for _, r in records)
            (workdir / "hyde_scored.sdf").write_text(text)
            return parse_hyde_output(text)
        except proc.ToolCancelled:
            raise
        except sdflib.SdfError as exc:
            fail_run(run_id, exc)
            raise proc.ToolError(f"HPSee HYDE output is corrupt: {exc}") from exc
        except Exception as exc:
            fail_run(run_id, exc)
            raise


# ---------------------------------------------------------------------------
# Chemical Space Docking
# ---------------------------------------------------------------------------
def definition_path(definition_id: str | None, kind: str) -> Path:
    """Absolute path of an uploaded SeeSAR definition file (an Artifact row with no job)."""
    if not definition_id:
        raise P.ParamError(f"A SeeSAR {'.flexx docking' if kind == 'flexx' else '.hydescorer scoring'} "
                           "definition file is required for Chemical Space Docking.")
    with get_session() as s:
        a = s.get(models.Artifact, definition_id)
        if a is None or a.stage != "hpsee_definition" or a.kind != kind:
            raise P.ParamError(f"Unknown {kind} definition file; upload it again.")
        path = storage.resolve(a.storage_path)
    if not path.is_file():
        raise P.ParamError(f"The uploaded {kind} definition file is missing on disk; upload it again.")
    return path


def _csd_files(job_id: str) -> tuple[Path, Path]:
    prep = storage.ensure_dir("runs", job_id, "prepare")
    return prep / "docking_definition.flexx", prep / "scoring_definition.hydescorer"


def _job_params(job_id: str) -> dict:
    with get_session() as s:
        return P.resolve(s.get(models.Job, job_id).params)


def _latest(job_id: str, kinds: tuple[str, ...]) -> models.HPSeeRun | None:
    with get_session() as s:
        run = s.execute(select(models.HPSeeRun).where(
            models.HPSeeRun.job_id == job_id, models.HPSeeRun.kind.in_(kinds))
            .order_by(models.HPSeeRun.round.desc(), models.HPSeeRun.created_at.desc()).limit(1)).scalar_one_or_none()
        if run is not None:
            s.expunge(run)
        return run


def _stage_log(job_id: str, stage: str) -> Path:
    return storage.ensure_dir("runs", job_id, stage) / "hpsee.log"


def _resume_failed(run: models.HPSeeRun, client: H.HPSeeClient, log_path: Path) -> bool:
    """A failed workflow with retryable errors is requeued on HPSee itself (no duplicate work)."""
    if not (run.workflow_id and run.error_category == "workflow" and run.retryable):
        return False
    try:
        client.retry_workflow(run.workflow_id)
    except H.HPSeeError as exc:
        _log_line(log_path, f"HPSee could not requeue workflow {run.workflow_id}: {exc}; submitting anew")
        return False
    _log_line(log_path, f"asked HPSee to requeue the failed jobs of workflow {run.workflow_id}")
    _update_run(run.id, status="running", error=None, error_category=None, finished_at=None)
    return True


def _ensure_csd(run_id: str, client: H.HPSeeClient, p: dict, log_path: Path) -> str:
    run = _run_snapshot(run_id)
    if run.csd_id:
        return run.csd_id
    marker = _marker(run_id)
    for page in client.recent_csds():
        if marker in (page.get("description") or ""):
            _update_run(run_id, csd_id=page["chemicalSpaceDockingId"])
            return page["chemicalSpaceDockingId"]
    info = client.create_csd(p["hpsee_space_id"], f"MDP campaign {run.job_id[:8]} {marker}")
    _update_run(run_id, csd_id=info["chemicalSpaceDockingId"])
    _log_line(log_path, f"created Chemical Space Docking {info['chemicalSpaceDockingId']} on space {p['hpsee_space_id']}")
    return info["chemicalSpaceDockingId"]


def _store_entries(run_id: str, job_id: str, records: list[tuple[int, str]]) -> dict:
    """Parse, rank and persist result entries; write each record to disk. Returns counts."""
    entries = [H.parse_entry(eid, rec) for eid, rec in records]
    entries.sort(key=H.Entry.sort_key)
    d = storage.ensure_dir("runs", job_id, "hpsee", run_id)
    job_dir = storage.ensure_dir("runs", job_id)
    with get_session() as s:
        s.execute(models.HPSeeEntry.__table__.delete().where(models.HPSeeEntry.run_id == run_id))
        for rank, e in enumerate(entries, start=1):
            path = d / f"entry_{e.entry_id}.sdf"
            path.write_text(e.record)
            hy = e.hyde
            s.add(models.HPSeeEntry(
                run_id=run_id, job_id=job_id, entry_id=e.entry_id, rank=rank, name=e.name, smiles=e.smiles,
                n_linkers=e.n_linkers, docking_score=e.docking_score,
                hyde_kj_mol=hy.cumulative_kj_mol if hy else None, note=e.note,
                hyde=_hyde_payload(hy) if hy else None, pose_path=path.relative_to(job_dir).as_posix()))
    return {"entries": len(entries), "complete": sum(e.complete for e in entries),
            "with_open_attachment_points": sum(e.n_linkers > 0 for e in entries),
            "unreadable": sum(e.smiles is None for e in entries)}


def _hyde_payload(h: HydeScore) -> dict:
    from ..science.engines.hyde import clash_label
    return {"cumulative_kj_mol": h.cumulative_kj_mol, "components_kj_mol": h.components_kj_mol,
            "affinity_lower_nm": h.affinity_lower_nm, "affinity_upper_nm": h.affinity_upper_nm,
            "ligand_efficiency": h.ligand_efficiency, "lipophilic_efficiency": h.lipophilic_efficiency,
            "inter_clash": clash_label(h.inter_clash), "intra_clash": clash_label(h.intra_clash),
            "torsion_quality": h.torsion_quality, "h_bonds": h.h_bonds, "clogp": h.clogp, "tpsa": h.tpsa,
            "unfavorable_torsions": h.unfavorable_torsions}


def _execute_csd_run(run_id: str, job_id: str, stage: str, client: H.HPSeeClient, submit: Callable[[str], dict],
                     label: str, cancel: threading.Event, shutdown: threading.Event,
                     known: Callable[[], str | None] | None) -> dict:
    log_path = _stage_log(job_id, stage)
    try:
        if _run_snapshot(run_id).status != "succeeded":
            submit_or_adopt(run_id, client, submit, label, log_path, known)
            info = wait(run_id, client, cancel, shutdown, log_path)
            finish(run_id, client, info)
        run = _run_snapshot(run_id)
        records, sorting = [], ""
        if run.result_library_id and run.result_size:
            records, sorting = fetch_records(client, run.result_library_id, settings.hpsee_max_fetch_entries)
        counts = _store_entries(run_id, job_id, records)
        counts.update(result_size=run.result_size or 0, sorted_by=sorting or "HPSee entry order (no score property)")
        _update_run(run_id, fetched_count=len(records), summary=counts)
        _log_line(log_path, f"stored {counts['entries']} of {counts['result_size']} result entries: "
                            f"{counts['complete']} complete, {counts['with_open_attachment_points']} with open attachment points")
        return counts
    except proc.ToolCancelled:
        raise
    except Exception as exc:
        fail_run(run_id, exc)
        raise


def stage_anchoring(job_id: str, cancel: threading.Event, shutdown: threading.Event) -> str:
    from .runner import _set_stage
    p = _job_params(job_id)
    H.require_status(H.WF_ANCHORING)
    client = _client()
    log_path = _stage_log(job_id, "csd_anchoring")
    flexx_def, hyde_def = _csd_files(job_id)
    run = _latest(job_id, ("anchoring",))
    if run is not None and run.status in DONE_STATES:   # resumed after anchoring already finished
        c = run.summary or {}
        _set_stage(job_id, "csd_anchoring", "succeeded", finish=True,
                   counts=(c.get("result_size", 0), c.get("entries", 0), c.get("unreadable", 0)))
        return "ok"
    if run is None or run.status == "cancelled" or (run.status == "failed" and not _resume_failed(run, client, log_path)):
        run_id = new_run(job_id, "anchoring", round=0)   # a C-S-D project holds ONE anchoring, so a new one gets a new project
    else:
        run_id = run.id
    csd_id = _ensure_csd(run_id, client, p, log_path)
    _set_stage(job_id, "csd_anchoring", "running", detail=f"HPSee Chemical Space Docking {csd_id}",
               tool=_engine_label())

    def known() -> str | None:
        return client.csd(csd_id).get("anchoringWorkflow")

    counts = _execute_csd_run(
        run_id, job_id, "csd_anchoring", client,
        lambda d: client.start_anchoring(csd_id, flexx_def, hyde_def, description=d, filters=p["hpsee_anchor_filters"]),
        f"MDP anchoring {job_id[:8]}", cancel, shutdown, known)
    if not counts["entries"]:
        raise H.HPSeeError("Anchoring finished without any docked anchor synthon"
                           + (" (check the anchoring filter)" if p["hpsee_anchor_filters"] else ""), category="workflow")
    _set_stage(job_id, "csd_anchoring", "succeeded", finish=True, counts=(counts["result_size"], counts["entries"], counts["unreadable"]),
               detail=f"{counts['result_size']} anchor pose(s) returned by HPSee; the best {counts['entries']} "
                      f"(by {counts['sorted_by']}) were downloaded for review.")
    return "ok"


def stage_extension(job_id: str, cancel: threading.Event, shutdown: threading.Event) -> str:
    """State machine over HPSee runs; returns 'ok' or raises Paused."""
    from .runner import _set_stage
    p = _job_params(job_id)
    client = None
    flexx_def, hyde_def = _csd_files(job_id)
    while True:
        last = _latest(job_id, ("anchoring", "extension"))
        if last is None or (last.kind == "anchoring" and last.status != "succeeded" and last.status != "awaiting_review"
                            and last.status != "reviewed"):
            raise H.HPSeeError("Anchoring has not produced results yet.", category="workflow")
        if last.status == "succeeded" and last.kind == "anchoring":
            _await_review(job_id, last, "anchors")
            raise Paused()
        if last.status == "awaiting_review":
            raise Paused()
        if last.status == "reviewed" and last.decision == "finish":
            return _import_products(job_id, p)
        if last.status == "reviewed" and last.decision == "extend":
            run_id = new_run(job_id, "extension", round=last.round + 1, csd_id=last.csd_id,
                             selected=list(last.selected_entry_ids or []))
            _update_run(last.id, status="extended")
            continue
        if last.kind == "extension" and last.status in ("submitting", "running", "failed", "cancelled", "succeeded"):
            client = client or _client()
            H.require_status(H.WF_EXTENSION)
            log_path = _stage_log(job_id, "csd_extension")
            run_id = last.id
            if last.status == "cancelled" or (last.status == "failed" and not _resume_failed(last, client, log_path)):
                if last.status != "succeeded":
                    run_id = new_run(job_id, "extension", round=last.round, csd_id=last.csd_id,
                                     selected=list(last.selected_entry_ids or []))
                    _update_run(last.id, status=f"{last.status}-replaced")
            ids = [int(x) for x in (_run_snapshot(run_id).selected_entry_ids or [])]
            _set_stage(job_id, "csd_extension", "running", tool=_engine_label(),
                       detail=f"Round {last.round}: extending {len(ids)} selected synthon(s) on HPSee")
            prev = set(client.csd(last.csd_id).get("extensionWorkflows") or [])

            def known() -> str | None:
                with get_session() as s:
                    mine = set(s.execute(select(models.HPSeeRun.workflow_id).where(
                        models.HPSeeRun.job_id == job_id, models.HPSeeRun.workflow_id.is_not(None))).scalars())
                new = [w for w in (client.csd(last.csd_id).get("extensionWorkflows") or []) if w not in mine and w not in prev]
                return new[0] if len(new) == 1 else None

            counts = _execute_csd_run(
                run_id, job_id, "csd_extension", client,
                lambda d: client.start_extension(last.csd_id, ids, flexx_def, hyde_def, description=d,
                                                 filters=p["hpsee_extension_filters"]),
                f"MDP extension round {last.round} {job_id[:8]}", cancel, shutdown, known)
            run = _run_snapshot(run_id)
            if counts["with_open_attachment_points"] and run.round < P.HPSEE_MAX_ROUNDS:
                _await_review(job_id, run, "extended synthons")
                raise Paused()
            _update_run(run_id, status="reviewed", decision="finish")
            return _import_products(job_id, p)
        raise H.HPSeeError(f"Unexpected HPSee run state '{last.status}' for {last.kind}", category="protocol")


def _await_review(job_id: str, run: models.HPSeeRun, what: str) -> None:
    from .runner import _set_stage
    _update_run(run.id, status="awaiting_review")
    s = run.summary or {}
    _set_stage(job_id, "csd_extension", "awaiting_review",
               detail=f"Waiting for your review: {s.get('with_open_attachment_points', 0)} {what} with open attachment "
                      f"points can be extended" + (f"; {s.get('complete', 0)} complete products so far" if s.get("complete") else "")
                      + ". Nothing is extended until you confirm.")


def _import_products(job_id: str, p: dict) -> str:
    """Complete, docked + scored products of all extension rounds become the campaign's molecules."""
    from rdkit import Chem
    from .runner import _artifact, _set_stage

    with get_session() as s:
        job = s.get(models.Job, job_id)
        run_tag = (job.params.get("run_tag") or job.id[:4]).upper()
        already = s.execute(select(func.count()).select_from(models.Molecule).where(
            models.Molecule.job_id == job_id, models.Molecule.source_stage == "csd_extension")).scalar()
        rows = s.execute(select(models.HPSeeEntry, models.HPSeeRun).join(
            models.HPSeeRun, models.HPSeeRun.id == models.HPSeeEntry.run_id).where(
            models.HPSeeEntry.job_id == job_id, models.HPSeeRun.kind == "extension",
            models.HPSeeEntry.n_linkers == 0, models.HPSeeEntry.smiles.is_not(None))).all()
        for e, _ in rows:
            s.expunge(e)
    if already:
        _set_stage(job_id, "csd_extension", "succeeded", finish=True)
        return "ok"
    entries = [e for e, _ in rows]
    entries.sort(key=lambda e: ((e.hyde or {}).get("affinity_lower_nm") is None,
                                (e.hyde or {}).get("affinity_lower_nm") or 0.0,
                                e.docking_score is None, e.docking_score or 0.0, e.entry_id))
    seen, chosen, dupes = set(), [], 0
    for e in entries:
        m = Chem.MolFromSmiles(e.smiles)
        key = Chem.MolToInchiKey(m) if m is not None else e.smiles
        if key in seen:
            dupes += 1      # several poses / rounds of the same product: keep the best-scored one
            continue
        seen.add(key)
        chosen.append((e, key))
    capped = max(0, len(chosen) - p["hpsee_max_products"])
    chosen = chosen[: p["hpsee_max_products"]]
    job_dir = storage.ensure_dir("runs", job_id)
    poses = storage.ensure_dir("runs", job_id, "docking", "poses")
    version = H.status().version or "unknown"
    space = p.get("hpsee_space_name") or p.get("hpsee_space_id")
    with get_session() as s:
        for i, (e, key) in enumerate(chosen, start=1):
            did = f"MDP-{run_tag}-{i:04d}"
            rec = sdflib.parse_sdf((job_dir / e.pose_path).read_text())[0]
            block = did + rec.molblock[rec.molblock.index("\n"):]
            pose = poses / f"{did}.sdf"
            pose.write_text(sdflib.with_props(block, {
                **rec.props, "MOLECULE_ID": did, "ORIGIN": f"HPSee Chemical Space Docking product ({space})",
                "HPSEE_ENTRY_ID": e.entry_id, "HPSEE_NAME": e.name}))
            mol = models.Molecule(job_id=job_id, display_id=did, smiles=e.smiles, inchikey=key, source="hpsee_csd",
                                  external_id=e.name, source_stage="csd_extension")
            s.add(mol)
            s.flush()
            rel = pose.relative_to(job_dir).as_posix()
            s.add(models.StageResult(molecule_id=mol.id, job_id=job_id, stage="docking", payload={
                "engine": "hpsee", "engine_version": version, "score": e.docking_score, "score_unit": "FlexX score",
                "n_poses": 1, "pose_path": rel, "hpsee_entry_id": e.entry_id, "chemical_space": space}))
            if e.hyde:
                s.add(models.StageResult(molecule_id=mol.id, job_id=job_id, stage="hyde", payload={
                    **e.hyde, "engine_version": f"HPSee {version}", "pose_path": rel,
                    "source": "scored by HPSee (SeeSAR .hydescorer definition); no per-atom scores"}))
        products = storage.ensure_dir("runs", job_id, "csd_extension") / "csd_products.sdf"
        products.write_text("".join((poses / f"MDP-{run_tag}-{i:04d}.sdf").read_text() for i in range(1, len(chosen) + 1)))
        _artifact(s, job_id, "csd_extension", products, "sdf_output")
    detail = (f"{len(chosen)} complete product(s) from {space} entered the funnel"
              + (f"; {capped} more beyond the limit of {p['hpsee_max_products']} were not imported" if capped else "")
              + (f"; {dupes} duplicate pose(s) merged" if dupes else "") + ". These are enumerated members of the "
              "chemical space (make-on-demand), not catalogue stock.")
    _set_stage(job_id, "csd_extension", "succeeded", finish=True, detail=detail,
               counts=(len(entries), len(chosen), 0), tool=_engine_label())
    if not chosen:
        raise H.HPSeeError("Extension produced no complete, readable product molecule.", category="workflow")
    return "ok"


# ---------------------------------------------------------------------------
# Review (API side)
# ---------------------------------------------------------------------------
class ReviewError(ValueError):
    pass


def awaiting_run(session, job_id: str) -> models.HPSeeRun | None:
    return session.execute(select(models.HPSeeRun).where(
        models.HPSeeRun.job_id == job_id, models.HPSeeRun.status == "awaiting_review")
        .order_by(models.HPSeeRun.round.desc())).scalars().first()


def submit_review(session, job: models.Job, run_id: str, action: str, entry_ids: list[int], confirm: bool) -> dict:
    from .runner import event
    if job.status != "AWAITING_REVIEW":
        raise ReviewError("This campaign is not waiting for a review.")
    run = awaiting_run(session, job.id)
    if run is None or run.id != run_id:
        raise ReviewError("That result set is no longer awaiting review; reload the page.")
    if not confirm:
        raise ReviewError("Extension must be confirmed explicitly.")
    if action == "extend":
        ids = sorted({int(x) for x in entry_ids})
        if not 1 <= len(ids) <= H.MAX_EXTENSION_SELECTION:
            raise ReviewError(f"Select between 1 and {H.MAX_EXTENSION_SELECTION} entries to extend.")
        rows = {e.entry_id: e for e in session.execute(select(models.HPSeeEntry).where(
            models.HPSeeEntry.run_id == run.id, models.HPSeeEntry.entry_id.in_(ids))).scalars()}
        unknown = [i for i in ids if i not in rows]
        if unknown:
            raise ReviewError(f"These entries are not part of this HPSee result: {unknown[:10]}")
        closed = [rows[i].name or i for i in ids if rows[i].n_linkers == 0]
        if closed:
            raise ReviewError(f"These entries are already complete molecules and cannot be extended: {closed[:10]}")
        run.selected_entry_ids, run.decision = ids, "extend"
        detail = f"{len(ids)} {'anchor(s)' if run.kind == 'anchoring' else 'synthon(s)'} confirmed for extension round {run.round + 1}"
    elif action == "finish":
        if run.kind != "extension" or not (run.summary or {}).get("complete"):
            raise ReviewError("There are no complete products yet; extend at least once.")
        run.decision = "finish"
        detail = "Extension finished by the user; complete products go on to the remaining steps"
    else:
        raise ReviewError("action must be 'extend' or 'finish'")
    run.status = "reviewed"
    for st in session.execute(select(models.JobStage).where(
            models.JobStage.job_id == job.id, models.JobStage.stage == "csd_extension")).scalars():
        st.status, st.detail = "pending", detail
    job.status, job.failure_reason = "QUEUED", None
    event(session, job.id, "csd_extension", "REVIEWED", detail)
    return {"ok": True, "detail": detail}
