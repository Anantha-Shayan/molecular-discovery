"""
Stage executors: the scientific work of one batch.

Each executor takes the molecules of ONE batch and returns one `MolOutcome`
per molecule. A molecule-level problem (cannot embed, tool rejected it) is a
failed outcome for that molecule only; a batch-level problem (tool missing,
crash, timeout) raises, which the runner records against the whole batch.
Executors never touch the database: the runner persists their results. (The
HPSee engines are the exception: they record their remote workflow in
hpsee_runs so a restart re-attaches to it instead of submitting it again.)
"""
from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Callable

from ..services import TargetContext, kinetics_service
from .. import storage
from ..config import settings
from ..science import admet as admet_model
from ..science.engines import flexx as flexx_engine
from ..science.engines import vina as vina_engine
from ..science.engines.common import DockInput, DockSettings
from ..science.engines.hyde import HydeEngine, clash_label
from ..science.engines.koffee import KoffeeEngine, KoffeeInput, KoffeeInputError
from ..stages import sa_scoring
from ..tools import proc


@dataclasses.dataclass
class MolRef:
    id: str
    display_id: str
    smiles: str


@dataclasses.dataclass
class MolOutcome:
    molecule_id: str
    ok: bool
    payload: dict = dataclasses.field(default_factory=dict)
    error: str | None = None


@dataclasses.dataclass
class Ctx:
    job_id: str
    params: dict
    job_dir: Path           # absolute run directory
    target: TargetContext
    prior: dict             # {molecule_id: {stage: payload}} for stages already done
    cpu: int                # CPU threads this batch may use
    batch_id: str | None = None
    shutdown: threading.Event | None = None   # set when the application stops (remote work keeps running)


Progress = Callable[[int, int], None]


def _poses_dir(ctx: Ctx, stage: str) -> Path:
    d = ctx.job_dir / stage / "poses"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_docking(ctx: Ctx, mols: list[MolRef], batch_dir: Path, log_path: Path,
                cancel: threading.Event, progress: Progress, timeout: float) -> list[MolOutcome]:
    p = ctx.params
    site = p["site"]
    prep = ctx.job_dir / "prepare"
    cfg = DockSettings(
        cpu=ctx.cpu, exhaustiveness=p["exhaustiveness"], num_modes=p["num_modes"],
        seed=p["library_seed"], per_ligand_timeout=min(timeout, 600.0), cancel_event=cancel,
        clash_tolerance=p["clash_tolerance"],
    )
    items = [DockInput(m.id, m.display_id, m.smiles) for m in mols]
    if p["engine"] == "vina":
        eng = vina_engine.VinaEngine()
        outcomes: list[MolOutcome] = []
        ok = failed = 0
        for item in items:  # per-ligand loop so progress is real
            res = eng.dock_batch(
                [item], receptor_pdbqt=prep / "receptor.pdbqt", center=site["center"],
                size=site["size"], workdir=batch_dir, log_path=log_path, cfg=cfg,
            )[0]
            outcomes.append(_dock_outcome(ctx, mols, res, eng, p))
            ok, failed = (ok + 1, failed) if res.ok else (ok, failed + 1)
            progress(ok, failed)
        return outcomes
    if p["engine"] == "hpsee":
        from .hpsee_flow import HPSeeDockingEngine
        eng = HPSeeDockingEngine(ctx.job_id, ctx.batch_id, ctx.shutdown)
    else:
        eng = flexx_engine.FlexxEngine()   # legacy campaigns only
    results = eng.dock_batch(
        items, protein_pdb=prep / "protein.pdb", ref_ligand_sdf=prep / "ref_ligand.sdf",
        workdir=batch_dir, log_path=log_path, cfg=cfg, timeout=timeout,
    )
    out = [_dock_outcome(ctx, mols, r, eng, p) for r in results]
    progress(sum(o.ok for o in out), sum(not o.ok for o in out))
    return out


def _dock_outcome(ctx: Ctx, mols: list[MolRef], res, eng, p) -> MolOutcome:
    if not res.ok:
        return MolOutcome(res.molecule_id, False, error=res.error)
    m = next(x for x in mols if x.id == res.molecule_id)
    pose = _poses_dir(ctx, "docking") / f"{m.display_id}.sdf"
    pose.write_text(res.pose_sdf)
    return MolOutcome(res.molecule_id, True, {
        "engine": eng.key,
        "engine_version": eng.version,
        "score": res.score,
        "score_unit": res.score_unit,
        "n_poses": res.n_poses,
        "mode_scores": res.extra.get("mode_scores"),
        "pose_path": pose.relative_to(ctx.job_dir).as_posix(),
        "exhaustiveness": p["exhaustiveness"] if eng.key == "vina" else None,
    })


def _hyde_engine(ctx: Ctx):
    if ctx.params["engine"] == "hpsee":
        from .hpsee_flow import HPSeeHydeEngine
        return HPSeeHydeEngine(ctx.job_id, ctx.batch_id, ctx.shutdown)
    return HydeEngine()


def run_hyde(ctx: Ctx, mols: list[MolRef], batch_dir: Path, log_path: Path,
             cancel: threading.Event, progress: Progress, timeout: float) -> list[MolOutcome]:
    prep = ctx.job_dir / "prepare"
    if not (prep / "ref_ligand.sdf").exists():
        raise proc.ToolError(
            "HYDE needs a reference ligand to locate the pocket; define the binding site from a "
            "co-crystallised ligand."
        )
    batch_dir.mkdir(parents=True, exist_ok=True)
    poses = batch_dir / "poses_in.sdf"
    with poses.open("w") as fh:
        for m in mols:
            rel = ctx.prior.get(m.id, {}).get("docking", {}).get("pose_path")
            if rel:
                fh.write((ctx.job_dir / rel).read_text())
    eng = _hyde_engine(ctx)
    scores = eng.score(
        poses, protein_pdb=prep / "protein.pdb", ref_ligand_sdf=prep / "ref_ligand.sdf",
        workdir=batch_dir, log_path=log_path, threads=ctx.cpu, timeout=timeout, cancel_event=cancel,
    )
    outcomes = []
    for m in mols:
        s = scores.get(m.display_id)
        if s is None:
            outcomes.append(MolOutcome(m.id, False, error="HYDE returned no score for this molecule: its pose did not meet HYDE's input or optimisation quality criteria"))
            continue
        pose = _poses_dir(ctx, "hyde") / f"{m.display_id}.sdf"
        pose.write_text(s.pose_text)
        outcomes.append(MolOutcome(m.id, True, {
            "engine_version": eng.version,
            "cumulative_kj_mol": s.cumulative_kj_mol,
            "components_kj_mol": s.components_kj_mol,
            "clogp": s.clogp, "tpsa": s.tpsa, "unfavorable_torsions": s.unfavorable_torsions,
            "affinity_lower_nm": s.affinity_lower_nm,
            "affinity_upper_nm": s.affinity_upper_nm,
            "ligand_efficiency": s.ligand_efficiency,
            "lipophilic_efficiency": s.lipophilic_efficiency,
            "inter_clash": clash_label(s.inter_clash),
            "intra_clash": clash_label(s.intra_clash),
            "torsion_quality": s.torsion_quality,
            "h_bonds": s.h_bonds,
            "pose_path": pose.relative_to(ctx.job_dir).as_posix(),
        }))
    progress(sum(o.ok for o in outcomes), sum(not o.ok for o in outcomes))
    return outcomes


def run_sa(ctx: Ctx, mols: list[MolRef], *_a, progress: Progress, **_k) -> list[MolOutcome]:
    # One batch -> one call: RDKit scores each molecule in-process anyway, and the
    # ChemAIRS engine (when configured) takes a SMILES collection per task, so either
    # way this is the right granularity — never one network round trip per molecule.
    results = sa_scoring.score_batch_for_stage([m.smiles for m in mols])
    out = []
    for m, r in zip(mols, results):
        if "error" in r:
            out.append(MolOutcome(m.id, False, error=r["error"]))
        else:
            out.append(MolOutcome(m.id, True, r))
    progress(sum(o.ok for o in out), sum(not o.ok for o in out))
    return out


def run_admet(ctx: Ctx, mols: list[MolRef], *_a, progress: Progress, **_k) -> list[MolOutcome]:
    preds = admet_model.predict([m.smiles for m in mols], threads=ctx.cpu)
    out = [MolOutcome(m.id, True, pr) for m, pr in zip(mols, preds)]
    progress(len(out), 0)
    return out


def run_affinity(ctx: Ctx, mols: list[MolRef], batch_dir: Path, log_path: Path,
                 cancel: threading.Event, progress: Progress, timeout: float) -> list[MolOutcome]:
    """Score existing successful docked poses; Koffee never receives generated conformers."""
    prep = ctx.job_dir / "prepare"
    # Vina's protonated PDBFixer product is the only local protein artifact that
    # can meet Koffee's explicit-hydrogen requirement.  Other engines remain
    # eligible only when their prepared protein actually contains hydrogens.
    protein = prep / "receptor_H.pdb"
    if not protein.exists():
        protein = prep / "protein.pdb"
    eligible: list[KoffeeInput] = []
    prefailed: dict[str, str] = {}
    for m in mols:
        rel = ctx.prior.get(m.id, {}).get("docking", {}).get("pose_path")
        if not rel:
            prefailed[m.id] = "Koffee requires a completed docked ligand pose."
            continue
        pose = ctx.job_dir / rel
        if not pose.is_file():
            prefailed[m.id] = "The completed docked pose artifact is missing."
            continue
        eligible.append(KoffeeInput(m.id, m.display_id, pose))
    if not eligible:
        out = [MolOutcome(m.id, False, error=prefailed.get(m.id, "No eligible docked pose.")) for m in mols]
        progress(0, len(out))
        return out
    try:
        scored, missing, archive, results_path = KoffeeEngine().run(
            protein=protein, inputs=eligible,
            workdir=settings.koffee_work_dir / ctx.job_id / (ctx.batch_id or "unbatched"), log_path=log_path,
            cpus=ctx.cpu, timeout=timeout, cancel_event=cancel,
        )
    except KoffeeInputError as exc:
        out = [MolOutcome(m.id, False, error=str(exc)) for m in mols]
        progress(0, len(out))
        return out
    out: list[MolOutcome] = []
    for m in mols:
        if m.id in prefailed:
            out.append(MolOutcome(m.id, False, error=prefailed[m.id]))
        elif m.id in missing:
            out.append(MolOutcome(m.id, False, error="Koffee produced no results.tsv row for this ligand."))
        else:
            r = scored[m.id]
            out.append(MolOutcome(m.id, True, {
                "engine": KoffeeEngine.key, "engine_version": KoffeeEngine.version,
                "score_kcal_mol": r.score_kcal_mol, "ligand_strain_kcal_mol": r.ligand_strain_kcal_mol,
                "strain_included_in_score": ctx.params["koffee_ligand_strain_in_score"],
                "system_id": r.system_id,
                "input_archive_path": storage.to_stored(archive),
                "results_tsv_path": storage.to_stored(results_path),
                "protein_path": protein.relative_to(ctx.job_dir).as_posix(),
            }))
    progress(sum(x.ok for x in out), sum(not x.ok for x in out))
    return out


def run_kinetics(ctx: Ctx, mols: list[MolRef], *_a, progress: Progress, **_k) -> list[MolOutcome]:
    out = []
    for m in mols:
        kd = ctx.prior.get(m.id, {}).get("affinity", {}).get("kd_nm", 10.0)
        out.append(MolOutcome(m.id, True, {**kinetics_service.run(ctx.target, m.smiles, kd), "mocked": True}))
    progress(len(out), 0)
    return out


EXECUTORS = {
    "docking": run_docking,
    "hyde": run_hyde,
    "sa": run_sa,
    "admet": run_admet,
    "affinity": run_affinity,
    "kinetics": run_kinetics,
}


def call(stage: str, ctx: Ctx, mols, batch_dir, log_path, cancel, progress, timeout):
    fn = EXECUTORS[stage]
    if stage in ("docking", "hyde", "affinity"):
        return fn(ctx, mols, batch_dir, log_path, cancel, progress, timeout)
    return fn(ctx, mols, progress=progress)
