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
# Campaigns (stored as "jobs")
# ---------------------------------------------------------------------------
class SiteRequest(BaseModel):
    """Binding site: from a co-crystallised ligand, or a manual box (Vina only)."""

    mode: str = "ligand"                 # ligand | manual
    reference: str | None = None         # "HET:chain:resseq" from /api/targets/{id}/sites
    padding: float = 6.0
    center: list[float] | None = None
    size: list[float] | None = None


class CreateJobRequest(BaseModel):
    target_id: str
    label: str | None = None
    chain: str | None = None
    engine: str = "vina"
    site: SiteRequest
    library_source: str = "zinc20"
    library_count: int = 30
    library_seed: int = 42
    library_upload: str | None = Field(default=None, max_length=2_000_000)
    filter_min_heavy: int = 10
    filter_max_heavy: int = 40
    filter_min_mw: float = 150.0
    filter_max_mw: float = 600.0
    filter_min_logp: float = -5.0
    filter_max_logp: float = 5.0
    filter_max_rotatable: int = 12
    filter_max_hbd: int = 5
    filter_max_hba: int = 10
    filter_max_tpsa: float = 140.0
    filter_reactivity: str = "mild"
    exhaustiveness: int = 8
    num_modes: int = 5
    clash_tolerance: int = 0
    dock_keep_fraction: float = 0.5
    enable_hyde: bool = True
    sa_threshold: float = 5.0
    enable_admet: bool = True
    strict_admet_gate: bool = True
    enable_affinity: bool = False
    koffee_ligand_strain_in_score: bool = False
    enable_kinetics: bool = True
    rank_by: str = "hyde"
    enable_grow: bool = False
    grow_parents: int = 3
    grow_per_parent: int = 20
    batch_size: int | None = None
    # BioSolveIT HPSee (engine="hpsee")
    hpsee_mode: str = "library"               # library | chemical_space
    hpsee_space_id: str | None = None
    hpsee_docking_definition: str | None = None   # id returned by POST /api/hpsee/definitions (kind=flexx)
    hpsee_scoring_definition: str | None = None   # id returned by POST /api/hpsee/definitions (kind=hydescorer)
    hpsee_anchor_filters: str = Field(default="", max_length=2000)
    hpsee_extension_filters: str = Field(default="", max_length=2000)
    hpsee_max_products: int = 500


class RetryRequest(BaseModel):
    batch_ids: list[str] | None = None
    failed_molecules: bool = False
