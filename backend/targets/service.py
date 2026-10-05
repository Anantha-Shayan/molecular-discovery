"""
Target intake: turn a user-supplied structure into a validated, persisted
Target record plus a stored PDB artifact.

Three intake routes, one code path after parsing:

    upload   ──┐
    PDB ID   ──┼──> parse_structure() ──> validate ──> persist file + Target
    demo     ──┘

The demo route reads a fixture shipped inside the application, so the whole
flow works with no network access. The PDB-ID route fetches from RCSB and
reports a clear error when that isn't reachable, rather than silently
substituting something else.

Security stance: uploaded files are *data*. They are parsed as text, never
executed, and never influence a filesystem path — storage locations are
built only from server-generated IDs. The user's filename is kept purely as
display metadata, after being reduced to a plain, printable basename.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from sqlalchemy.orm import Session

from .. import models, storage
from ..config import settings
from ..logging_config import get_logger
from .structure import ParsedStructure, parse_structure

log = get_logger("targets")

RCSB_URL = "https://files.rcsb.org/download/{pdb_id}.pdb"
PDB_ID_PATTERN = re.compile(r"^[0-9][A-Za-z0-9]{3}$")

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


# ---------------------------------------------------------------------------
# Input hygiene
# ---------------------------------------------------------------------------
def clean_text(value: str | None, max_length: int) -> str | None:
    """Strip control/format characters, collapse whitespace, cap the length."""
    if not value:
        return None
    printable = "".join(
        ch for ch in value
        if unicodedata.category(ch)[0] != "C" or ch in "\t "
    )
    collapsed = " ".join(printable.split())
    return collapsed[:max_length] or None


def sanitize_filename(filename: str | None) -> str | None:
    """Reduce an uploaded filename to a plain printable basename.

    The result is display metadata only — it is never used to build a path —
    but it is still normalised so that a crafted name (path separators,
    control characters, markup) cannot cause trouble wherever it is shown.
    """
    if not filename:
        return None
    base = filename.replace("\\", "/").split("/")[-1]
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    base = base.strip(" .")
    return base[:255] or None


# ---------------------------------------------------------------------------
# Demo fixture
# ---------------------------------------------------------------------------
def demo_fixture_path() -> Path:
    return settings.fixture_dir / DEMO_FIXTURE_FILENAME


def load_demo_structure() -> bytes:
    path = demo_fixture_path()
    if not path.exists():
        raise TargetIntakeError(
            "The bundled demo fixture is missing from this installation "
            "(see data/fixtures/README.md).",
            status_code=500,
        )
    return path.read_bytes()


# ---------------------------------------------------------------------------
# RCSB fetch
# ---------------------------------------------------------------------------
def fetch_pdb_by_id(pdb_id: str) -> bytes:
    """Fetch a structure from RCSB.

    Raises TargetIntakeError with an actionable message when the ID is
    unknown or the network is unavailable — the demo must never appear to
    succeed with a structure it didn't actually retrieve.
    """
    clean = pdb_id.strip().upper()
    if not PDB_ID_PATTERN.match(clean):
        raise TargetIntakeError(
            f"{clean[:20]!r} is not a valid 4-character PDB ID (e.g. 7RPZ)."
        )

    # The bundled fixture answers for its own ID, so the demo's happy path
    # never depends on the network.
    if clean == DEMO_PDB_ID:
        return load_demo_structure()

    url = RCSB_URL.format(pdb_id=clean)  # fixed host; ID validated above
    try:
        with urllib.request.urlopen(url, timeout=settings.rcsb_timeout_seconds) as response:
            return response.read(settings.max_upload_bytes + 1)
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
        log.warning("RCSB fetch failed for %s: %s", clean, exc.__class__.__name__)
        raise TargetIntakeError(
            f"Could not reach RCSB to fetch {clean} ({exc.__class__.__name__}). "
            "Upload the structure file directly, or use the bundled demo "
            "target, which needs no network access."
        ) from exc


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
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
    filename = sanitize_filename(filename)
    parsed = parse_structure(raw, filename, max_bytes=settings.max_upload_bytes)
    if not parsed.ok:
        raise TargetIntakeError(parsed.error_detail, parsed.checks_as_dicts())

    title = clean_text(parsed.title, 500)
    target = models.Target(
        name=clean_text(name, 120) or _derive_name(title, parsed, filename, pdb_id),
        source=source,
        pdb_id=(pdb_id or parsed.pdb_id_in_file or None),
        original_filename=filename,
        structure_format=parsed.structure_format,
        checksum=parsed.checksum,
        file_size_bytes=parsed.file_size_bytes,
        title=title,
        experiment_method=clean_text(parsed.experiment_method, 120),
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

    target.structure_path = _store_structure(target.id, raw, parsed)

    artifact = models.Artifact(
        stage="target",
        kind="pdb",
        storage_path=target.structure_path,
    )
    session.add(artifact)
    session.flush()
    target.artifact_id = artifact.id
    session.flush()
    log.info(
        "target created", extra={"target_id": target.id, "status": f"source={source}"}
    )
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
    Returns the stored (DATA_DIR-relative) path, or None if there is no file.
    """
    if not target.structure_path:
        return None
    try:
        source = storage.resolve(target.structure_path)
    except storage.StoragePathError:
        log.error("target structure path rejected", extra={"target_id": target.id})
        return None
    if not source.exists():
        return None
    run_dir = storage.ensure_dir("runs", job_id, "target")
    destination = run_dir / "target.pdb"
    shutil.copyfile(source, destination)
    return storage.to_stored(destination)


def read_structure_text(target: models.Target) -> str:
    try:
        path = storage.resolve(target.structure_path or "")
    except storage.StoragePathError as exc:
        raise TargetIntakeError("Structure file is unavailable.", status_code=404) from exc
    if not target.structure_path or not path.is_file():
        raise TargetIntakeError(
            "Structure file for this target is no longer on disk.", status_code=404
        )
    return path.read_text(encoding="utf-8", errors="replace")


def _store_structure(target_id: str, raw: bytes, parsed: ParsedStructure) -> str:
    """Write the original bytes under DATA_DIR/targets/<id>/ and return the
    stored (relative) path. The filename is fixed; user input never reaches it."""
    directory = storage.ensure_dir("targets", target_id)
    extension = "cif" if parsed.structure_format == "mmcif" else "pdb"
    path = directory / f"structure.{extension}"
    path.write_bytes(raw)
    return storage.to_stored(path)


def _derive_name(
    title: str | None, parsed: ParsedStructure, filename: str | None, pdb_id: str | None
) -> str:
    """Best available human label, in descending order of trustworthiness."""
    if title:
        return title if len(title) <= 80 else title[:77] + "…"
    if pdb_id:
        return pdb_id.upper()
    header_id = clean_text(parsed.pdb_id_in_file, 20)
    if header_id:
        return header_id.upper()
    if filename:
        return clean_text(Path(filename).stem, 80) or "Uploaded structure"
    return "Uploaded structure"
