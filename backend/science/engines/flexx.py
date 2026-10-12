"""
BioSolveIT FlexX adapter (CLI per the vendor's "FlexX Command Line
Documentation", v6.3, section 3-4).

    flexx -i ligands3d.sdf -p protein.pdb -r reference_ligand.sdf -o poses.sdf \
          --max-nof-conf N --thread-count N --keep-input-sd-tags -v 2

Requirements from the documentation, enforced here:
  * input ligands need 3D coordinates (molecules without are skipped by FlexX),
    so each batch is embedded with RDKit first;
  * the binding site is defined by a protein plus a reference ligand lying in
    the pocket (6.5 Å residue selection by the tool itself);
  * the score is written to the SD tag BIOSOLVEIT.DOCKING_SCORE for each pose.

STATUS: executed against the real FlexX 6.5.3 binary (tests/integration, `-m real`).
The output naming (`<name>_<pose>`) was learned from that run, not from the manual.
"""
from __future__ import annotations

import re
from pathlib import Path

from rdkit import Chem

from ...tools import proc, registry
from .. import sdf as sdflib
from .common import DockInput, DockResult, DockSettings, LigandPrepError, embed_3d, mol_to_sdf

TAG_SCORE = "BIOSOLVEIT.DOCKING_SCORE"


_POSE_SUFFIX = re.compile(r"^(.*)_(\d+)$")


def parse_flexx_output(text: str, name_to_id: dict[str, str], engine_label: str) -> dict[str, DockResult]:
    """Group FlexX's pose records by input molecule.

    Observed with FlexX 6.5.3: every pose record is renamed `<input name>_<pose number>`
    and carries `BIOSOLVEIT.DOCKING_SCORE` (more negative = better). All poses of a
    molecule are kept, best score first and renamed back to the input name, so HYDE can
    re-score each of them (the vendor's recommended flow) and a viewer can show the best.
    """
    poses: dict[str, list[tuple[float, sdflib.SdfRecord, str]]] = {}
    for rec in sdflib.parse_sdf(text):
        m = _POSE_SUFFIX.match(rec.name)
        base = m.group(1) if m else rec.name
        mid = name_to_id.get(base)
        if mid is None:
            continue
        try:
            score = float(rec.props[TAG_SCORE])
        except (KeyError, ValueError):
            continue
        poses.setdefault(mid, []).append((score, rec, base))
    out = {}
    for mid, items in poses.items():
        items.sort(key=lambda t: t[0])
        texts = []
        for score, rec, base in items:
            block = base + rec.molblock[rec.molblock.index("\n"):]  # restore the input name
            texts.append(sdflib.with_props(
                block, {"DOCKING_ENGINE": engine_label, "FLEXX_DOCKING_SCORE": score}))
        out[mid] = DockResult(
            mid, True, score=items[0][0], score_unit="FlexX score", pose_sdf="".join(texts),
            n_poses=len(items), extra={"mode_scores": [t[0] for t in items]},
        )
    return out


class FlexxEngine:
    key = "flexx"
    name = "BioSolveIT FlexX"
    score_unit = "FlexX score"

    def __init__(self) -> None:
        self.status = registry.require("flexx")

    @property
    def version(self) -> str:
        return self.status.version or "unknown"

    def dock_batch(
        self,
        items: list[DockInput],
        *,
        protein_pdb: Path,
        ref_ligand_sdf: Path,
        workdir: Path,
        log_path: Path,
        cfg: DockSettings,
        timeout: float,
    ) -> list[DockResult]:
        workdir.mkdir(parents=True, exist_ok=True)
        results: dict[str, DockResult] = {}
        lib = workdir / "ligands3d.sdf"
        name_to_id: dict[str, str] = {}
        w = Chem.SDWriter(str(lib))
        for it in items:
            try:
                mol = embed_3d(it.smiles, cfg.seed)
            except LigandPrepError as exc:
                results[it.molecule_id] = DockResult(it.molecule_id, False, error=str(exc))
                continue
            mol.SetProp("_Name", it.display_id)
            w.write(mol)
            name_to_id[it.display_id] = it.molecule_id
        w.close()
        if name_to_id:
            out = workdir / "flexx_poses.sdf"
            out.unlink(missing_ok=True)
            argv = [
                self.status.path, "-i", str(lib), "-p", str(protein_pdb), "-r", str(ref_ligand_sdf),
                "-o", str(out), "--max-nof-conf", str(cfg.num_modes),
                "--thread-count", str(cfg.cpu), "--clash-tolerance", str(cfg.clash_tolerance),
                "--keep-input-sd-tags", "-v", "2",
            ]
            proc.run(
                argv, log_path=log_path, timeout=timeout, cancel_event=cfg.cancel_event,
                env=proc.base_env(registry.license_env()), cwd=workdir,
            )
            if not out.exists():
                raise proc.ToolError("FlexX finished but wrote no pose file")
            try:
                parsed = parse_flexx_output(
                    out.read_text(), name_to_id, f"BioSolveIT FlexX {self.version}"
                )
            except sdflib.SdfError as exc:
                raise proc.ToolError(f"FlexX output is corrupt: {exc}") from exc
            results.update(parsed)
        for mid in name_to_id.values():
            results.setdefault(
                mid, DockResult(mid, False, error="FlexX produced no valid pose (ligand skipped or no fit)")
            )
        return [results[it.molecule_id] for it in items]
