#!/usr/bin/env python3
"""Pre-download ZINC20 library chunks so campaigns need no internet and can sample from a bigger pool.

    .venv/bin/python scripts/prefetch_zinc.py                 # chunks 1-20, ZINC_PREFIX_MB each (default 3 MB)
    .venv/bin/python scripts/prefetch_zinc.py --mb 20 --chunks 1,2,3   # ~20 MB prefix of three chunks

Each ZINC20 archive chunk is ~450 MB (~10 million molecules) and is ordered by ID; only the first `--mb` megabytes
of a chunk are fetched (HTTP range request on the gzip stream). 3 MB gives ~400,000 molecules; 20 MB gives ~2.7 million.
Files are cached under <DATA_DIR>/library_cache and reused by every campaign. Nothing is written to the database.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.science import library as lib  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default=",".join(str(i) for i in range(1, lib.ZINC_CHUNKS + 1)))
    ap.add_argument("--mb", type=int, default=None, help="prefix size per chunk in MB (default: ZINC_PREFIX_MB)")
    a = ap.parse_args()
    mb = a.mb or lib.settings.zinc_prefix_mb
    chunks = [int(c) for c in a.chunks.split(",") if c.strip()]
    total = 0
    for c in chunks:
        try:
            r = lib.prefetch_zinc([c], mb)[0]
            total += r["molecules"]
            print(f"chunk {c:2d}: {r['molecules']:>9,} molecules cached ({mb} MB prefix)", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"chunk {c:2d}: FAILED {exc}", file=sys.stderr)
    print(f"total cached this run: {total:,} molecules in {lib.settings.zinc_cache_dir}")


if __name__ == "__main__":
    main()
