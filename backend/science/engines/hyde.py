"""
BioSolveIT HYDE re-scoring adapter (`hydescorer`, CLI documented in the
vendor's "HYDE Command Line Documentation", v2.5).

Command (documented, section 3):
    hydescorer -i poses.sdf -o scored.sdf -p protein.pdb -r reference_ligand.sdf \
               --keep-input-sd-tags --write-atom-scores --thread-count N

Outputs read back (section 5), by their exact vendor tag names:
    BIOSOLVEIT.HYDE_ESTIMATED_AFFINITY_LOWER_BOUNDARY [nM]
    BIOSOLVEIT.HYDE_ESTIMATED_AFFINITY_UPPER_BOUNDARY [nM]
    BIOSOLVEIT.HYDE_LIGAND_EFFICIENCY, ..._LIPOPHILIC_EFFICIENCY
    BIOSOLVEIT.INTER_CLASH / INTRA_CLASH / TORSION_QUALITY / NUMBER_OF_H_BONDS
    BIOSOLVEIT.HYDE_ATOM_SCORES [kJ/mol]  (per-atom "Total" column)

"Cumulative molecular score": defined by the company's HYDE SDF Extractor
(HYDE_SCORER.html): the per-atom contributions of BIOSOLVEIT.HYDE_ATOM_SCORES are
summed into five components - Total, Rec-Desolv, Rec-IA, Lig-Desolv, Lig-IA
(kJ/mol, negative = favourable). This module reproduces that logic exactly (checked
against the extractor's own JavaScript on real hydescorer output). It is reported next
to, never instead of, the vendor's affinity range. When a molecule has several poses
(FlexX output), the pose with the lowest Total is kept.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from ...tools import proc, registry
from .. import sdf as sdflib

P = "BIOSOLVEIT."
TAG_LOWER = P + "HYDE_ESTIMATED_AFFINITY_LOWER_BOUNDARY [nM]"
TAG_UPPER = P + "HYDE_ESTIMATED_AFFINITY_UPPER_BOUNDARY [nM]"
TAG_LE = P + "HYDE_LIGAND_EFFICIENCY"
TAG_LLE = P + "HYDE_LIGAND_LIPOPHILIC_EFFICIENCY"
TAG_ATOMS = P + "HYDE_ATOM_SCORES [kJ/mol]"
_CLASH_LABEL = {0: "none", 1: "tolerable", 2: "major"}


@dataclasses.dataclass
class HydeScore:
    name: str
    components_kj_mol: dict | None   # total, rec_desolv, rec_ia, lig_desolv, lig_ia
    clogp: float | None
    tpsa: float | None
    molecular_weight: float | None
    unfavorable_torsions: int | None
    cumulative_kj_mol: float | None
    affinity_lower_nm: float | None
    affinity_upper_nm: float | None
    ligand_efficiency: float | None
    lipophilic_efficiency: float | None
    inter_clash: int | None
    intra_clash: int | None
    torsion_quality: int | None
    h_bonds: int | None
    pose_text: str  # the HYDE-optimised pose record, as SDF text


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _i(v: str | None) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


_COMPONENTS = ("total", "rec_desolv", "rec_ia", "lig_desolv", "lig_ia")


def atom_score_components(block: str | None) -> dict | None:
    """Sum the per-atom columns Total; Rec-Desolv; Rec-IA; Lig-Desolv; Lig-IA (kJ/mol).

    Same rules as the company's HYDE SDF Extractor: comment lines (#) skipped, a line
    needs >= 6 ';'-separated fields, unparsable numbers count as 0.
    """
    if not block:
        return None
    sums = dict.fromkeys(_COMPONENTS, 0.0)
    n = 0
    for ln in block.splitlines():
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        parts = ln.split(";")
        if len(parts) < 6:
            continue
        for k, key in enumerate(_COMPONENTS, start=1):
            try:
                sums[key] += float(parts[k].strip())
            except ValueError:
                pass
        n += 1
    return {k: round(v, 4) for k, v in sums.items()} if n else None


def cumulative_from_atom_scores(block: str | None) -> float | None:
    """Sum of the per-atom 'Total' column (the extractor's 'Total HYDE Score')."""
    comp = atom_score_components(block)
    return comp["total"] if comp else None


def parse_hyde_output(text: str) -> dict[str, HydeScore]:
    """{molecule name: best pose's score}. Raises SdfError on a corrupt file."""
    best: dict[str, HydeScore] = {}
    for rec in sdflib.parse_sdf(text):
        p = rec.props
        comps = atom_score_components(p.get(TAG_ATOMS))
        score = HydeScore(
            name=rec.name,
            components_kj_mol=comps,
            clogp=_f(p.get(P + "LOGP")),
            tpsa=_f(p.get(P + "TPSA")),
            molecular_weight=_f(p.get(P + "MOLECULAR_WEIGHT")),
            unfavorable_torsions=_i(p.get(P + "NUMBER_OF_UNFAVORABLE_TORSIONS")),
            cumulative_kj_mol=comps["total"] if comps else None,
            affinity_lower_nm=_f(p.get(TAG_LOWER)),
            affinity_upper_nm=_f(p.get(TAG_UPPER)),
            ligand_efficiency=_f(p.get(TAG_LE)),
            lipophilic_efficiency=_f(p.get(TAG_LLE)),
            inter_clash=_i(next((v for k, v in p.items() if k.startswith(P + "INTER_CLASH")), None)),
            intra_clash=_i(next((v for k, v in p.items() if k.startswith(P + "INTRA_CLASH")), None)),
            torsion_quality=_i(next((v for k, v in p.items() if k.startswith(P + "TORSION_QUALITY")), None)),
            h_bonds=_i(p.get(P + "NUMBER_OF_H_BONDS")),
            pose_text=rec.text,
        )
        cur = best.get(rec.name)
        if cur is None or _rank(score) < _rank(cur):
            best[rec.name] = score
    return best


def _rank(s: HydeScore) -> tuple[float, float]:
    """Best pose: lowest cumulative score. HPSee's scoring writes no per-atom scores, so its poses
    (cumulative = None) are compared by the vendor's estimated affinity lower bound instead."""
    if s.cumulative_kj_mol is not None:
        return (s.cumulative_kj_mol, 0.0)
    return (float("inf"), s.affinity_lower_nm if s.affinity_lower_nm is not None else float("inf"))


def clash_label(v: int | None) -> str | None:
    return _CLASH_LABEL.get(v) if v is not None else None


class HydeEngine:
    key = "hyde"
    name = "BioSolveIT HYDE"

    def __init__(self) -> None:
        self.status = registry.require("hydescorer")

    @property
    def version(self) -> str:
        return self.status.version or "unknown"

    def score(
        self,
        poses_sdf: Path,
        *,
        protein_pdb: Path,
        ref_ligand_sdf: Path,
        workdir: Path,
        log_path: Path,
        threads: int,
        timeout: float,
        cancel_event=None,
    ) -> dict[str, HydeScore]:
        out = workdir / "hyde_scored.sdf"
        out.unlink(missing_ok=True)
        argv = [
            self.status.path, "-i", str(poses_sdf), "-o", str(out),
            "-p", str(protein_pdb), "-r", str(ref_ligand_sdf),
            "--keep-input-sd-tags", "--write-atom-scores",
            "--thread-count", str(threads), "-v", "3",
        ]
        proc.run(
            argv, log_path=log_path, timeout=timeout, cancel_event=cancel_event,
            env=proc.base_env(registry.license_env()), cwd=workdir,
        )
        if not out.exists():
            raise proc.ToolError("HYDE finished but wrote no output file (no molecule could be scored)")
        try:
            return parse_hyde_output(out.read_text())
        except sdflib.SdfError as exc:
            raise proc.ToolError(f"HYDE output is corrupt: {exc}") from exc
