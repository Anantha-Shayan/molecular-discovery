from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
class ValidationCheckOut(BaseModel):
    key: str
    label: str
    passed: bool
    detail: str


class ChainOut(BaseModel):
    chain_id: str
    residue_count: int
    atom_count: int
    first_residue: int | None = None
    last_residue: int | None = None


class TargetResponse(BaseModel):
    """A validated target. Every field here is read from the user's own file.

    Fields the file does not state (resolution for a non-diffraction
    structure, for instance) are null rather than estimated.
    """

    target_id: str
    name: str
    source: str  # upload | pdb_id | demo
    pdb_id: str | None = None
    original_filename: str | None = None
    structure_format: str | None = None
    checksum: str | None = None
    file_size_bytes: int | None = None
    title: str | None = None
    experiment_method: str | None = None
    resolution_a: float | None = None
    chains: list[ChainOut] = Field(default_factory=list)
    selected_chain: str | None = None
    residue_count: int | None = None
    atom_count: int | None = None
    ligands: list[str] = Field(default_factory=list)
    validation_status: str | None = None
    validation_checks: list[ValidationCheckOut] = Field(default_factory=list)
    is_demo: bool = False
    created_at: str | None = None


class PdbIdRequest(BaseModel):
    pdb_id: str
    name: str | None = None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
class CreateJobRequest(BaseModel):
    """Create a discovery run.

    Preferred form supplies `target_id` from one of the /api/targets
    endpoints. The legacy `target_name` / `pdb_id` form is still accepted so
    the original API contract keeps working; it creates a metadata-only
    target with no structure file.
    """

    target_id: str | None = None

    # Legacy intake (pre-upload API). Retained for backwards compatibility.
    target_name: str | None = None
    pdb_id: str | None = None

    run_tag: str | None = None
    label: str | None = None
    selected_chain: str | None = None

    # Configuration — only parameters that genuinely affect this pipeline.
    library_limit: int = 25
    sa_threshold: float = 5.0
    enable_admet: bool = True
    enable_affinity: bool = True
    enable_kinetics: bool = True
    strict_admet_gate: bool = True
    affinity_shortlist: int = 8
    stage_delay_seconds: float | None = None


class StageCard(BaseModel):
    stage_no: int
    key: str
    label: str
    subtitle: str
    count: int | None
    count_label: str
    badge: str
    status: str  # "done" | "active" | "pending" | "failed" | "skipped"
    is_mocked: bool
    scale_note: str | None = None  # e.g. "10T+ conceptual library scale"
    count_in: int | None = None
    count_out: int | None = None
    detail: str | None = None


class EngineInfo(BaseModel):
    key: str
    name: str
    is_mocked: bool
    backend: str
    note: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    current_stage: str | None
    target_name: str
    pdb_id: str | None
    stages: list[StageCard]
    retained: int
    # Added for the real flow; existing fields above are unchanged.
    target: TargetResponse | None = None
    config: dict = Field(default_factory=dict)
    engines: list[EngineInfo] = Field(default_factory=list)
    label: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    failure_reason: str | None = None


class JobSummary(BaseModel):
    job_id: str
    label: str | None
    status: str
    current_stage: str | None
    target_name: str
    pdb_id: str | None
    target_source: str | None
    candidate_count: int
    created_at: str | None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
class CandidateRow(BaseModel):
    molecule_id: str
    display_id: str
    rank: int
    smiles: str
    structure_svg: str
    molecular_weight: float
    sa_score: float
    admet_profile: str
    kd_nm: float
    koff_per_s: float
    residence_time_min: float
    status: str


class CandidateListResponse(BaseModel):
    job_id: str
    candidates: list[CandidateRow]


class ProvenanceStep(BaseModel):
    label: str
    is_mocked: bool


class CandidateDetailResponse(BaseModel):
    molecule_id: str
    display_id: str
    smiles: str
    structure_svg: str
    molecular_weight: float
    sa_score: float
    admet_profile: str
    admet_property_count: int
    kd_nm: float
    delta_g_kcal_mol: float
    koff_per_s: float
    residence_time_min: float
    status: str
    provenance: list[ProvenanceStep]
