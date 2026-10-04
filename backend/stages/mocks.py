"""
Mocked pipeline stages.

None of these call a real screening engine, ADMET model, docking engine,
or MD/kinetics simulator — Molecular Solutions' actual partner software
for these stages is not available during this take-home. Every value
produced here is a DETERMINISTIC pseudo-random function of the
molecule's SMILES (so repeated runs are stable and demo-able), not a
real prediction, and every StageResult row written from this module
sets is_mocked=True so the API/UI can say so honestly.

Swapping a real partner integration in later means replacing the body
of one function per stage — the pipeline orchestrator and data model
don't change.
"""
from __future__ import annotations

import hashlib
import math


# ---------------------------------------------------------------------------
# Stage 02 — Chemical-space screening
# ---------------------------------------------------------------------------
# Real pipelines search combinatorial make-on-demand libraries sized in the
# billions-to-trillions. We obviously can't do that here, so this is a small
# fixed seed set of drug-like SMILES standing in for "what survived
# screening." The dashboard's "10T+" label refers to the conceptual library
# scale a production run would start from, NOT this demo's actual input size
# — that distinction is deliberately kept visible in the API response
# (see schemas.ScreeningSummary).
SEED_LIBRARY = [
    "C=CC(=O)N1CCN(CC1)C2=NC=NC3=C2C=C(C(=C3F)C4=C(C=CC=C4F)O)C#N",  # dashboard's lead
    "CC(=O)OC1=CC=CC=C1C(=O)O",  # aspirin
    "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O",  # ibuprofen
    "CN1C=NC2=C1C(=O)N(C(=O)N2C)C",  # caffeine
    "COC1=CC2=C(C=C1)N(C=N2)CCN",  # generic indole amine
    "CC1=CC(=NO1)C2=CC=CC=C2",  # isoxazole-phenyl scaffold
    "O=C(NC1CCCCC1)C1=CC=CC=C1",  # cyclohexyl benzamide
    "CC(C)(C)NCC(O)C1=CC(O)=CC(O)=C1",  # beta-blocker-like scaffold
    "C1CN(CCN1)C2=NC=CC=N2",  # piperazine-pyrimidine
    "CC1=NN(C(=O)C1)C2=CC=C(C=C2)Cl",  # pyrazolone
]


def screen_chemical_space(limit: int = 10) -> list[str]:
    """Stand-in for ultra-large virtual screening output."""
    return SEED_LIBRARY[:limit]


def _deterministic_unit_interval(key: str, salt: str) -> float:
    """Stable pseudo-random float in [0, 1) derived from a string key."""
    h = hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Stage 04 — ADMET
# ---------------------------------------------------------------------------
def predict_admet(smiles: str) -> dict:
    """Mocked ADMET verdict. A real integration would call a partner model
    and return 220+ named descriptors; we return a small illustrative
    subset plus a derived "profile" label so the UI has something to show.
    """
    u = _deterministic_unit_interval(smiles, "admet")
    favorable = u > 0.3
    return {
        "profile": "Favorable" if favorable else "Moderate",
        "property_count": 220,
        "sample_properties": {
            "logP_est": round(1.5 + u * 3, 2),
            "hbd_est": int(u * 4),
            "hba_est": int(2 + u * 6),
            "caco2_permeability_class": "high" if favorable else "moderate",
        },
        "note": "Mocked — illustrative subset only, not a real ADMET model output.",
    }


# ---------------------------------------------------------------------------
# Stage 05 — Binding affinity
# ---------------------------------------------------------------------------
def predict_binding_affinity(smiles: str) -> dict:
    """Mocked Kd/ΔG. Real stage would run a scoring function or re-docking
    against the prepared protein pocket (needs the PDB, not just the ligand).
    """
    u = _deterministic_unit_interval(smiles, "affinity")
    kd_nm = round(2 + u * 40, 1)  # plausible nanomolar range for a "hit"
    delta_g = round(-9.5 + u * 2.5, 2)  # kcal/mol, illustrative only
    return {"kd_nm": kd_nm, "delta_g_kcal_mol": delta_g}


# ---------------------------------------------------------------------------
# Stage 06 — Unbinding kinetics (koff / residence time)
# ---------------------------------------------------------------------------
def predict_unbinding_kinetics(smiles: str, kd_nm: float) -> dict:
    """Mocked koff / residence time. Real stage needs GPU-accelerated
    enhanced-sampling MD (e.g. metadynamics / tau-RAMD style methods)
    against the bound pose, not just the ligand structure.
    """
    u = _deterministic_unit_interval(smiles, "kinetics")
    # Rough inverse relationship with affinity, with noise, purely for a
    # plausible-looking demo — not derived from any real physics here.
    koff = round((1.0e-4) * (0.5 + u) * (kd_nm / 10.0), 6)
    residence_min = round(1.0 / (koff * 60.0), 1) if koff > 0 else math.inf
    return {"koff_per_s": koff, "residence_time_min": residence_min}
