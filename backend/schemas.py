from __future__ import annotations

from pydantic import BaseModel


class CreateJobRequest(BaseModel):
    target_name: str = "KRAS G12D"
    pdb_id: str | None = "8AZX"
    run_tag: str = "0894"
    library_limit: int = 10
    sa_threshold: float = 5.0


class StageCard(BaseModel):
    stage_no: int
    key: str
    label: str
    subtitle: str
    count: int | None
    count_label: str
    badge: str
    status: str  # "done" | "active" | "pending" | "failed"
    is_mocked: bool
    scale_note: str | None = None  # e.g. "10T+ conceptual library scale"


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    current_stage: str | None
    target_name: str
    pdb_id: str | None
    stages: list[StageCard]
    retained: int


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
