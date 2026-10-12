"""
Protein preparation for docking.

Vina path:   chain selected -> heteroatoms/waters removed -> missing atoms and
             hydrogens added at pH 7.4 (PDBFixer/OpenMM) -> Meeko PDBQT.
FlexX/HYDE:  both vendors do their own receptor preparation, so they get the
             selected chain (plus non-solvent cofactors) as PDB and the
             reference ligand as SDF (connectivity only: the vendor tools use
             it to locate the pocket, not its chemistry).

Known simplifications, stated honestly in the UI and docs:
  * the Vina receptor is rigid and excludes cofactors, metals and waters;
  * protonation states come from PDBFixer's pH 7.4 rules, not from a pKa
    calculation;
  * residue gaps are not rebuilt (loops missing in the file stay missing).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from ..tools import proc
from .site import LigandSite, _NOT_LIGANDS, _atom_lines


@dataclasses.dataclass
class PreparedReceptor:
    pdbqt: Path | None
    protein_pdb: Path           # for FlexX / HYDE
    ref_ligand_sdf: Path | None  # for FlexX / HYDE (ligand-defined sites only)
    notes: list[str]


def write_chain_pdb(src: Path, dst: Path, chain: str | None, ref: LigandSite | None) -> int:
    """Selected chain's protein atoms + non-solvent cofactors, minus the reference ligand."""
    kept = []
    for ln in _atom_lines(src):
        ch = ln[21].strip() or "-"
        if chain and ch != chain:
            continue
        if ln.startswith("HETATM"):
            name = ln[17:20].strip()
            if name in _NOT_LIGANDS or name in ("HOH", "WAT", "DOD"):
                continue
            if ref and name == ref.het_name and ch == ref.chain and ln[22:27].strip() == ref.resseq:
                continue
        kept.append(ln)
    if sum(1 for ln in kept if ln.startswith("ATOM  ")) < 20:
        raise proc.ToolError(
            f"Chain {chain!r} has too few protein atoms to dock into; choose another chain."
        )
    dst.write_text("\n".join(kept) + "\nEND\n")
    return len(kept)


def write_reference_ligand(src: Path, dst: Path, ref: LigandSite) -> None:
    from rdkit import Chem
    from rdkit.Chem import rdDetermineBonds

    lines = [
        ln for ln in _atom_lines(src)
        if ln.startswith("HETATM") and ln[17:20].strip() == ref.het_name
        and (ln[21].strip() or "-") == ref.chain and ln[22:27].strip() == ref.resseq
    ]
    tmp = dst.with_suffix(".raw.pdb")
    tmp.write_text("\n".join(lines) + "\nEND\n")
    mol = Chem.MolFromPDBFile(str(tmp), removeHs=False, sanitize=False)
    tmp.unlink(missing_ok=True)
    if mol is None or mol.GetNumAtoms() == 0:
        raise proc.ToolError(f"Could not read reference ligand {ref.key} from the structure.")
    rdDetermineBonds.DetermineConnectivity(mol)
    mol.SetProp("_Name", ref.key)
    w = Chem.SDWriter(str(dst))
    w.write(mol)
    w.close()


def prepare_vina_receptor(chain_pdb: Path, workdir: Path, center: list[float], size: list[float]) -> Path:
    """PDBFixer protonation + Meeko PDBQT. Returns the receptor PDBQT path."""
    from openmm.app import PDBFile
    from pdbfixer import PDBFixer

    fixer = PDBFixer(str(chain_pdb))
    fixer.removeHeterogens(keepWater=False)
    fixer.findMissingResidues()
    fixer.missingResidues = {}  # never invent loops
    fixer.findNonstandardResidues()
    fixer.replaceNonstandardResidues()
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    fixer.addMissingHydrogens(7.4)
    h_pdb = workdir / "receptor_H.pdb"
    with h_pdb.open("w") as fh:
        PDBFile.writeFile(fixer.topology, fixer.positions, fh, keepIds=True)

    import sys

    mk = Path(sys.executable).parent / "mk_prepare_receptor.py"
    if not mk.exists():
        raise proc.ToolError("Meeko's mk_prepare_receptor.py is not installed in this environment.")
    base = workdir / "receptor"
    proc.run(
        [sys.executable, str(mk), "--read_pdb", str(h_pdb), "-o", str(base), "-p",
         "--box_center", *[str(c) for c in center], "--box_size", *[str(s) for s in size]],
        log_path=workdir / "receptor_prep.log", timeout=300,
    )
    out = workdir / "receptor.pdbqt"
    if not out.exists() or out.stat().st_size == 0:
        raise proc.ToolError("Receptor preparation produced no PDBQT file.")
    return out
