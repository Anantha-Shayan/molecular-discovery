"""
ORM models for the drug-discovery pipeline integrator MVP.

Schema mirrors the design discussed before implementation:

    Target 1──n Job 1──n Molecule 1──n StageResult
                  │                          │
                  └──n JobStageLog      Artifact (n, polymorphic FK)

StageResult.payload is a generic JSON blob on purpose: several of the
stages wrap mocked or not-yet-finalized third-party output schemas, so a
wide, rigidly-typed table per stage would just mean repeated migrations.
The JSON column is the explicit trade-off documented in the design notes
(see README) — "schema-flexible now, typed tables later once a real
partner schema is locked in."
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> dt.datetime:
    # Naive UTC: utcnow() is deprecated in 3.12+, and the DateTime columns
    # here are timezone-naive, so we drop the tzinfo after converting.
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Target(Base):
    """A protein target the user supplied, plus everything we could actually
    derive from their structure file.

    Every scalar here is *measured* from the uploaded/fetched file (or read
    straight out of its own header records) — nothing on this model is a
    scientific estimate produced by this application. Fields the file does
    not state (e.g. resolution for a non-diffraction structure) stay NULL
    rather than being invented.
    """

    __tablename__ = "targets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)  # "upload" | "pdb_id" | "demo"
    pdb_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # targets -> artifacts -> jobs -> targets is a foreign-key cycle. Naming
    # this constraint and marking it use_alter makes the DDL emit it as a
    # separate ALTER TABLE after both tables exist (required by PostgreSQL).
    artifact_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("artifacts.id", use_alter=True, name="fk_targets_artifact_id"),
        nullable=True,
    )

    # --- structure file -------------------------------------------------
    original_filename: Mapped[str | None] = mapped_column(String, nullable=True)
    structure_path: Mapped[str | None] = mapped_column(String, nullable=True)
    structure_format: Mapped[str | None] = mapped_column(String, nullable=True)  # pdb | mmcif
    checksum: Mapped[str | None] = mapped_column(String, nullable=True)  # sha256
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- parsed header metadata (NULL when the file doesn't state it) ---
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    experiment_method: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_a: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- derived structural counts --------------------------------------
    chains: Mapped[list | None] = mapped_column(JSON, nullable=True)
    selected_chain: Mapped[str | None] = mapped_column(String, nullable=True)
    residue_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    atom_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ligands: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # --- validation ------------------------------------------------------
    validation_status: Mapped[str | None] = mapped_column(String, nullable=True)  # PASSED | FAILED
    validation_checks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    jobs: Mapped[list["Job"]] = relationship(back_populates="target")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    target_id: Mapped[str] = mapped_column(String, ForeignKey("targets.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="SUBMITTED")
    current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now
    )

    target: Mapped["Target"] = relationship(back_populates="jobs")
    molecules: Mapped[list["Molecule"]] = relationship(back_populates="job")
    stage_logs: Mapped[list["JobStageLog"]] = relationship(back_populates="job")


class JobStageLog(Base):
    """Append-only transition log — the backbone of progress/retry tracking."""

    __tablename__ = "job_stage_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(String, ForeignKey("jobs.id"), index=True)
    stage: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)  # STARTED | SUCCEEDED | FAILED | RETRIED
    attempt: Mapped[int] = mapped_column(default=1)
    external_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped["Job"] = relationship(back_populates="stage_logs")


class Molecule(Base):
    __tablename__ = "molecules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(String, ForeignKey("jobs.id"), index=True)
    display_id: Mapped[str] = mapped_column(String)  # e.g. MDP-894-012
    smiles: Mapped[str] = mapped_column(Text)
    inchikey: Mapped[str | None] = mapped_column(String, nullable=True)
    source_stage: Mapped[str] = mapped_column(String, default="screening")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    job: Mapped["Job"] = relationship(back_populates="molecules")
    stage_results: Mapped[list["StageResult"]] = relationship(back_populates="molecule")


class StageResult(Base):
    __tablename__ = "stage_results"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    molecule_id: Mapped[str] = mapped_column(String, ForeignKey("molecules.id"), index=True)
    job_id: Mapped[str] = mapped_column(String, ForeignKey("jobs.id"), index=True)
    stage: Mapped[str] = mapped_column(String)  # screening | sa | admet | affinity | kinetics
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    is_mocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    molecule: Mapped["Molecule"] = relationship(back_populates="stage_results")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("jobs.id"), nullable=True, index=True
    )
    molecule_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("molecules.id"), nullable=True
    )
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String)  # pdb | sdf_input | sdf_output | csv_results
    # Relative to DATA_DIR (see backend/storage.py) — never an absolute host path.
    storage_path: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
