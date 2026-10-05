"""
Filesystem artifact storage.

Two rules, both for deployment safety:

1. **The database stores paths relative to DATA_DIR.** Absolute paths bake
   the host's directory layout into every row, so moving the data volume,
   recreating the container or changing DATA_DIR would orphan every
   artifact. Relative paths survive all three.

2. **Every path is resolved through `resolve()`**, which refuses anything
   that escapes DATA_DIR. Paths are only ever built by the server from
   generated IDs (never from user filenames), so this is defence in depth:
   a corrupted or tampered row cannot turn into an arbitrary file read.
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import settings


class StoragePathError(ValueError):
    """A stored path resolves outside the data directory."""


def data_dir() -> Path:
    return settings.data_dir


def resolve(stored_path: str) -> Path:
    """Absolute path for a stored (relative) artifact path, jail-checked.

    Legacy absolute paths are accepted only if they already sit inside
    DATA_DIR, so older rows keep working without opening a hole.
    """
    root = data_dir().resolve()
    candidate = Path(stored_path)
    full = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if full != root and root not in full.parents:
        raise StoragePathError("Artifact path is outside the data directory")
    return full


def to_stored(path: Path | str) -> str:
    """Relative-to-DATA_DIR form of an absolute path, for persisting."""
    root = data_dir().resolve()
    full = Path(path).resolve()
    try:
        return full.relative_to(root).as_posix()
    except ValueError as exc:
        raise StoragePathError("Refusing to store a path outside the data directory") from exc


def ensure_dir(*parts: str) -> Path:
    """Create and return DATA_DIR/<parts>, jail-checked."""
    path = resolve(os.path.join(*parts)) if parts else data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_writable() -> bool:
    """True if the data directory exists (or can be created) and accepts writes."""
    try:
        root = data_dir()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-check"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False
