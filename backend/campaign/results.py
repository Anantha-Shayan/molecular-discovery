"""Ranking policy, candidate assembly and result-file generation."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, storage
from ..science import sdf as sdflib
from ..stages import depiction
from . import params as P


def stage_payloads(session: Session, job_id: str, molecule_ids: list[str] | None = None) -> dict[str, dict[str, dict]]:
    """{molecule_id: {stage: payload}} for SUCCEEDED results only."""
    q = select(models.StageResult).where(
        models.StageResult.job_id == job_id, models.StageResult.status == "succeeded"
    )
    if molecule_ids is not None:
        q = q.where(models.StageResult.molecule_id.in_(molecule_ids))
    out: dict[str, dict[str, dict]] = {}
    for row in session.execute(q).scalars():
        out.setdefault(row.molecule_id, {})[row.stage] = row.payload
    return out


def hyde_measure(p: dict) -> str:
    """HPSee's HYDE scoring writes no per-atom scores, so its campaigns rank by HYDE's estimated affinity."""
    return "affinity" if p.get("engine") == "hpsee" else "cumulative"


def rank_key(p: dict, by: str):
    def key(item):
        mol, pay = item
        sa = pay.get("sa", {}).get("sa_score", 99.0)
        if by == "hyde" and hyde_measure(p) == "affinity":
            primary = pay.get("hyde", {}).get("affinity_lower_nm")
        elif by == "hyde":
            primary = pay.get("hyde", {}).get("cumulative_kj_mol")
        elif by == "docking":
            primary = pay.get("docking", {}).get("score")
        else:
            primary = sa
        return (primary is None, primary if primary is not None else 0.0, sa, mol.display_id)
    return key


def ranked_candidates(session: Session, job: models.Job) -> list[tuple[models.Molecule, dict]]:
    p = P.resolve(job.params)
    mols = session.execute(
        select(models.Molecule).where(
            models.Molecule.job_id == job.id, models.Molecule.outcome == "active"
        )
    ).scalars().all()
    pay = stage_payloads(session, job.id, [m.id for m in mols])
    items = [(m, pay.get(m.id, {})) for m in mols]
    items.sort(key=rank_key(p, p["rank_by"]))
    return items


def flat_metrics(by_stage: dict[str, dict]) -> dict:
    """The separate, correctly labelled metrics for tables and files."""
    d, h, sa, ad = (by_stage.get(k, {}) for k in ("docking", "hyde", "sa", "admet"))
    aff, kin = by_stage.get("affinity", {}), by_stage.get("kinetics", {})
    return {
        "docking_engine": d.get("engine"),
        "docking_score": d.get("score"),
        "docking_unit": d.get("score_unit"),
        "hyde_cumulative_kj_mol": h.get("cumulative_kj_mol"),
        "hyde_affinity_lower_nm": h.get("affinity_lower_nm"),
        "hyde_affinity_upper_nm": h.get("affinity_upper_nm"),
        "hyde_ligand_efficiency": h.get("ligand_efficiency"),
        "hyde_inter_clash": h.get("inter_clash"),
        "hyde_rec_desolv_kj_mol": (h.get("components_kj_mol") or {}).get("rec_desolv"),
        "hyde_rec_ia_kj_mol": (h.get("components_kj_mol") or {}).get("rec_ia"),
        "hyde_lig_desolv_kj_mol": (h.get("components_kj_mol") or {}).get("lig_desolv"),
        "hyde_lig_ia_kj_mol": (h.get("components_kj_mol") or {}).get("lig_ia"),
        "hyde_intra_clash": h.get("intra_clash"),
        "hyde_h_bonds": h.get("h_bonds"),
        "hyde_unfavorable_torsions": h.get("unfavorable_torsions"),
        "sa_score": sa.get("sa_score"),
        "molecular_weight": sa.get("molecular_weight"),
        "admet_profile": ad.get("profile"),
        "admet_tox_flags": ",".join(ad.get("tox_flags", [])) if ad else None,
        "admet_herg": ad.get("values", {}).get("hERG") if ad else None,
        "admet_qed": ad.get("values", {}).get("QED") if ad else None,
        "koffee_score_kcal_mol": aff.get("score_kcal_mol"),
        "koffee_ligand_strain_kcal_mol": aff.get("ligand_strain_kcal_mol"),
        "koffee_strain_included_in_score": aff.get("strain_included_in_score"),
        "affinity_kd_nm_MOCKED": aff.get("kd_nm"),
        "koff_per_s_MOCKED": kin.get("koff_per_s"),
        "residence_time_min_MOCKED": kin.get("residence_time_min"),
    }


def sdf_record(job_dir: Path, mol: models.Molecule, by_stage: dict[str, dict], rank: int | None) -> str:
    """One SDF record: the HYDE-optimised pose if present, else the docked pose,
    else a 2D depiction; with every metric as a separately named tag."""
    pose_rel = by_stage.get("hyde", {}).get("pose_path") or by_stage.get("docking", {}).get("pose_path")
    pose_src = "HYDE-optimised pose" if by_stage.get("hyde", {}).get("pose_path") else "docking pose"
    props = {
        "MOLECULE_ID": mol.display_id,
        "RANK": rank,
        "SMILES": mol.smiles,
        "SOURCE": mol.source,
        "SOURCE_ID": mol.external_id,
        "POSE_SOURCE": pose_src if pose_rel else "none (2D depiction only)",
    }
    m = flat_metrics(by_stage)
    for k, v in m.items():
        if v is not None:
            props[k.upper()] = v
    ad = by_stage.get("admet")
    if ad:
        props["ADMET_MODEL"] = ad.get("model")
    props["PROVENANCE"] = (
        "Docking, HYDE, SA, ADMET and Koffee values are distinct model outputs and are NOT comparable "
        "or combined. Koffee values are predicted scores, not measured affinities. Fields ending _MOCKED are placeholders."
    )
    if pose_rel:
        recs = sdflib.parse_sdf((job_dir / pose_rel).read_text())
        if recs:
            return sdflib.with_props(recs[0].molblock, props)
    return depiction.sdf_for_candidate(mol.smiles, mol.display_id, props)


def results_csv(items: list[tuple[models.Molecule, dict]]) -> str:
    buf = io.StringIO()
    cols = ["rank", "molecule_id", "source", "source_id", "smiles"]
    metric_cols = list(flat_metrics({}).keys())
    w = csv.writer(buf)
    w.writerow(cols + metric_cols)
    for i, (mol, by_stage) in enumerate(items, start=1):
        m = flat_metrics(by_stage)
        w.writerow([i, mol.display_id, mol.source, mol.external_id, mol.smiles] + [m[c] for c in metric_cols])
    return buf.getvalue()


def write_artifact(session: Session, job: models.Job, stage: str, kind: str, name: str, content: str) -> str:
    d = storage.ensure_dir("runs", job.id, stage)
    path = d / name
    path.write_text(content, encoding="utf-8")
    stored = storage.to_stored(path)
    existing = session.execute(
        select(models.Artifact).where(
            models.Artifact.job_id == job.id, models.Artifact.storage_path == stored
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(models.Artifact(job_id=job.id, stage=stage, kind=kind, storage_path=stored))
    return stored


def funnel(session: Session, job_id: str) -> list[dict]:
    rows = session.execute(
        select(models.JobStage).where(models.JobStage.job_id == job_id).order_by(models.JobStage.position)
    ).scalars().all()
    return [
        {"stage": r.stage, "status": r.status, "in": r.count_in, "out": r.count_out, "failed": r.count_failed}
        for r in rows
    ]


def write_run_outputs(session: Session, job: models.Job) -> dict:
    items = ranked_candidates(session, job)
    job_dir = storage.ensure_dir("runs", job.id)
    sdf_text = "".join(sdf_record(job_dir, m, by, i) for i, (m, by) in enumerate(items, start=1))
    out = {}
    if sdf_text:
        out["sdf"] = write_artifact(session, job, "rank", "sdf_output", "ranked_candidates.sdf", sdf_text)
    out["csv"] = write_artifact(session, job, "rank", "csv_results", "ranked_candidates.csv", results_csv(items))
    summary = {
        "job_id": job.id,
        "rank_policy": P.rank_policy_text(P.resolve(job.params)),
        "funnel": funnel(session, job.id),
        "candidates": len(items),
        "parameters": job.params,
    }
    out["summary"] = write_artifact(session, job, "rank", "json", "campaign_summary.json", json.dumps(summary, indent=2, default=str))
    for rank, (mol, by) in enumerate(items, start=1):
        session.execute(
            models.StageResult.__table__.delete().where(
                models.StageResult.molecule_id == mol.id, models.StageResult.stage == "rank"
            )
        )
        session.add(models.StageResult(
            molecule_id=mol.id, job_id=job.id, stage="rank",
            payload={"rank": rank, "policy": P.rank_policy_text(P.resolve(job.params))},
        ))
    return out
