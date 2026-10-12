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
"""
from __future__ import annotations

import dataclasses
import random
import re
import urllib.request
import zlib
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, rdMolDescriptors
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


@dataclasses.dataclass
class Filters:
    min_heavy: int = 10
    max_heavy: int = 40
    min_mw: float = 150.0
    max_mw: float = 600.0
    max_rotatable: int = 12

    def describe(self) -> str:
        return (
            f"{self.min_heavy}-{self.max_heavy} heavy atoms, MW {self.min_mw:g}-{self.max_mw:g}, "
            f"<= {self.max_rotatable} rotatable bonds, elements C/H/N/O/F/P/S/Cl/Br/I"
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
    if not filters.min_heavy <= heavy <= filters.max_heavy:
        raise ValueError(f"{heavy} heavy atoms outside {filters.min_heavy}-{filters.max_heavy}")
    if not filters.min_mw <= mw <= filters.max_mw:
        raise ValueError(f"molecular weight {mw:.0f} outside {filters.min_mw:g}-{filters.max_mw:g}")
    if rdMolDescriptors.CalcNumRotatableBonds(mol) > filters.max_rotatable:
        raise ValueError("too many rotatable bonds")
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
