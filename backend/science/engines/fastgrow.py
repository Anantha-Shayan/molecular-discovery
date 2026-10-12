"""
BioSolveIT FastGrow adapter: structure-based fragment growing ("analogue generation").

Documented CLI (FastGrow user guide 1.6, sections 3-5):
    fastgrow -d fragments.fastgrowdb -p protein.pdb -c core_with_linker.sdf -o grown.sdf -n N --thread-count T
    fastgrow_db_creator -i fragments_with_one_linker.smi -o fragments.fastgrowdb

What we feed it
  * core ligand: a scored pose (3D, with hydrogens) in which ONE hydrogen has been turned into the linker
    atom `R`. The H keeps its coordinates, so the exit vector is the real C-H direction in the pocket and
    the ring/aromaticity of the parent is untouched.
  * exit vectors are chosen automatically from the pose (see `choose_exit_vectors`): hydrogens that point
    into free space inside the pocket. This is a heuristic; a chemist would choose by design intent.
  * fragments: the BioSolveIT 120k library when `FASTGROW_DB` points at one (login-gated download), otherwise
    a small database built here from BRICS fragments of the campaign's own library.

What comes out is a set of GENERATED molecules, scored by FastGrow's shape/optimised score
(`BIOSOLVEIT.FASTGROW_OPTIMIZED_SCORE`, more negative = better). They are computational suggestions:
not catalogue compounds, not synthesised, not validated, and novelty versus the literature is not checked.
"""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import numpy as np
from rdkit import Chem
from rdkit.Chem import BRICS

from ...tools import proc, registry
from .. import sdf as sdflib

TAG_SCORE = "BIOSOLVEIT.FASTGROW_OPTIMIZED_SCORE"
TAG_SCORE_RAW = "BIOSOLVEIT.FASTGROW_SCORE"
MAX_FRAGMENT_HEAVY = 12
MIN_FRAGMENT_HEAVY = 2


@dataclasses.dataclass
class ExitVector:
    h_index: int          # index of the hydrogen (becomes the linker)
    parent_index: int     # heavy atom it is attached to
    contacts_6A: int      # protein heavy atoms within 6 A of the H (pocket depth)
    nearest_A: float      # distance to the nearest protein atom (free space)


def protein_coords(pdb: Path) -> np.ndarray:
    pts = []
    for ln in pdb.read_text(errors="replace").splitlines():
        if ln.startswith(("ATOM  ", "HETATM")) and ln[76:78].strip() not in ("H", "D"):
            try:
                pts.append((float(ln[30:38]), float(ln[38:46]), float(ln[46:54])))
            except ValueError:
                continue
    return np.array(pts)


def choose_exit_vectors(pose: Chem.Mol, protein_pdb: Path, n: int = 2) -> list[ExitVector]:
    """Hydrogens that point into open space within the pocket, most buried-but-free first.

    A good growing position has room (nothing within 2.8 A of the H, so a fragment can be attached) and
    pocket context (many protein atoms within 6 A, so the new group has something to interact with).
    """
    prot = protein_coords(protein_pdb)
    if len(prot) == 0:
        return []
    conf = pose.GetConformer()
    cands: list[ExitVector] = []
    for a in pose.GetAtoms():
        if a.GetAtomicNum() != 1 or a.GetDegree() != 1:
            continue
        nb = a.GetNeighbors()[0]
        if nb.GetAtomicNum() == 1:
            continue
        p = np.array(conf.GetAtomPosition(a.GetIdx()))
        d = np.linalg.norm(prot - p, axis=1)
        nearest = float(d.min())
        contacts = int((d < 6.0).sum())
        if nearest >= 2.8 and contacts >= 8:
            cands.append(ExitVector(a.GetIdx(), nb.GetIdx(), contacts, nearest))
    cands.sort(key=lambda c: (-c.contacts_6A, -c.nearest_A))
    chosen: list[ExitVector] = []
    for c in cands:  # keep growth points at least 3 bonds-ish apart (different parents)
        if all(c.parent_index != o.parent_index for o in chosen):
            chosen.append(c)
        if len(chosen) >= n:
            break
    return chosen


def core_with_linker(pose: Chem.Mol, exit_vector: ExitVector) -> Chem.Mol:
    """The pose with the chosen hydrogen turned into the linker atom R (dummy atom)."""
    rw = Chem.RWMol(pose)
    rw.GetAtomWithIdx(exit_vector.h_index).SetAtomicNum(0)
    core = rw.GetMol()
    core.UpdatePropertyCache(strict=False)
    return core


def fragments_from_smiles(smiles: list[str]) -> list[str]:
    """Mono-attachment BRICS fragments (2-12 heavy atoms), as `[R]`-marked SMILES."""
    out: set[str] = set()
    for smi in smiles:
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        for frag in BRICS.BRICSDecompose(mol, minFragmentSize=2, keepNonLeafNodes=False):
            fm = Chem.MolFromSmiles(frag)
            if fm is None:
                continue
            dummies = [a for a in fm.GetAtoms() if a.GetAtomicNum() == 0]
            heavy = fm.GetNumHeavyAtoms() - 1
            if len(dummies) != 1 or not MIN_FRAGMENT_HEAVY <= heavy <= MAX_FRAGMENT_HEAVY:
                continue
            dummies[0].SetIsotope(0)
            out.add(Chem.MolToSmiles(fm).replace("*", "[R]"))
    return sorted(out)


def fragment_pool(library_smiles: list[str], extra_zinc: int = 1500) -> tuple[list[str], str]:
    """SMILES to derive fragments from: the campaign library + known drugs + a cached ZINC20 sample.

    A campaign library of a few dozen molecules yields too few fragments to grow anything, so the pool is
    widened with a fixed, seeded ZINC sample (cached, so it is built once). Offline, it falls back to the
    campaign library plus the built-in drug list.
    """
    from ..library import Filters, demo_library, fetch_zinc20
    pool = list(library_smiles)
    note = "campaign library"
    try:
        drugs, _ = demo_library(40, Filters())
        pool += [d["smiles"] for d in drugs]
        zinc, _ = fetch_zinc20(extra_zinc, 2024, Filters())
        pool += [z["smiles"] for z in zinc]
        note = f"campaign library + known drugs + {len(zinc)} ZINC20 molecules (seed 2024)"
    except Exception:  # noqa: BLE001 - offline: keep what we have
        note = "campaign library + known drugs (offline)"
    return pool, note


def build_fragment_db(smiles: list[str], cache_dir: Path, log_path: Path, threads: int = 4,
                      timeout: float = 900) -> tuple[Path, int]:
    """Create (or reuse) a .fastgrowdb from BRICS fragments of `smiles`. Returns (path, fragment count)."""
    frags = fragments_from_smiles(smiles)
    if len(frags) < 20:
        raise proc.ToolError(f"Only {len(frags)} usable fragments could be derived from the library; need at least 20.")
    key = hashlib.sha256("\n".join(frags).encode()).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    db = cache_dir / f"brics_{key}.fastgrowdb"
    if db.exists() and db.stat().st_size > 0:
        return db, len(frags)
    smi = cache_dir / f"brics_{key}.smi"
    smi.write_text("".join(f"{s} f{i}\n" for i, s in enumerate(frags)))
    creator = registry.require("fastgrow_db_creator")
    tmp = db.with_suffix(".tmp.fastgrowdb")
    tmp.unlink(missing_ok=True)
    proc.run([creator.path, "-i", str(smi), "-o", str(tmp), "--thread-count", str(threads), "-v", "2"],
             log_path=log_path, timeout=timeout, env=proc.base_env(registry.license_env()))
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise proc.ToolError("FastGrowDBCreator produced no database")
    tmp.replace(db)
    return db, len(frags)


@dataclasses.dataclass
class GrownMolecule:
    smiles: str
    score: float
    pose_text: str       # SDF record of the grown molecule (3D)
    heavy_atoms: int


def grow(core_sdf: Path, protein_pdb: Path, db: Path, out_sdf: Path, log_path: Path, n_solutions: int,
         threads: int, timeout: float, cancel_event=None) -> list[GrownMolecule]:
    tool = registry.require("fastgrow")
    out_sdf.unlink(missing_ok=True)
    proc.run(
        [tool.path, "-d", str(db), "-p", str(protein_pdb), "-c", str(core_sdf), "-o", str(out_sdf),
         "-n", str(n_solutions), "--thread-count", str(threads), "-v", "2"],
        log_path=log_path, timeout=timeout, cancel_event=cancel_event, env=proc.base_env(registry.license_env()),
    )
    if not out_sdf.exists():
        return []  # FastGrow writes nothing when the screening finds no fitting fragment
    try:
        records = sdflib.parse_sdf(out_sdf.read_text())
    except sdflib.SdfError as exc:
        raise proc.ToolError(f"FastGrow output is corrupt: {exc}") from exc
    grown = []
    for rec in records:
        raw = rec.props.get(TAG_SCORE) or rec.props.get(TAG_SCORE_RAW)
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        m = Chem.MolFromMolBlock(rec.molblock, removeHs=False, sanitize=True)
        if m is None:
            continue
        grown.append(GrownMolecule(Chem.MolToSmiles(Chem.RemoveHs(m)), score, rec.text, m.GetNumHeavyAtoms()))
    grown.sort(key=lambda g: g.score)
    return grown
