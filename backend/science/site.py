"""
Binding-site definition from a protein structure file.

Two guided ways to define a site, matching how docking tools work:

  ligand  the site is the pocket occupied by a co-crystallised ligand already
          present in the structure (the most reliable definition, and the
          only one FlexX/HYDE accept: both need a 3D reference ligand);
  manual  an explicit box (centre + size in Å) for AutoDock Vina.

Everything here is measured from the coordinates in the file; no pocket
detection is guessed.
"""
from __future__ import annotations

import dataclasses
import math
from pathlib import Path

# Residue names that are solvent, buffer or counter-ions rather than ligands.
_NOT_LIGANDS = {
    "HOH", "WAT", "DOD", "NA", "CL", "K", "MG", "CA", "ZN", "MN", "FE", "CU", "CO", "NI",
    "SO4", "PO4", "GOL", "EDO", "PEG", "PGE", "DMS", "ACT", "FMT", "TRS", "MPD", "BME", "IOD",
}
MIN_LIGAND_HEAVY_ATOMS = 6
BOX_MIN, BOX_MAX = 16.0, 40.0
DEFAULT_PADDING = 6.0


class SiteError(ValueError):
    """The requested binding site cannot be defined from this structure."""


@dataclasses.dataclass(frozen=True)
class LigandSite:
    het_name: str
    chain: str
    resseq: str
    heavy_atoms: int
    center: tuple[float, float, float]
    extent: tuple[float, float, float]

    @property
    def key(self) -> str:
        return f"{self.het_name}:{self.chain}:{self.resseq}"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "het_name": self.het_name,
            "chain": self.chain,
            "resseq": self.resseq,
            "heavy_atoms": self.heavy_atoms,
            "center": [round(v, 2) for v in self.center],
            "extent": [round(v, 2) for v in self.extent],
        }


def _atom_lines(path: Path) -> list[str]:
    return [
        ln for ln in path.read_text(errors="replace").splitlines()
        if ln.startswith(("ATOM  ", "HETATM"))
    ]


def _element(line: str) -> str:
    el = line[76:78].strip() if len(line) >= 78 else ""
    return el or line[12:16].strip().lstrip("0123456789")[:1]


def list_ligand_sites(pdb_path: Path) -> list[LigandSite]:
    """Co-crystallised ligands in a PDB file that can define a binding site."""
    groups: dict[tuple[str, str, str], list[tuple[float, float, float]]] = {}
    for ln in _atom_lines(pdb_path):
        if not ln.startswith("HETATM"):
            continue
        name = ln[17:20].strip()
        if name in _NOT_LIGANDS or _element(ln).upper() in ("H", "D"):
            continue
        try:
            xyz = (float(ln[30:38]), float(ln[38:46]), float(ln[46:54]))
        except ValueError:
            continue
        groups.setdefault((name, ln[21].strip() or "-", ln[22:27].strip()), []).append(xyz)
    sites = []
    for (name, chain, resseq), pts in groups.items():
        if len(pts) < MIN_LIGAND_HEAVY_ATOMS:
            continue
        cols = list(zip(*pts))
        center = tuple(sum(c) / len(c) for c in cols)
        extent = tuple(max(c) - min(c) for c in cols)
        sites.append(LigandSite(name, chain, resseq, len(pts), center, extent))
    return sorted(sites, key=lambda s: -s.heavy_atoms)


def box_from_ligand(site: LigandSite, padding: float = DEFAULT_PADDING) -> dict:
    size = [min(BOX_MAX, max(BOX_MIN, e + 2 * padding)) for e in site.extent]
    return {
        "mode": "ligand",
        "reference": site.key,
        "center": [round(v, 2) for v in site.center],
        "size": [round(v, 1) for v in size],
    }


def validate_manual_box(center: list[float], size: list[float], pdb_path: Path) -> dict:
    """Validate a user-drawn box: finite, sane size, and actually touching protein."""
    if len(center) != 3 or len(size) != 3:
        raise SiteError("Box centre and size need three numbers each (x, y, z).")
    if not all(math.isfinite(v) for v in [*center, *size]):
        raise SiteError("Box values must be finite numbers.")
    if any(s < BOX_MIN or s > BOX_MAX for s in size):
        raise SiteError(f"Each box dimension must be between {BOX_MIN:g} and {BOX_MAX:g} Å.")
    inside = 0
    for ln in _atom_lines(pdb_path):
        if not ln.startswith("ATOM  "):
            continue
        try:
            xyz = (float(ln[30:38]), float(ln[38:46]), float(ln[46:54]))
        except ValueError:
            continue
        if all(abs(xyz[i] - center[i]) <= size[i] / 2 for i in range(3)):
            inside += 1
    if inside < 20:
        raise SiteError(
            "The box contains almost no protein atoms. Move it onto the protein surface or pocket."
        )
    return {
        "mode": "manual",
        "reference": None,
        "center": [round(float(v), 2) for v in center],
        "size": [round(float(v), 1) for v in size],
        "protein_atoms_in_box": inside,
    }


def find_site(pdb_path: Path, key: str) -> LigandSite:
    for s in list_ligand_sites(pdb_path):
        if s.key == key:
            return s
    raise SiteError(f"Reference ligand {key!r} was not found in the structure.")
