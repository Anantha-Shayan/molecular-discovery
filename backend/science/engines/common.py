"""Shared types and ligand preparation for docking engines."""
from __future__ import annotations

import dataclasses
import threading
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import AllChem


@dataclasses.dataclass
class DockInput:
    molecule_id: str
    display_id: str
    smiles: str


@dataclasses.dataclass
class DockResult:
    molecule_id: str
    ok: bool
    score: float | None = None          # engine-native docking score
    score_unit: str | None = None
    pose_sdf: str | None = None         # best pose, SDF text (3D, with H)
    n_poses: int = 0
    extra: dict = dataclasses.field(default_factory=dict)
    error: str | None = None


@dataclasses.dataclass
class DockSettings:
    cpu: int = 4
    exhaustiveness: int = 8
    num_modes: int = 5
    seed: int = 42
    per_ligand_timeout: float = 300.0
    clash_tolerance: int = 0          # FlexX --clash-tolerance: 0 standard, 1 medium, 2 high
    cancel_event: threading.Event | None = None


class LigandPrepError(ValueError):
    pass


def embed_3d(smiles: str, seed: int = 42) -> Chem.Mol:
    """Protonated 3D conformer (ETKDGv3 + MMFF/UFF clean-up).

    One starting conformer is enough for Vina/FlexX because both treat the
    ligand as flexible; it is a starting geometry, not a binding pose.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise LigandPrepError("SMILES could not be parsed")
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if AllChem.EmbedMolecule(mol, params) != 0:
        params.useRandomCoords = True
        if AllChem.EmbedMolecule(mol, params) != 0:
            raise LigandPrepError("3D conformer generation failed")
    try:
        if AllChem.MMFFHasAllMoleculeParams(mol):
            AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
        else:
            AllChem.UFFOptimizeMolecule(mol, maxIters=500)
    except Exception:  # noqa: BLE001 - optimisation is best-effort
        pass
    return mol


def mol_to_sdf(mol: Chem.Mol, name: str, props: dict | None = None) -> str:
    mol = Chem.Mol(mol)
    mol.SetProp("_Name", name)
    for k, v in (props or {}).items():
        if v is not None:
            mol.SetProp(k, str(v))
    from io import StringIO
    buf = StringIO()
    w = Chem.SDWriter(buf)
    w.write(mol)
    w.close()
    return buf.getvalue()


def write_3d_library(items: list[DockInput], path: Path, seed: int) -> tuple[dict[str, str], dict[str, str]]:
    """Write one RDKit 3D start geometry per molecule, titled with its display id.

    FlexX (local or on HPSee) skips molecules without 3D coordinates, so every
    batch is embedded first. Returns ({display_id: molecule_id}, {molecule_id: error}).
    """
    name_to_id: dict[str, str] = {}
    failed: dict[str, str] = {}
    w = Chem.SDWriter(str(path))
    for it in items:
        try:
            mol = embed_3d(it.smiles, seed)
        except LigandPrepError as exc:
            failed[it.molecule_id] = str(exc)
            continue
        mol.SetProp("_Name", it.display_id)
        w.write(mol)
        name_to_id[it.display_id] = it.molecule_id
    w.close()
    return name_to_id, failed
