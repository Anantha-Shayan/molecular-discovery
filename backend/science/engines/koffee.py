"""Kvantify Koffee Binding Affinity Docker adapter.

Koffee consumes a tarball of one PDB and one single-record SDF per pose.  This
module deliberately never creates coordinates: it validates and copies the
docked pose the upstream engine wrote.  The manifest is the authoritative
mapping between Koffee's file names and campaign molecule ids.
"""
from __future__ import annotations

import csv
import dataclasses
import math
import os
import re
import tarfile
import threading
from pathlib import Path

from ...config import settings
from ...tools import proc

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


class KoffeeInputError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class KoffeeInput:
    molecule_id: str
    display_id: str
    pose_path: Path


@dataclasses.dataclass(frozen=True)
class KoffeeResult:
    molecule_id: str
    score_kcal_mol: float
    ligand_strain_kcal_mol: float | None
    system_id: str | None


def _filename(value: str, suffix: str) -> str:
    clean = _SAFE.sub("_", value).strip("._") or "molecule"
    return clean[:80] + suffix


def _validate_protein(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise KoffeeInputError("Koffee needs a non-empty prepared protein PDB file.")
    atom_lines = [line for line in path.read_text(errors="replace").splitlines()
                  if line.startswith(("ATOM  ", "HETATM"))]
    if len(atom_lines) < 20:
        raise KoffeeInputError("Koffee protein input has too few atoms.")
    # Koffee infers residue charges from explicit PDB hydrogens.  Do not claim a
    # docking receptor without H atoms is valid for affinity calculation.
    if not any(line[76:78].strip().upper() == "H" or line[12:16].strip().startswith("H") for line in atom_lines):
        raise KoffeeInputError("Koffee requires explicit protein hydrogens matching the intended protonation state.")


def _validate_pose(path: Path) -> None:
    try:
        from rdkit import Chem
        supplier = Chem.SDMolSupplier(str(path), sanitize=True, removeHs=False)
        mols = [m for m in supplier if m is not None]
    except Exception as exc:  # RDKit preserves the input pose; it does not generate one here.
        raise KoffeeInputError(f"Docked pose is not a readable SDF: {exc}") from exc
    if len(mols) != 1:
        raise KoffeeInputError("Koffee requires exactly one molecule in each ligand SDF.")
    mol = mols[0]
    if mol.GetNumConformers() != 1 or not mol.GetConformer().Is3D():
        raise KoffeeInputError("Docked pose requires one 3D conformer; no coordinates were generated.")
    if not any(atom.GetAtomicNum() == 1 for atom in mol.GetAtoms()):
        raise KoffeeInputError("Koffee ligand input requires explicit hydrogens and formal charges.")
    if any(not math.isfinite(v) for p in mol.GetConformer().GetPositions() for v in p):
        raise KoffeeInputError("Docked pose has non-finite 3D coordinates.")


def build_input_archive(protein: Path, inputs: list[KoffeeInput], workdir: Path) -> tuple[Path, dict[str, str]]:
    """Build an uncompressed, path-safe Koffee input tar and return ligand->id map."""
    _validate_protein(protein)
    if not inputs:
        raise KoffeeInputError("No completed docked poses are eligible for Koffee.")
    source = workdir / "input"
    source.mkdir(parents=True, exist_ok=True)
    protein_name = "protein.pdb"
    (source / protein_name).write_bytes(protein.read_bytes())
    mapping: dict[str, str] = {}
    rows: list[tuple[str, str]] = []
    for index, item in enumerate(inputs, 1):
        _validate_pose(item.pose_path)
        name = f"ligands/{index:05d}_{_filename(item.molecule_id, '.sdf')}"
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.pose_path.read_bytes())
        if name in mapping:
            raise KoffeeInputError("Duplicate generated Koffee ligand filename.")
        mapping[name] = item.molecule_id
        rows.append((protein_name, name))
    with (source / "molecules.tsv").open("w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(("protein", "ligand"))
        writer.writerows(rows)
    archive = workdir / "input.tar"
    with tarfile.open(archive, "w") as tar:
        for item in sorted(source.rglob("*")):
            if item.is_file():
                tar.add(item, arcname=item.relative_to(source).as_posix(), recursive=False)
    if archive.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise KoffeeInputError("Koffee input archive exceeds the configured 512 MiB safety limit.")
    return archive, mapping


def parse_results(path: Path, mapping: dict[str, str]) -> tuple[dict[str, KoffeeResult], set[str]]:
    if not path.is_file() or path.stat().st_size == 0:
        raise proc.ToolError("Koffee completed without results.tsv")
    found: dict[str, KoffeeResult] = {}
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        required = {"protein", "ligand", "score_kcal_mol"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise proc.ToolError("Koffee results.tsv is missing required protein, ligand, or score_kcal_mol columns")
        for row in reader:
            ligand = (row.get("ligand") or "").replace("\\", "/").lstrip("./")
            molecule_id = mapping.get(ligand)
            if not molecule_id:
                continue  # Vendor may report a failed/unrecognised input; it must not be guessed by row order.
            try:
                score = float(row["score_kcal_mol"])
                if not math.isfinite(score):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise proc.ToolError(f"Koffee returned a nonnumeric score for {ligand!r}") from exc
            raw_strain = (row.get("ligand_strain_kcal_mol") or "").strip()
            try:
                strain = float(raw_strain) if raw_strain else None
                if strain is not None and not math.isfinite(strain):
                    raise ValueError
            except ValueError as exc:
                raise proc.ToolError(f"Koffee returned a nonnumeric ligand strain for {ligand!r}") from exc
            if molecule_id in found:
                raise proc.ToolError(f"Koffee returned duplicate result rows for {ligand!r}")
            found[molecule_id] = KoffeeResult(molecule_id, score, strain, row.get("system_id") or None)
    return found, set(mapping.values()) - set(found)


class KoffeeEngine:
    key = "koffee_binding_affinity"
    version = "Koffee Binding Affinity v0.9.4"

    def run(self, *, protein: Path, inputs: list[KoffeeInput], workdir: Path, log_path: Path,
            cpus: int, timeout: float, cancel_event: threading.Event | None = None) -> tuple[dict[str, KoffeeResult], set[str], Path, Path]:
        license_file = settings.koffee_license_file
        if license_file is None or not license_file.is_file():
            raise proc.ToolError("Koffee is not configured: set KOFFEE_LICENSE_FILE to a readable vendor license file.")
        workdir.mkdir(parents=True, exist_ok=True)
        archive, mapping = build_input_archive(protein, inputs, workdir)
        uid, gid = os.getuid(), os.getgid()
        argv = [settings.koffee_docker_bin, "run", "--rm", "--user", f"{uid}:{gid}",
                "--cpus", str(max(1, cpus)), "--volume", f"{workdir.resolve()}:/host",
                "--volume", f"{license_file.resolve().parent}:/license:ro",
                settings.koffee_image, "--default-directory", "/host", "--input-archive", "/host/input.tar",
                "--license-file", f"/license/{license_file.name}", "--log-directory", "/host/logs", "--cpus", str(max(1, cpus)),
                "--optimization-level", str(settings.koffee_optimization_level),
                "--system-pre-optimization" if settings.koffee_system_pre_optimization else "--no-system-pre-optimization",
                "--ligand-strain-in-score" if settings.koffee_ligand_strain_in_score else "--no-ligand-strain-in-score",
                "--protein-preparation-mode", settings.koffee_protein_preparation_mode]
        display_argv = list(argv)
        display_argv[display_argv.index(f"{workdir.resolve()}:/host")] = "<koffee-workdir>:/host"
        display_argv[display_argv.index(f"{license_file.resolve().parent}:/license:ro")] = "<koffee-license>:/license:ro"
        display_argv[display_argv.index(f"/license/{license_file.name}")] = "/license/<license-file>"
        proc.run(argv, log_path=log_path, timeout=timeout, cancel_event=cancel_event, display_argv=display_argv)
        results_path = workdir / "results.tsv"
        parsed, missing = parse_results(results_path, mapping)
        return parsed, missing, archive, results_path
