"""
Protein structure parsing and MVP-level input validation.

SCOPE — read this before extending anything here:

This module answers exactly one question: *can this file be read as a
protein structure, and what does it actually contain?* It checks
readability, atom records, coordinate sanity, chains and residues.

It does NOT — and must not be described as — an assessment of whether a
target is scientifically suitable for docking, binding-affinity
prediction or kinetics. Pocket detection, druggability, protonation,
missing-loop repair and structure preparation are all real problems that
real tools solve; none of them happen here. Everything this module
reports is either counted from the coordinate records or copied out of
the file's own header.

Implementation note: the ATOM/HETATM parsing is done against the PDB
fixed-column specification rather than by splitting on whitespace,
because PDB fields can run together (a long residue name followed by a
negative coordinate is the classic case) and whitespace splitting
silently mangles those lines. RDKit is used as an independent
second-opinion reader, but a failure there is reported as a warning
rather than a rejection: RDKit is stricter than we need to be for
structural intake, and we don't want to reject files it merely dislikes.
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
import re

from rdkit import Chem, RDLogger

# RDKit is noisy about PDB quirks we deliberately tolerate; we surface our
# own messages instead of letting it write to stderr.
RDLogger.DisableLog("rdApp.*")

PDB_EXTENSIONS = {".pdb", ".ent"}
MMCIF_EXTENSIONS = {".cif", ".mmcif"}
SUPPORTED_EXTENSIONS = PDB_EXTENSIONS | MMCIF_EXTENSIONS

MAX_FILE_BYTES = 128 * 1024 * 1024  # 128 MB, matching the UI's stated limit

# Residue names that are solvent/cryoprotectant rather than cofactors of
# interest. Used only to label the ligand list in the UI honestly.
_SOLVENT_RESIDUES = {"HOH", "DOD", "WAT", "EDO", "GOL", "PEG", "SO4", "PO4", "ACT", "DMS"}

_STANDARD_AMINO_ACIDS = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "SEC", "PYL", "MSE",
}


@dataclasses.dataclass
class ValidationCheck:
    """One user-visible validation gate."""

    key: str
    label: str
    passed: bool
    detail: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ChainInfo:
    chain_id: str
    residue_count: int
    atom_count: int
    first_residue: int | None
    last_residue: int | None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ParsedStructure:
    """Result of reading a structure file. `ok` is the only success signal."""

    ok: bool
    checks: list[ValidationCheck]
    structure_format: str | None = None
    checksum: str | None = None
    file_size_bytes: int = 0
    title: str | None = None
    experiment_method: str | None = None
    resolution_a: float | None = None
    pdb_id_in_file: str | None = None
    chains: list[ChainInfo] = dataclasses.field(default_factory=list)
    residue_count: int = 0
    atom_count: int = 0
    hetatm_count: int = 0
    ligands: list[str] = dataclasses.field(default_factory=list)
    solvent_residues: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    text: str = ""

    @property
    def error_detail(self) -> str:
        """First failing check's detail — what the API returns to the user."""
        for check in self.checks:
            if not check.passed:
                return check.detail
        return "Structure could not be validated."

    def checks_as_dicts(self) -> list[dict]:
        return [c.as_dict() for c in self.checks]

    def chains_as_dicts(self) -> list[dict]:
        return [c.as_dict() for c in self.chains]


def _fail(checks: list[ValidationCheck], key: str, label: str, detail: str) -> ParsedStructure:
    checks.append(ValidationCheck(key, label, False, detail))
    return ParsedStructure(ok=False, checks=checks)


def detect_format(filename: str | None) -> str | None:
    """Map a filename to a supported structure format, or None."""
    if not filename or "." not in filename:
        return None
    ext = "." + filename.rsplit(".", 1)[-1].lower()
    if ext in PDB_EXTENSIONS:
        return "pdb"
    if ext in MMCIF_EXTENSIONS:
        return "mmcif"
    return None


def parse_structure(
    raw: bytes | str, filename: str | None = None, max_bytes: int = MAX_FILE_BYTES
) -> ParsedStructure:
    """Parse and validate a protein structure file.

    Pure function: no database, no network, no filesystem. That's what makes
    it directly unit-testable and reusable for upload, PDB-ID fetch and the
    bundled demo fixture alike.
    """
    checks: list[ValidationCheck] = []

    # --- Check 1: the file can be read ---------------------------------
    if isinstance(raw, str):
        data = raw.encode("utf-8", errors="replace")
        text_content = raw
    else:
        data = raw
        if len(data) > max_bytes:
            return _fail(
                checks, "readable", "Structure readable",
                f"File is {len(data) / 1e6:.1f} MB, above the {max_bytes // (1024 * 1024)} MB limit.",
            )
        try:
            text_content = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text_content = data.decode("latin-1")
            except Exception:
                return _fail(
                    checks, "readable", "Structure readable",
                    "File is not text-decodable — expected a PDB or mmCIF text file.",
                )

    if not text_content.strip():
        return _fail(checks, "readable", "Structure readable", "File is empty.")

    structure_format = detect_format(filename)
    if filename and structure_format is None:
        return _fail(
            checks, "readable", "Structure readable",
            f"Unsupported file type {filename!r}. Supported formats: "
            ".pdb, .ent, .cif, .mmcif.",
        )
    if structure_format is None:
        # No filename given (e.g. fetched by PDB ID): infer from content.
        structure_format = "mmcif" if "_atom_site." in text_content else "pdb"

    checksum = hashlib.sha256(data).hexdigest()

    if structure_format == "mmcif":
        atoms, parse_error = _parse_mmcif_atoms(text_content)
    else:
        atoms, parse_error = _parse_pdb_atoms(text_content)

    checks.append(
        ValidationCheck(
            "readable", "Structure readable",
            True,
            f"Parsed as {structure_format.upper()} ({len(data) / 1024:.1f} KB).",
        )
    )

    # --- Check 2: the file contains atom records ------------------------
    if parse_error:
        return _fail(checks, "atoms", "Atom records present", parse_error)
    if not atoms:
        return _fail(
            checks, "atoms", "Atom records present",
            "No ATOM or HETATM records found — this file contains no atoms.",
        )
    polymer_atoms = [a for a in atoms if not a["is_hetatm"]]
    hetatm_atoms = [a for a in atoms if a["is_hetatm"]]
    checks.append(
        ValidationCheck(
            "atoms", "Atom records present", True,
            f"{len(atoms):,} atom records ({len(polymer_atoms):,} ATOM, "
            f"{len(hetatm_atoms):,} HETATM).",
        )
    )

    # --- Check 3: coordinates are present and finite --------------------
    bad_coord = next(
        (a for a in atoms if not all(math.isfinite(v) for v in (a["x"], a["y"], a["z"]))),
        None,
    )
    if bad_coord is not None:
        return _fail(
            checks, "coordinates", "Atomic coordinates detected",
            f"Non-finite coordinate on atom {bad_coord['serial']} "
            f"({bad_coord['res_name']} {bad_coord['res_seq']}).",
        )
    xs = [a["x"] for a in atoms]
    ys = [a["y"] for a in atoms]
    zs = [a["z"] for a in atoms]
    if max(xs) - min(xs) == 0 and max(ys) - min(ys) == 0 and max(zs) - min(zs) == 0:
        return _fail(
            checks, "coordinates", "Atomic coordinates detected",
            "All atoms share one coordinate — the file has no usable geometry.",
        )
    checks.append(
        ValidationCheck(
            "coordinates", "Atomic coordinates detected", True,
            f"Bounding box {max(xs) - min(xs):.1f} × {max(ys) - min(ys):.1f} × "
            f"{max(zs) - min(zs):.1f} Å.",
        )
    )

    # --- Check 4: chains can be identified ------------------------------
    chains = _summarise_chains(polymer_atoms or atoms)
    if not chains:
        return _fail(
            checks, "chains", "Chain information available",
            "No chain identifiers could be read from the atom records.",
        )
    chain_labels = ", ".join(c.chain_id for c in chains[:6])
    if len(chains) > 6:
        chain_labels += f" (+{len(chains) - 6} more)"
    checks.append(
        ValidationCheck(
            "chains", "Chain information available", True,
            f"{len(chains)} chain(s) detected: {chain_labels}.",
        )
    )

    # --- Check 5: residues can be identified ----------------------------
    residue_count = sum(c.residue_count for c in chains)
    if residue_count == 0:
        return _fail(
            checks, "residues", "Residue information available",
            "Atom records carry no residue numbering.",
        )
    standard = {
        a["res_name"] for a in polymer_atoms if a["res_name"] in _STANDARD_AMINO_ACIDS
    }
    checks.append(
        ValidationCheck(
            "residues", "Residue information available", True,
            f"{residue_count:,} residues across {len(chains)} chain(s); "
            f"{len(standard)} distinct standard amino acids.",
        )
    )

    # --- Header metadata — only what the file itself states --------------
    title = method = pdb_id_in_file = None
    resolution = None
    if structure_format == "pdb":
        title, method, resolution, pdb_id_in_file = _parse_pdb_header(text_content)
    else:
        title, method, resolution, pdb_id_in_file = _parse_mmcif_header(text_content)

    het_names = [a["res_name"] for a in hetatm_atoms]
    ligands = sorted({n for n in het_names if n not in _SOLVENT_RESIDUES})
    solvents = sorted({n for n in het_names if n in _SOLVENT_RESIDUES})

    warnings: list[str] = []
    if not polymer_atoms:
        warnings.append("File contains only HETATM records — no polymer chain found.")
    if not standard:
        warnings.append("No standard amino-acid residues recognised; this may not be a protein.")
    if not _rdkit_can_read(text_content, structure_format):
        warnings.append(
            "RDKit's PDB reader could not parse this file; structural intake "
            "still succeeded, but downstream RDKit-based steps may be limited."
        )

    return ParsedStructure(
        ok=True,
        checks=checks,
        structure_format=structure_format,
        checksum=checksum,
        file_size_bytes=len(data),
        title=title,
        experiment_method=method,
        resolution_a=resolution,
        pdb_id_in_file=pdb_id_in_file,
        chains=chains,
        residue_count=residue_count,
        atom_count=len(atoms),
        hetatm_count=len(hetatm_atoms),
        ligands=ligands,
        solvent_residues=solvents,
        warnings=warnings,
        text=text_content,
    )


# ---------------------------------------------------------------------------
# PDB
# ---------------------------------------------------------------------------
def _parse_pdb_atoms(text_content: str) -> tuple[list[dict], str | None]:
    """Parse ATOM/HETATM records by fixed column positions (PDB v3.3).

    Only the first MODEL of a multi-model file (NMR ensembles) is read, so
    counts describe one structure rather than an ensemble sum.
    """
    atoms: list[dict] = []
    in_later_model = False

    for line_no, line in enumerate(text_content.splitlines(), start=1):
        record = line[:6].strip()

        if record == "MODEL":
            model_num = line[10:14].strip()
            in_later_model = model_num not in ("", "1")
            continue
        if record == "ENDMDL":
            in_later_model = True
            continue
        if in_later_model or record not in ("ATOM", "HETATM"):
            continue

        if len(line) < 54:
            return [], (
                f"Malformed {record} record at line {line_no}: line is "
                f"{len(line)} characters, but coordinates require at least 54."
            )
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except ValueError:
            return [], (
                f"Malformed coordinates at line {line_no}: "
                f"{line[30:54]!r} is not three numbers."
            )

        alt_loc = line[16:17].strip()
        if alt_loc not in ("", "A"):
            continue  # count one conformer only

        atoms.append(
            {
                "serial": line[6:11].strip(),
                "name": line[12:16].strip(),
                "res_name": line[17:20].strip(),
                "chain_id": line[21:22].strip() or " ",
                "res_seq": line[22:27].strip(),  # includes insertion code
                "x": x, "y": y, "z": z,
                "is_hetatm": record == "HETATM",
            }
        )
    return atoms, None


def _parse_pdb_header(text_content: str) -> tuple[str | None, str | None, float | None, str | None]:
    title_parts: list[str] = []
    compnd_molecule: str | None = None
    method: str | None = None
    resolution: float | None = None
    pdb_id: str | None = None

    for line in text_content.splitlines():
        record = line[:6].strip()
        if record == "HEADER":
            candidate = line[62:66].strip()
            if candidate:
                pdb_id = candidate
        elif record == "TITLE":
            title_parts.append(line[10:80].strip())
        elif record == "COMPND" and compnd_molecule is None:
            body = line[10:80].strip()
            if body.upper().startswith("MOLECULE:"):
                compnd_molecule = body.split(":", 1)[1].strip().rstrip(";")
            elif ":" in body and body.split(":", 1)[0].strip().isdigit():
                inner = body.split(":", 1)[1].strip()
                if inner.upper().startswith("MOLECULE:"):
                    compnd_molecule = inner.split(":", 1)[1].strip().rstrip(";")
        elif record == "EXPDTA" and method is None:
            method = line[10:80].strip() or None
        elif line.startswith("REMARK   2") and "RESOLUTION." in line:
            match = re.search(r"RESOLUTION\.\s+([0-9]+\.?[0-9]*)\s*ANGSTROM", line)
            if match:
                resolution = float(match.group(1))

    title = " ".join(p for p in title_parts if p).strip() or compnd_molecule
    return (title or None), method, resolution, pdb_id


# ---------------------------------------------------------------------------
# mmCIF — intentionally minimal: the _atom_site loop plus a few header items
# ---------------------------------------------------------------------------
def _parse_mmcif_atoms(text_content: str) -> tuple[list[dict], str | None]:
    lines = text_content.splitlines()
    atoms: list[dict] = []

    i = 0
    while i < len(lines):
        if lines[i].strip() != "loop_":
            i += 1
            continue

        # Collect the column headers of this loop.
        j = i + 1
        headers: list[str] = []
        while j < len(lines) and lines[j].strip().startswith("_"):
            headers.append(lines[j].strip())
            j += 1
        if not any(h.startswith("_atom_site.") for h in headers):
            i = j
            continue

        index = {name: pos for pos, name in enumerate(headers)}

        def col(key: str) -> int | None:
            return index.get(f"_atom_site.{key}")

        needed = {k: col(k) for k in ("Cartn_x", "Cartn_y", "Cartn_z")}
        if any(v is None for v in needed.values()):
            return [], "mmCIF _atom_site loop has no Cartn_x/y/z coordinate columns."

        c_group = col("group_PDB")
        c_chain = col("auth_asym_id") if col("auth_asym_id") is not None else col("label_asym_id")
        c_res = col("auth_seq_id") if col("auth_seq_id") is not None else col("label_seq_id")
        c_resname = col("auth_comp_id") if col("auth_comp_id") is not None else col("label_comp_id")
        c_name = col("auth_atom_id") if col("auth_atom_id") is not None else col("label_atom_id")
        c_serial = col("id")
        c_alt = col("label_alt_id")
        c_model = col("pdbx_PDB_model_num")

        while j < len(lines):
            row = lines[j].strip()
            if not row or row.startswith("#") or row.startswith("loop_") or row.startswith("_"):
                break
            fields = _split_cif_row(row)
            if len(fields) < len(headers):
                j += 1
                continue

            if c_model is not None and fields[c_model] not in (".", "?", "1"):
                j += 1
                continue
            if c_alt is not None and fields[c_alt] not in (".", "?", "A"):
                j += 1
                continue
            try:
                x = float(fields[needed["Cartn_x"]])
                y = float(fields[needed["Cartn_y"]])
                z = float(fields[needed["Cartn_z"]])
            except ValueError:
                return [], f"Malformed mmCIF coordinates in _atom_site row: {row[:60]!r}"

            group = fields[c_group] if c_group is not None else "ATOM"
            atoms.append(
                {
                    "serial": fields[c_serial] if c_serial is not None else "",
                    "name": fields[c_name].strip('"') if c_name is not None else "",
                    "res_name": fields[c_resname] if c_resname is not None else "",
                    "chain_id": fields[c_chain] if c_chain is not None else " ",
                    "res_seq": fields[c_res] if c_res is not None else "",
                    "x": x, "y": y, "z": z,
                    "is_hetatm": group.upper() == "HETATM",
                }
            )
            j += 1
        i = j

    if not atoms:
        return [], "No _atom_site records found — this mmCIF file contains no atoms."
    return atoms, None


def _split_cif_row(row: str) -> list[str]:
    """Split an mmCIF data row, honouring single/double-quoted values."""
    return [
        m.group(1) or m.group(2) or m.group(3)
        for m in re.finditer(r"'([^']*)'|\"([^\"]*)\"|(\S+)", row)
    ]


def _parse_mmcif_header(text_content: str) -> tuple[str | None, str | None, float | None, str | None]:
    def item(key: str) -> str | None:
        match = re.search(
            rf"^{re.escape(key)}\s+(?:'([^']*)'|\"([^\"]*)\"|(\S+))",
            text_content,
            re.MULTILINE,
        )
        if not match:
            return None
        value = match.group(1) or match.group(2) or match.group(3)
        return None if value in (".", "?") else value.strip()

    title = item("_struct.title")
    method = item("_exptl.method")
    pdb_id = item("_entry.id")
    resolution_raw = item("_refine.ls_d_res_high") or item(
        "_em_3d_reconstruction.resolution"
    )
    try:
        resolution = float(resolution_raw) if resolution_raw else None
    except ValueError:
        resolution = None
    return title, method, resolution, pdb_id


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _summarise_chains(atoms: list[dict]) -> list[ChainInfo]:
    by_chain: dict[str, dict] = {}
    for atom in atoms:
        chain = atom["chain_id"] or " "
        entry = by_chain.setdefault(chain, {"residues": set(), "atoms": 0})
        entry["residues"].add(atom["res_seq"])
        entry["atoms"] += 1

    chains: list[ChainInfo] = []
    for chain_id, entry in sorted(by_chain.items()):
        numeric = []
        for residue in entry["residues"]:
            match = re.match(r"-?\d+", residue)
            if match:
                numeric.append(int(match.group()))
        chains.append(
            ChainInfo(
                chain_id=chain_id.strip() or "_",
                residue_count=len(entry["residues"]),
                atom_count=entry["atoms"],
                first_residue=min(numeric) if numeric else None,
                last_residue=max(numeric) if numeric else None,
            )
        )
    return chains


def _rdkit_can_read(text_content: str, structure_format: str) -> bool:
    """Independent second opinion. Advisory only — never a rejection reason."""
    if structure_format != "pdb":
        return True  # RDKit's mmCIF support isn't a fair test here.
    try:
        mol = Chem.MolFromPDBBlock(text_content, sanitize=False, removeHs=False)
        return mol is not None and mol.GetNumAtoms() > 0
    except Exception:
        return False
