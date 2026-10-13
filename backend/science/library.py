"""
Compound-library sources and standardisation.

Sources
  zinc20   ZINC20 (Irwin et al. 2020) "Deep Docking" SMILES set, streamed from
           files.docking.org. ZINC is a catalogue of purchasable/enumerated
           compounds: every hit is a catalogue entry, NOT a novel molecule.
  demo     the small built-in list of known drugs/scaffolds (offline use).
  upload   user-supplied SMILES text ("SMILES [name]" per line).

Only the first few MB of a ZINC archive chunk are downloaded (HTTP range
request on a gzip stream), cached under DATA_DIR/library_cache, and sampled
with a fixed seed so a campaign is reproducible. The archive is ordered by ID,
so this is a convenience sample, not a random draw from all of ZINC; the UI
and the artifact metadata say so.

Measured directly (7 of 20 chunks checked, RDKit heavy-atom count on a
400-500 molecule sample of each): every chunk clusters inside roughly 16-31
heavy atoms, none spanning true fragments (<15 atoms) or large compounds
(>35). A fragment-like Filters() will very likely keep nothing from this
source, not because the filter is broken but because this archive doesn't
contain that size class — see docs/SCIENCE_NOTES.md.

Filters (Filters dataclass below) are user-configurable per campaign and
modelled on the same dimensions ZINC's own tranche grid / "/substances"
search page uses (heavy atoms, MW, logP, rotatable bonds, H-bond donor/
acceptor count, TPSA, reactivity level). Purchasability tier, pH state and
net charge are NOT offered: they need live ZINC catalogue/vendor metadata
this flat SMILES archive does not carry, so faking them would be dishonest.
"""
from __future__ import annotations

import dataclasses
import random
import re
import urllib.request
import zlib
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, FilterCatalog, rdMolDescriptors
from rdkit.Chem.MolStandardize import rdMolStandardize

from ..config import settings
from ..logging_config import get_logger
from ..stages.library import DEMO_LIBRARY

log = get_logger("library")
RDLogger.DisableLog("rdApp.*")

ZINC_BASE = "https://files.docking.org/zinc20-ML/smiles/ZINC20_smiles_chunk_{n}.tar.gz"
ZINC_CHUNKS = 20
ZINC_CHUNK_SIZE_MB = 450       # each archive chunk is ~450 MB compressed (~10M molecules); only a prefix is fetched
_ZINC_CACHE_RE = re.compile(r"^zinc20_chunk(\d+)_(\d+)\.smi$")
ALLOWED_ELEMENTS = {1, 6, 7, 8, 9, 15, 16, 17, 35, 53}
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

_largest = rdMolStandardize.LargestFragmentChooser()
_uncharge = rdMolStandardize.Uncharger()

# --- Reactivity levels, modelled on ZINC's own published tranche definitions
# (wiki.docking.org/index.php/Tranche_Browser) as closely as a public SMARTS-based
# reimplementation can get. ZINC's internal rule set is not published, so "clean" and
# "anodyne" here are OUR reading of its prose description, not a byte-for-byte copy:
#   mild    (ZINC's own default) - no extra exclusion; PAINS and weakly reactive groups allowed.
#   clean   - excludes aldehydes, thiols and Michael acceptors (the "weakly reactive" groups
#             ZINC names explicitly), using real RDKit SMARTS matches against the structure.
#   anodyne - clean's exclusions, PLUS RDKit's full PAINS filter catalogue (A+B+C, 480 patterns).
#             ZINC distinguishes "PAINS with vs. without a clear mechanism"; that distinction is
#             not publicly operationalisable, so anodyne here removes ALL PAINS catalogue hits.
REACTIVITY_LEVELS = ("mild", "clean", "anodyne")
_REACTIVE_GROUP_SMARTS = {
    "aldehyde": "[CX3H1](=O)[#6]",
    "thiol": "[#16X2H]",
    "michael_acceptor": "[CX3]=[CX3][CX3]=[OX1,NX3]",  # alpha,beta-unsaturated carbonyl
}
_REACTIVE_GROUPS = {name: Chem.MolFromSmarts(s) for name, s in _REACTIVE_GROUP_SMARTS.items()}

_pains_params = FilterCatalog.FilterCatalogParams()
_pains_params.AddCatalog(FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS)
_PAINS_CATALOG = FilterCatalog.FilterCatalog(_pains_params)


def _reactivity_violation(mol: Chem.Mol, level: str) -> str | None:
    """None if `mol` passes `level`, else the reason it was rejected."""
    if level == "mild":
        return None
    for name, patt in _REACTIVE_GROUPS.items():
        if mol.HasSubstructMatch(patt):
            return f"reactive group ({name.replace('_', ' ')}) excluded by reactivity={level}"
    if level == "anodyne" and _PAINS_CATALOG.HasMatch(mol):
        return "matches a PAINS pattern, excluded by reactivity=anodyne"
    return None


@dataclasses.dataclass
class Filters:
    # Structural / physicochemical — computed directly from the molecule, same dimensions
    # ZINC's own tranche grid and /substances search use (molecular weight, logP, H-bond
    # count, rotatable bonds, TPSA, size).
    min_heavy: int = 10
    max_heavy: int = 40
    min_mw: float = 150.0
    max_mw: float = 600.0
    min_logp: float = -5.0
    max_logp: float = 5.0
    max_rotatable: int = 12
    max_hbd: int = 5
    max_hba: int = 10
    max_tpsa: float = 140.0
    reactivity: str = "mild"   # one of REACTIVITY_LEVELS

    def __post_init__(self):
        if self.reactivity not in REACTIVITY_LEVELS:
            raise ValueError(f"reactivity must be one of {REACTIVITY_LEVELS}")

    def describe(self) -> str:
        return (
            f"{self.min_heavy}-{self.max_heavy} heavy atoms, MW {self.min_mw:g}-{self.max_mw:g}, "
            f"logP {self.min_logp:g}-{self.max_logp:g}, <= {self.max_rotatable} rotatable bonds, "
            f"<= {self.max_hbd} H-bond donors, <= {self.max_hba} H-bond acceptors, "
            f"TPSA <= {self.max_tpsa:g} Å², reactivity={self.reactivity}, "
            f"elements C/H/N/O/F/P/S/Cl/Br/I "
            f"(purchasability/pH/charge-state are NOT filterable: our ZINC source is a flat SMILES "
            f"archive with no vendor/tranche metadata, unlike the live ZINC website)"
        )

    @classmethod
    def from_params(cls, p: dict) -> "Filters":
        """Build from campaign params (backend/campaign/params.py's filter_* keys)."""
        return cls(
            min_heavy=p["filter_min_heavy"], max_heavy=p["filter_max_heavy"],
            min_mw=p["filter_min_mw"], max_mw=p["filter_max_mw"],
            min_logp=p["filter_min_logp"], max_logp=p["filter_max_logp"],
            max_rotatable=p["filter_max_rotatable"], max_hbd=p["filter_max_hbd"],
            max_hba=p["filter_max_hba"], max_tpsa=p["filter_max_tpsa"],
            reactivity=p["filter_reactivity"],
        )


class LibraryError(RuntimeError):
    pass


def standardize(smiles: str, filters: Filters) -> dict:
    """Parse, keep the largest fragment, neutralise, apply property filters.

    Raises ValueError with a human-readable reason on rejection.
    """
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        raise ValueError("SMILES could not be parsed")
    mol = _largest.choose(mol)
    try:
        mol = _uncharge.uncharge(mol)
    except Exception:  # noqa: BLE001 - keep the molecule as given
        pass
    Chem.SanitizeMol(mol)
    if any(a.GetAtomicNum() not in ALLOWED_ELEMENTS for a in mol.GetAtoms()):
        raise ValueError("contains elements unsupported by the docking tools")
    heavy = mol.GetNumHeavyAtoms()
    mw = Descriptors.MolWt(mol)
    logp = Descriptors.MolLogP(mol)
    if not filters.min_heavy <= heavy <= filters.max_heavy:
        raise ValueError(f"{heavy} heavy atoms outside {filters.min_heavy}-{filters.max_heavy}")
    if not filters.min_mw <= mw <= filters.max_mw:
        raise ValueError(f"molecular weight {mw:.0f} outside {filters.min_mw:g}-{filters.max_mw:g}")
    if not filters.min_logp <= logp <= filters.max_logp:
        raise ValueError(f"logP {logp:.1f} outside {filters.min_logp:g}-{filters.max_logp:g}")
    if rdMolDescriptors.CalcNumRotatableBonds(mol) > filters.max_rotatable:
        raise ValueError("too many rotatable bonds")
    if rdMolDescriptors.CalcNumHBD(mol) > filters.max_hbd:
        raise ValueError("too many H-bond donors")
    if rdMolDescriptors.CalcNumHBA(mol) > filters.max_hba:
        raise ValueError("too many H-bond acceptors")
    if Descriptors.TPSA(mol) > filters.max_tpsa:
        raise ValueError("TPSA above limit")
    reactivity_reason = _reactivity_violation(mol, filters.reactivity)
    if reactivity_reason:
        raise ValueError(reactivity_reason)
    return {
        "smiles": Chem.MolToSmiles(mol),
        "inchikey": Chem.MolToInchiKey(mol),
        "heavy_atoms": heavy,
        "molecular_weight": round(mw, 2),
    }


def zinc_prefix_bytes() -> int:
    return settings.zinc_prefix_mb * 1_000_000


def _read_zinc_prefix(chunk: int, cache_dir: Path, prefix_bytes: int | None = None) -> list[tuple[str, str]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    prefix_bytes = prefix_bytes or zinc_prefix_bytes()
    cache = cache_dir / f"zinc20_chunk{chunk}_{prefix_bytes}.smi"
    if not cache.exists():
        url = ZINC_BASE.format(n=chunk)
        req = urllib.request.Request(url, headers={"Range": f"bytes=0-{prefix_bytes}", "User-Agent": "molecular-discovery-platform/1.0 (curl-compatible)"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https host
                raw = resp.read(prefix_bytes + 1)
        except OSError as exc:
            raise LibraryError(f"Could not download ZINC20 chunk {chunk}: {exc}") from exc
        try:
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        except zlib.error as exc:
            raise LibraryError("ZINC20 download was not a valid gzip stream") from exc
        body = data[512:].decode("ascii", errors="ignore")  # skip the 512-byte tar header
        lines = body.splitlines()[:-1]  # last line is truncated
        tmp = cache.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(cache)
    out = []
    for ln in cache.read_text().splitlines():
        parts = ln.split()
        if len(parts) >= 2 and _ID_RE.match(parts[1]):
            out.append((parts[0], parts[1]))
    return out


def zinc_cache_status() -> dict:
    """What is already on disk for the ZINC20 source (no network access)."""
    d = settings.zinc_cache_dir
    chunks = []
    if d.is_dir():
        for f in sorted(d.iterdir()):
            m = _ZINC_CACHE_RE.match(f.name)
            if m:
                with f.open("rb") as fh:
                    n = sum(1 for _ in fh)
                chunks.append({"chunk": int(m.group(1)), "prefix_mb": int(m.group(2)) // 1_000_000, "molecules": n,
                               "size_mb": round(f.stat().st_size / 1e6, 1)})
    return {
        "source": "ZINC20 (Deep Docking set) at files.docking.org",
        "chunks_total": ZINC_CHUNKS,
        "chunk_size_mb": ZINC_CHUNK_SIZE_MB,
        "prefix_mb_setting": settings.zinc_prefix_mb,
        "cached": chunks,
        "cached_molecules": sum(c["molecules"] for c in chunks),
        "cache_dir": "<data>/library_cache",
    }


def prefetch_zinc(chunks: list[int], prefix_mb: int) -> list[dict]:
    """Download and cache prefixes of the given chunks (for offline use / bigger pools)."""
    out = []
    for c in chunks:
        pool = _read_zinc_prefix(c, settings.zinc_cache_dir, prefix_mb * 1_000_000)
        out.append({"chunk": c, "molecules": len(pool)})
    return out


def fetch_zinc20(count: int, seed: int, filters: Filters) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    chunk = rng.randint(1, ZINC_CHUNKS)
    pool = _read_zinc_prefix(chunk, settings.zinc_cache_dir)
    if not pool:
        raise LibraryError("ZINC20 download contained no SMILES")
    # Strided start so different seeds sample different regions of the prefix.
    stride = max(1, len(pool) // max(count * 8, 1))
    offset = rng.randrange(stride)
    candidates = pool[offset::stride]
    rng.shuffle(candidates)
    return _collect(
        ((smi, zid) for smi, zid in candidates), count, filters, "zinc20",
        {"chunk": chunk, "pool_size": len(pool), "seed": seed},
    )


def demo_library(count: int, filters: Filters) -> tuple[list[dict], dict]:
    return _collect(
        ((e.smiles, e.name) for e in DEMO_LIBRARY), count, filters, "demo_library", {}
    )


def parse_upload(text: str, count: int, filters: Filters) -> tuple[list[dict], dict]:
    def rows():
        for i, ln in enumerate(text.splitlines(), start=1):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split(None, 1)
            name = parts[1].strip() if len(parts) > 1 else f"upload-{i}"
            yield parts[0], name[:64]
    return _collect(rows(), count, filters, "upload", {})


def _collect(rows, count: int, filters: Filters, source: str, meta: dict) -> tuple[list[dict], dict]:
    kept: list[dict] = []
    seen: set[str] = set()
    rejected = {"duplicate": 0, "invalid_or_filtered": 0}
    reasons: dict[str, int] = {}
    scanned = 0
    for smi, ext_id in rows:
        scanned += 1
        try:
            rec = standardize(smi, filters)
        except ValueError as exc:
            rejected["invalid_or_filtered"] += 1
            key = re.sub(r"\d+(\.\d+)?", "N", str(exc))
            reasons[key] = reasons.get(key, 0) + 1
            continue
        if rec["inchikey"] in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(rec["inchikey"])
        rec.update(source=source, external_id=ext_id)
        kept.append(rec)
        if len(kept) >= count:
            break
    if not kept:
        raise LibraryError("No usable molecules after standardisation and filtering")
    meta = {
        **meta, "source": source, "requested": count, "kept": len(kept), "scanned": scanned,
        "rejected": rejected, "reject_reasons": reasons, "filters": filters.describe(),
    }
    return kept, meta
