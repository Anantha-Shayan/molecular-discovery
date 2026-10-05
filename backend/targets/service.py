"""
Target intake: turn a user-supplied structure into a validated, persisted
Target record plus a stored PDB artifact.

Three intake routes, one code path after parsing:

    upload   ──┐
    PDB ID   ──┼──> parse_structure() ──> validate ──> persist file + Target
    demo     ──┘

The demo route reads a fixture committed to the repo, so the whole flow
works with no network access. The PDB-ID route fetches from RCSB and
reports a clear error when that isn't reachable, rather than silently
substituting something else.
"""
from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request

from sqlalchemy.orm import Session

from .. import models
from .structure import ParsedStructure, parse_structure

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(REPO_ROOT, "data")
FIXTURE_DIR = os.path.join(DATA_DIR, "fixtures")
TARGET_STORE = os.path.join(DATA_DIR, "targets")

RCSB_URL = "https://files.rcsb.org/download/{pdb_id}.pdb"
RCSB_TIMEOUT_SECONDS = 15

# The bundled demo target. See data/fixtures/README.md for provenance.
DEMO_FIXTURE_FILENAME = "demo_kras_g12d_7rpz.pdb"
DEMO_TARGET_NAME = "KRAS G12D"
DEMO_PDB_ID = "7RPZ"


class TargetIntakeError(Exception):
    """Input was rejected. `checks` carries the per-gate detail for the UI."""

    def __init__(self, message: str, checks: list[dict] | None = None, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.checks = checks or []
        self.status_code = status_code


def demo_fixture_path() -> str:
    return os.path.join(FIXTURE_DIR, DEMO_FIXTURE_FILENAME)


def load_demo_structure() -> bytes:
    path = demo_fixture_path()
    if not os.path.exists(path):
        raise TargetIntakeError(
            f"Demo fixture missing at {path}. It ships with the repository — "
            "see data/fixtures/README.md.",
            status_code=500,
        )
    with open(path, "rb") as handle:
        return handle.read()


def fetch_pdb_by_id(pdb_id: str) -> bytes:
    """Fetch a structure from RCSB.

    Raises TargetIntakeError with an actionable message when the ID is
    unknown or the network is unavailable — the demo must never appear to
    succeed with a structure it didn't actually retrieve.
    """
    clean = pdb_id.strip().upper()
    if not clean.isalnum() or len(clean) != 4:
        raise TargetIntakeError(
            f"{pdb_id!r} is not a 4-character PDB ID (e.g. 7RPZ)."
        )

    # The bundled fixture answers for its own ID, so the demo's happy path
    # never depends on the network.
    if clean == DEMO_PDB_ID:
        return load_demo_structure()

    url = RCSB_URL.format(pdb_id=clean)
    try:
        with urllib.request.urlopen(url, timeout=RCSB_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise TargetIntakeError(
                f"RCSB has no PDB entry {clean}. Check the ID, or upload the "
                "structure file directly."
            ) from exc
        raise TargetIntakeError(
            f"RCSB returned HTTP {exc.code} for {clean}. Try again, upload the "
            "file directly, or use the bundled demo target."
        ) from exc
    except Exception as exc:  # URLError, timeout, DNS failure, offline
        raise TargetIntakeError(
            f"Could not reach RCSB to fetch {clean} ({exc.__class__.__name__}). "
            "Upload the structure file directly, or use the bundled demo "
            "target, which needs no network access."
        ) from exc


def create_target(
    session: Session,
    *,
    raw: bytes,
    filename: str | None,
    source: str,
    name: str | None = None,
    pdb_id: str | None = None,
    is_demo: bool = False,
    selected_chain: str | None = None,
) -> models.Target:
    """Validate a structure and persist it as a Target + pdb Artifact."""
    parsed = parse_structure(raw, filename)
    if not parsed.ok:
        raise TargetIntakeError(parsed.error_detail, parsed.checks_as_dicts())

    target = models.Target(
        name=name or _derive_name(parsed, filename, pdb_id),
        source=source,
        pdb_id=(pdb_id or parsed.pdb_id_in_file or None),
        original_filename=filename,
        structure_format=parsed.structure_format,
        checksum=parsed.checksum,
        file_size_bytes=parsed.file_size_bytes,
        title=parsed.title,
        experiment_method=parsed.experiment_method,
        resolution_a=parsed.resolution_a,
        chains=parsed.chains_as_dicts(),
        selected_chain=selected_chain or (parsed.chains[0].chain_id if parsed.chains else None),
        residue_count=parsed.residue_count,
        atom_count=parsed.atom_count,
        ligands=parsed.ligands,
        validation_status="PASSED",
        validation_checks=parsed.checks_as_dicts(),
        is_demo=is_demo,
    )
    session.add(target)
    session.flush()  # assigns target.id

    stored_path = _store_structure(target.id, raw, parsed)
    target.structure_path = stored_path

    artifact = models.Artifact(
        stage="target",
        kind="pdb",
        storage_path=stored_path,
    )
    session.add(artifact)
    session.flush()
    target.artifact_id = artifact.id
    session.flush()
    return target


def create_demo_target(session: Session) -> models.Target:
    """Deterministic demo target — same fixture, same checksum, every time."""
    return create_target(
        session,
        raw=load_demo_structure(),
        filename=DEMO_FIXTURE_FILENAME,
        source="demo",
        name=DEMO_TARGET_NAME,
        pdb_id=DEMO_PDB_ID,
        is_demo=True,
    )


def copy_structure_to_run(target: models.Target, job_id: str) -> str | None:
    """Stage the target's structure into the job's own run directory.

    Gives every run a self-contained artifact tree, so a run's inputs stay
    reproducible even if the target record is later changed or removed.
    """
    if not target.structure_path or not os.path.exists(target.structure_path):
        return None
    run_dir = os.path.join(DATA_DIR, "runs", job_id, "target")
    os.makedirs(run_dir, exist_ok=True)
    destination = os.path.join(run_dir, "target.pdb")
    shutil.copyfile(target.structure_path, destination)
    return destination


def read_structure_text(target: models.Target) -> str:
    if not target.structure_path or not os.path.exists(target.structure_path):
        raise TargetIntakeError(
            "Structure file for this target is no longer on disk.", status_code=404
        )
    with open(target.structure_path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _store_structure(target_id: str, raw: bytes, parsed: ParsedStructure) -> str:
    directory = os.path.join(TARGET_STORE, target_id)
    os.makedirs(directory, exist_ok=True)
    extension = "cif" if parsed.structure_format == "mmcif" else "pdb"
    path = os.path.join(directory, f"structure.{extension}")
    with open(path, "wb") as handle:
        handle.write(raw)
    return path


def _derive_name(parsed: ParsedStructure, filename: str | None, pdb_id: str | None) -> str:
    """Best available human label, in descending order of trustworthiness."""
    if parsed.title:
        title = parsed.title.strip()
        return title if len(title) <= 80 else title[:77] + "…"
    if pdb_id:
        return pdb_id.upper()
    if parsed.pdb_id_in_file:
        return parsed.pdb_id_in_file.upper()
    if filename:
        return os.path.splitext(os.path.basename(filename))[0]
    return "Uploaded structure"
