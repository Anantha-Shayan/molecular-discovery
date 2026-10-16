"""Campaign configuration: defaults, validation and the stage plan."""
from __future__ import annotations

from ..config import settings

ENGINES = ("vina", "hpsee")          # offered for new campaigns
LEGACY_ENGINES = ("flexx",)          # local FlexX: replaced by HPSee; kept so stored campaigns still open/resume
HPSEE_MODES = ("library", "chemical_space")
LIBRARY_SOURCES = ("zinc20", "demo", "upload", "chemical_space")   # chemical_space: HPSee C-S-D only
RANK_BY = ("hyde", "docking", "sa")
MAX_LIBRARY = 2000

# stage key -> (label, short description, runs in batches?, mocked?)
STAGES: dict[str, dict] = {
    "prepare": dict(label="Target preparation", batched=False, mocked=False,
                    blurb="Prepare the protein and binding site for docking."),
    "library": dict(label="Compound library", batched=False, mocked=False,
                    blurb="Obtain, clean and de-duplicate candidate molecules."),
    "docking": dict(label="Docking", batched=True, mocked=False,
                    blurb="Predict how each molecule sits in the binding site and score the fit."),
    "hyde": dict(label="HYDE scoring", batched=True, mocked=False,
                 blurb="Re-score docked poses with BioSolveIT HYDE (desolvation-aware)."),
    "sa": dict(label="Synthetic accessibility", batched=True, mocked=False,
               blurb="Estimate how easy each molecule is to make (RDKit SAscore)."),
    "admet": dict(label="ADMET prediction", batched=True, mocked=False,
                  blurb="Predict absorption, metabolism and toxicity (ADMET-AI)."),
    "affinity": dict(label="Koffee Binding Affinity", batched=True, mocked=False,
                     blurb="Predict binding affinity from completed docked protein–ligand poses (Kvantify Koffee)."),
    "kinetics": dict(label="Unbinding kinetics", batched=True, mocked=True,
                     blurb="Placeholder: Koffee unbinding-kinetics integration is pending."),
    "csd_anchoring": dict(label="Chemical space anchoring", batched=False, mocked=False,
                          blurb="HPSee docks and scores the chemical space's anchor synthons (building blocks) in the pocket."),
    "csd_extension": dict(label="Chemical space extension", batched=False, mocked=False,
                          blurb="After you review and confirm the anchors, HPSee grows them into full molecules of the space and docks those."),
    "rank": dict(label="Ranking & export", batched=False, mocked=False,
                 blurb="Apply the stated ranking policy and write the result files."),
    "grow": dict(label="Analogue generation", batched=False, mocked=False,
                 blurb="FastGrow proposes new analogues of the top candidates by growing fragments into the pocket."),
}

DEFAULTS = {
    "engine": "vina",
    "chain": None,
    "site": None,                  # filled by the API: {"mode","center","size","reference"}
    "library_source": "zinc20",
    "library_count": 30,
    "library_seed": 42,
    "library_upload": None,
    # Structural filters applied to every candidate molecule before docking (all sources, not just
    # ZINC) - modelled on ZINC's own tranche/"/substances" search dimensions where our data source
    # allows (see backend/science/library.py::Filters for what cannot be replicated: purchasability,
    # pH and net-charge state, which need live ZINC metadata we don't fetch).
    "filter_min_heavy": 10,
    "filter_max_heavy": 40,
    "filter_min_mw": 150.0,
    "filter_max_mw": 600.0,
    "filter_min_logp": -5.0,
    "filter_max_logp": 5.0,
    "filter_max_rotatable": 12,
    "filter_max_hbd": 5,
    "filter_max_hba": 10,
    "filter_max_tpsa": 140.0,
    "filter_reactivity": "mild",
    "exhaustiveness": 8,
    "num_modes": 5,
    "clash_tolerance": 0,          # FlexX only: 0 standard, 1 medium, 2 high
    "dock_keep_fraction": 0.5,     # share of successfully docked molecules kept
    "enable_hyde": True,
    "sa_threshold": 5.0,
    "enable_admet": True,
    "strict_admet_gate": True,
    "enable_affinity": False,
    "koffee_ligand_strain_in_score": settings.koffee_ligand_strain_in_score,
    "enable_kinetics": True,       # mocked, annotation only
    "rank_by": "hyde",
    "enable_grow": False,          # optional: FastGrow analogues of the top candidates
    "grow_parents": 3,
    "grow_per_parent": 20,
    "batch_size": None,            # None = server default
    # --- BioSolveIT HPSee (engine="hpsee") ---
    "hpsee_mode": "library",       # library: dock the ZINC/demo/upload library remotely | chemical_space: C-S-D
    "hpsee_space_id": None,        # an HPSee Chemical Space id (chemical_space mode)
    "hpsee_space_name": None,      # display only; resolved from HPSee at creation
    "hpsee_docking_definition": None,   # uploaded SeeSAR .flexx file (definition id)
    "hpsee_scoring_definition": None,   # uploaded SeeSAR .hydescorer file (definition id)
    "hpsee_anchor_filters": "",    # HPSee filter expression for anchoring (passed through verbatim)
    "hpsee_extension_filters": "", # HPSee filter expression for extension (passed through verbatim)
    "hpsee_max_products": 500,     # best complete products (by HYDE, then docking score) entering the funnel
}

HPSEE_MAX_ROUNDS = 3               # extension rounds before products must be taken (3-component spaces need 2)


class ParamError(ValueError):
    pass


def resolve(params: dict | None) -> dict:
    p = {**DEFAULTS, **(params or {})}
    if p["engine"] not in ENGINES + LEGACY_ENGINES:
        raise ParamError(f"engine must be one of {ENGINES}")
    if p["hpsee_mode"] not in HPSEE_MODES:
        raise ParamError(f"hpsee_mode must be one of {HPSEE_MODES}")
    for key in ("hpsee_anchor_filters", "hpsee_extension_filters"):
        p[key] = str(p[key] or "").strip()
        if len(p[key]) > 2000:
            raise ParamError(f"{key} is too long (2000 characters at most)")
    p["hpsee_max_products"] = max(1, min(MAX_LIBRARY, int(p["hpsee_max_products"])))
    if p["library_source"] not in LIBRARY_SOURCES:
        raise ParamError(f"library_source must be one of {LIBRARY_SOURCES}")
    if (p["library_source"] == "chemical_space") != is_csd(p):
        raise ParamError("library_source 'chemical_space' is only used by HPSee Chemical Space Docking")
    if p["rank_by"] not in RANK_BY:
        raise ParamError(f"rank_by must be one of {RANK_BY}")
    p["library_count"] = int(p["library_count"])
    if not 1 <= p["library_count"] <= MAX_LIBRARY:
        raise ParamError(f"library_count must be between 1 and {MAX_LIBRARY}")
    p["library_seed"] = int(p["library_seed"])
    p["filter_min_heavy"] = max(1, int(p["filter_min_heavy"]))
    p["filter_max_heavy"] = max(p["filter_min_heavy"], int(p["filter_max_heavy"]))
    p["filter_min_mw"] = max(0.0, float(p["filter_min_mw"]))
    p["filter_max_mw"] = max(p["filter_min_mw"], float(p["filter_max_mw"]))
    p["filter_min_logp"] = float(p["filter_min_logp"])
    p["filter_max_logp"] = max(p["filter_min_logp"], float(p["filter_max_logp"]))
    p["filter_max_rotatable"] = max(0, int(p["filter_max_rotatable"]))
    p["filter_max_hbd"] = max(0, int(p["filter_max_hbd"]))
    p["filter_max_hba"] = max(0, int(p["filter_max_hba"]))
    p["filter_max_tpsa"] = max(0.0, float(p["filter_max_tpsa"]))
    if p["filter_reactivity"] not in ("mild", "clean", "anodyne"):
        raise ParamError("filter_reactivity must be one of mild, clean, anodyne")
    p["exhaustiveness"] = int(p["exhaustiveness"])
    if not 1 <= p["exhaustiveness"] <= 64:
        raise ParamError("exhaustiveness must be between 1 and 64")
    p["num_modes"] = max(1, min(20, int(p["num_modes"])))
    p["clash_tolerance"] = int(p["clash_tolerance"])
    if p["clash_tolerance"] not in (0, 1, 2):
        raise ParamError("clash_tolerance must be 0, 1 or 2")
    p["dock_keep_fraction"] = float(p["dock_keep_fraction"])
    if not 0.05 <= p["dock_keep_fraction"] <= 1.0:
        raise ParamError("dock_keep_fraction must be between 0.05 and 1.0")
    p["sa_threshold"] = float(p["sa_threshold"])
    if not 1.0 <= p["sa_threshold"] <= 10.0:
        raise ParamError("sa_threshold must be between 1 and 10")
    for flag in ("enable_hyde", "enable_admet", "strict_admet_gate", "enable_affinity", "enable_kinetics", "koffee_ligand_strain_in_score"):
        p[flag] = bool(p[flag])
    p["enable_grow"] = bool(p["enable_grow"])
    p["grow_parents"] = max(1, min(10, int(p["grow_parents"])))
    p["grow_per_parent"] = max(1, min(200, int(p["grow_per_parent"])))
    p["batch_size"] = max(1, int(p["batch_size"] or settings.batch_size))
    if is_csd(p):
        p["enable_hyde"] = False   # HPSee already scores with HYDE (the .hydescorer definition); no local re-score
    if p["rank_by"] == "hyde" and not (p["enable_hyde"] or is_csd(p)):
        p["rank_by"] = "docking"
    return p


def is_csd(p: dict) -> bool:
    return p.get("engine") == "hpsee" and p.get("hpsee_mode") == "chemical_space"


def engine_label(p: dict) -> str:
    if p["engine"] == "vina":
        return "AutoDock Vina"
    if p["engine"] == "flexx":
        return "BioSolveIT FlexX (local, legacy)"
    return "BioSolveIT HPSee — Chemical Space Docking" if is_csd(p) else "BioSolveIT HPSee — remote FlexX docking"


def enabled_stages(p: dict) -> list[tuple[str, bool]]:
    """[(stage_key, enabled)] in execution order."""
    if is_csd(p):
        # Candidates come from the chemical space, already docked and HYDE-scored by HPSee:
        # there is no library to fetch and nothing to dock or re-score locally.
        return [
            ("prepare", True),
            ("csd_anchoring", True),
            ("csd_extension", True),
            ("sa", True),
            ("admet", p["enable_admet"]),
            ("affinity", p["enable_affinity"]),
            ("kinetics", p["enable_kinetics"]),
            ("rank", True),
            ("grow", p["enable_grow"]),
        ]
    return [
        ("prepare", True),
        ("library", True),
        ("docking", True),
        ("hyde", p["enable_hyde"]),
        ("sa", True),
        ("admet", p["enable_admet"]),
        ("affinity", p["enable_affinity"]),
        ("kinetics", p["enable_kinetics"]),
        ("rank", True),
        ("grow", p["enable_grow"]),
    ]


def rank_policy_text(p: dict) -> str:
    primary = {
        "hyde": ("HYDE estimated affinity, lower bound (nM, smallest first; HPSee reports no per-atom HYDE "
                 "scores, so the cumulative score is unavailable)") if p["engine"] == "hpsee"
                else "HYDE cumulative score (kJ/mol, most negative first)",
        "docking": {"vina": "Vina", "flexx": "FlexX", "hpsee": "FlexX (run on HPSee)"}[p["engine"]]
                   + " docking score (most negative first)",
        "sa": "synthetic accessibility (lowest first)",
    }[p["rank_by"]]
    return (
        f"Candidates must pass every enabled filter. Ranked by {primary}; ties broken by lower "
        "SA score. No scores are combined; Koffee is displayed separately and never affects filtering or ranking."
    )
