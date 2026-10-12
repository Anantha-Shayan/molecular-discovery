"""
Discovery and diagnostics of external scientific tools.

Nothing here fakes availability: a tool is "available" only if its
executable was found AND (for licensed tools) the vendor's own
`--license-info` diagnostic reports a valid key. The result is what the API
and UI show, so a reviewer can see exactly which engines are usable.

The license file's CONTENTS are never read by this application. Only the
directory path is passed to the vendor executable through the
BIOSOLVE_LICENSE_FILE environment variable, as the vendor documents.
"""
from __future__ import annotations

import dataclasses
import functools
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import settings
from . import proc

LICENSED = {"flexx", "hydescorer", "fastgrow", "fastgrow_db_creator"}


@dataclasses.dataclass
class ToolStatus:
    key: str
    name: str
    available: bool
    path: str | None = None
    version: str | None = None
    licensed: bool = False
    license_ok: bool | None = None
    license_detail: str | None = None
    reason: str | None = None  # why unavailable, in plain words

    def public(self) -> dict:
        d = dataclasses.asdict(self)
        # Don't leak the host directory layout through the API.
        d["path"] = Path(self.path).name if self.path else None
        return d


_NAMES = {
    "vina": ("AutoDock Vina", "vina"),
    "flexx": ("BioSolveIT FlexX", "flexx"),
    "hydescorer": ("BioSolveIT HYDE", "hydescorer"),
    "fastgrow": ("BioSolveIT FastGrow", "fastgrow"),
    "fastgrow_db_creator": ("BioSolveIT FastGrowDBCreator", "fastgrow_db_creator"),
}


def license_env() -> dict[str, str]:
    if settings.biosolve_license_file:
        return {"BIOSOLVE_LICENSE_FILE": settings.biosolve_license_file}
    return {}


def find_binary(key: str) -> str | None:
    explicit = getattr(settings, f"{key}_bin")
    if explicit:
        p = Path(explicit).expanduser()
        return str(p) if p.is_file() else None
    exe = _NAMES[key][1]
    found = shutil.which(exe)
    if found:
        return found
    if settings.tools_dir and settings.tools_dir.is_dir():
        # Vendor tarballs unpack to <tool>-<ver>-Linux-x64/<exe>.
        for candidate in sorted(settings.tools_dir.glob(f"**/{exe}")):
            if candidate.is_file() and candidate.stat().st_mode & 0o111:
                return str(candidate)
    return None


def _run_quick(argv: list[str], env: dict[str, str]) -> str:
    try:
        out = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, timeout=30, env=env, stdin=subprocess.DEVNULL
        )
        return (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"ERROR: {exc.__class__.__name__}"


_VERSION_RE = re.compile(r"(?:version:?\s*|\bv)([0-9]+\.[0-9][0-9A-Za-z.\-]*)", re.I)


def _probe(key: str) -> ToolStatus:
    name, _ = _NAMES[key]
    path = find_binary(key)
    status = ToolStatus(key=key, name=name, available=False, path=path, licensed=key in LICENSED)
    if not path:
        status.reason = (
            "Executable not found. Install it and set "
            f"{key.upper()}_BIN or TOOLS_DIR (see docs/DEPLOYMENT.md)."
        )
        return status
    env = proc.base_env(license_env())
    ver_out = _run_quick([path, "--version"], env)
    m = _VERSION_RE.search(ver_out)
    status.version = m.group(1) if m else (ver_out.strip().splitlines() or [None])[0]
    if key in LICENSED:
        lic = _run_quick([path, "--license-info"], env)
        valid = re.search(r">>\s*Valid key,?\s*(expires on [^\n]+)?", lic)
        status.license_ok = bool(valid)
        status.license_detail = (
            valid.group(0).replace(">>", "").strip() if valid else "No valid license key found"
        )
        if not valid:
            status.reason = (
                "License check failed (`--license-info`). Place the license in a protected "
                "directory and set BIOSOLVE_LICENSE_FILE to that directory."
            )
            return status
    status.available = True
    return status


@functools.lru_cache(maxsize=1)
def _cached(stamp: int) -> dict[str, ToolStatus]:
    return {k: _probe(k) for k in _NAMES}


def tool_statuses(refresh: bool = False) -> dict[str, ToolStatus]:
    """All tools' status; cached for 60 s (probing spawns subprocesses)."""
    if refresh:
        _cached.cache_clear()
    return _cached(int(time.time() // 60))


def get(key: str) -> ToolStatus:
    return tool_statuses()[key]


def require(key: str) -> ToolStatus:
    status = get(key)
    if not status.available:
        raise proc.ToolError(f"{status.name} is not available: {status.reason}")
    return status
