"""
AutoDock Vina adapter (open-source docking; CPU).

Ligands: RDKit ETKDG 3D -> Meeko PDBQT. Receptor: PDBFixer + Meeko PDBQT (see
science/receptor.py). Vina is invoked once per ligand as the documented CLI:

    vina --receptor R.pdbqt --ligand L.pdbqt --center_x.. --size_x.. \
         --exhaustiveness N --num_modes N --cpu N --seed N --out O.pdbqt

The reported score is Vina's predicted binding free energy in kcal/mol for the
best pose (more negative = better). It is a docking score, NOT a measured or
calibrated affinity, and is not comparable with FlexX or HYDE values.
"""
from __future__ import annotations

import re
from pathlib import Path

from ...tools import proc, registry
from .common import DockInput, DockResult, DockSettings, LigandPrepError, embed_3d, mol_to_sdf

_RESULT_RE = re.compile(r"REMARK VINA RESULT:\s+(-?\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)")


def _ligand_pdbqt(smiles: str, seed: int) -> str:
    from meeko import MoleculePreparation, PDBQTWriterLegacy

    mol = embed_3d(smiles, seed)
    setups = MoleculePreparation().prepare(mol)
    if not setups:
        raise LigandPrepError("Meeko could not prepare the ligand")
    pdbqt, ok, err = PDBQTWriterLegacy.write_string(setups[0])
    if not ok:
        raise LigandPrepError(f"PDBQT writing failed: {err}")
    return pdbqt


def _best_pose_sdf(pdbqt_text: str, name: str, props: dict, smiles: str) -> str:
    from meeko import PDBQTMolecule, RDKitMolCreate
    from rdkit import Chem

    pm = PDBQTMolecule(pdbqt_text, is_dlg=False, skip_typing=True)
    mols = RDKitMolCreate.from_pdbqt_mol(pm)
    if not mols or mols[0] is None:
        raise proc.ToolError("Could not read the docked pose back from Vina output")
    mol = mols[0]  # conformer 0 = best-scored pose
    keep = Chem.Mol(mol)
    keep.RemoveAllConformers()
    keep.AddConformer(Chem.Conformer(mol.GetConformer(0)), assignId=True)
    return mol_to_sdf(keep, name, props)


class VinaEngine:
    key = "vina"
    name = "AutoDock Vina"
    score_unit = "kcal/mol"

    def __init__(self) -> None:
        self.status = registry.require("vina")

    @property
    def version(self) -> str:
        return self.status.version or "unknown"

    def dock_batch(
        self,
        items: list[DockInput],
        *,
        receptor_pdbqt: Path,
        center: list[float],
        size: list[float],
        workdir: Path,
        log_path: Path,
        cfg: DockSettings,
    ) -> list[DockResult]:
        workdir.mkdir(parents=True, exist_ok=True)
        results: list[DockResult] = []
        for item in items:
            if cfg.cancel_event is not None and cfg.cancel_event.is_set():
                raise proc.ToolCancelled("Cancelled by user")
            try:
                results.append(self._dock_one(item, receptor_pdbqt, center, size, workdir, log_path, cfg))
            except proc.ToolCancelled:
                raise
            except (LigandPrepError, proc.ToolError) as exc:
                results.append(DockResult(item.molecule_id, False, error=str(exc)))
            except Exception as exc:  # noqa: BLE001 - one bad molecule must not sink the batch
                results.append(DockResult(item.molecule_id, False, error=f"{exc.__class__.__name__}: {exc}"))
        return results

    def _dock_one(self, item, receptor, center, size, workdir, log_path, cfg) -> DockResult:
        lig = workdir / f"{item.molecule_id}.pdbqt"
        out = workdir / f"{item.molecule_id}_out.pdbqt"
        lig.write_text(_ligand_pdbqt(item.smiles, cfg.seed))
        argv = [
            self.status.path, "--receptor", str(receptor), "--ligand", str(lig),
            "--center_x", str(center[0]), "--center_y", str(center[1]), "--center_z", str(center[2]),
            "--size_x", str(size[0]), "--size_y", str(size[1]), "--size_z", str(size[2]),
            "--exhaustiveness", str(cfg.exhaustiveness), "--num_modes", str(cfg.num_modes),
            "--cpu", str(cfg.cpu), "--seed", str(cfg.seed), "--out", str(out),
        ]
        proc.run(argv, log_path=log_path, timeout=cfg.per_ligand_timeout, cancel_event=cfg.cancel_event)
        text = out.read_text() if out.exists() else ""
        scores = [float(m.group(1)) for m in _RESULT_RE.finditer(text)]
        if not scores:
            raise proc.ToolError("Vina produced no poses for this ligand")
        props = {
            "DOCKING_ENGINE": f"AutoDock Vina {self.version}",
            "VINA_SCORE_KCAL_MOL": scores[0],
        }
        sdf = _best_pose_sdf(text, item.display_id, props, item.smiles)
        lig.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        return DockResult(
            item.molecule_id, True, score=scores[0], score_unit=self.score_unit,
            pose_sdf=sdf, n_poses=len(scores), extra={"mode_scores": scores},
        )
