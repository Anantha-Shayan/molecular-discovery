"""
ADMET prediction with ADMET-AI (Swanson et al., Bioinformatics 2024).

Why this model: it is open (MIT), pip-installable, runs on CPU in seconds for
hundreds of molecules, and predicts 41 endpoints trained on public Therapeutics
Data Commons (TDC) datasets (Chemprop-RDKit graph neural network ensembles).
It needs no external service, so it works identically on the company server.

What the numbers are (and are not):
  * classification endpoints return a PROBABILITY (0-1) that the molecule is
    positive for that endpoint in its training data (e.g. hERG blocker);
  * regression endpoints return a predicted value in the endpoint's own unit;
  * all are statistical predictions from public data, with no applicability
    -domain estimate, and must not be read as experimental results.

The campaign's ADMET gate counts "toxicity alerts" among a short, explicit
list of endpoints (TOX_FLAGS below). An alert needs BOTH probability >= 0.5 and
a DrugBank-approved-drug percentile >= 90 (the molecule scores higher than 90%
of approved drugs for that endpoint).

Why the percentile condition: measured on this install, a bare 0.5 cut-off
flags 93% of a random ZINC sample and 52% of known drugs for hERG, and 58% of
known drugs for DILI, because those classifiers are trained on positive-heavy
data. A rule that rejects most approved drugs is not a useful filter, so the
alert is made relative to approved drugs. It is a triage rule chosen for
transparency, not a validated safety classification.
"""
from __future__ import annotations

import threading

from ..logging_config import get_logger

log = get_logger("admet")

MODEL_NAME = "ADMET-AI v2 (Chemprop-RDKit ensembles, TDC datasets)"
FLAG_THRESHOLD = 0.5
FLAG_PERCENTILE = 90.0

# endpoint -> (group, label, kind, unit/explanation)
ENDPOINTS: dict[str, tuple[str, str, str, str]] = {
    "HIA_Hou": ("Absorption", "Human intestinal absorption", "prob", "probability well absorbed"),
    "Bioavailability_Ma": ("Absorption", "Oral bioavailability (>=20%)", "prob", "probability"),
    "Caco2_Wang": ("Absorption", "Caco-2 permeability", "reg", "log cm/s"),
    "PAMPA_NCATS": ("Absorption", "PAMPA permeability", "prob", "probability permeable"),
    "Pgp_Broccatelli": ("Absorption", "P-gp inhibitor", "prob", "probability"),
    "Solubility_AqSolDB": ("Absorption", "Aqueous solubility", "reg", "log mol/L"),
    "Lipophilicity_AstraZeneca": ("Absorption", "Lipophilicity (logD)", "reg", "logD"),
    "BBB_Martins": ("Distribution", "Blood-brain barrier penetration", "prob", "probability"),
    "PPBR_AZ": ("Distribution", "Plasma protein binding", "reg", "% bound"),
    "VDss_Lombardo": ("Distribution", "Volume of distribution", "reg", "L/kg"),
    "CYP3A4_Veith": ("Metabolism", "CYP3A4 inhibitor", "prob", "probability"),
    "CYP2D6_Veith": ("Metabolism", "CYP2D6 inhibitor", "prob", "probability"),
    "CYP2C9_Veith": ("Metabolism", "CYP2C9 inhibitor", "prob", "probability"),
    "CYP2C19_Veith": ("Metabolism", "CYP2C19 inhibitor", "prob", "probability"),
    "CYP1A2_Veith": ("Metabolism", "CYP1A2 inhibitor", "prob", "probability"),
    "Half_Life_Obach": ("Excretion", "Half-life", "reg", "hours"),
    "Clearance_Hepatocyte_AZ": ("Excretion", "Hepatocyte clearance", "reg", "uL/min/10^6 cells"),
    "Clearance_Microsome_AZ": ("Excretion", "Microsomal clearance", "reg", "mL/min/g"),
    "AMES": ("Toxicity", "Mutagenicity (Ames)", "prob", "probability"),
    "hERG": ("Toxicity", "hERG channel blocker (cardiac risk)", "prob", "probability"),
    "DILI": ("Toxicity", "Drug-induced liver injury", "prob", "probability"),
    "ClinTox": ("Toxicity", "Clinical-trial toxicity", "prob", "probability"),
    "Carcinogens_Lagunin": ("Toxicity", "Carcinogenicity", "prob", "probability"),
    "Skin_Reaction": ("Toxicity", "Skin reaction", "prob", "probability"),
    "LD50_Zhu": ("Toxicity", "Acute toxicity LD50", "reg", "log(1/(mol/kg))"),
    "QED": ("Drug-likeness", "QED drug-likeness", "reg", "0-1, higher is more drug-like"),
    "Lipinski": ("Drug-likeness", "Lipinski rule-of-5 (0-4 passed)", "reg", "rules satisfied"),
}
TOX_FLAGS = ["AMES", "hERG", "DILI", "ClinTox", "Carcinogens_Lagunin"]

_lock = threading.Lock()
_model = None


def _get_model():
    global _model
    if _model is None:
        from admet_ai import ADMETModel  # heavy import (torch); keep lazy
        _model = ADMETModel()
    return _model


def evaluate_flags(values: dict[str, float], percentiles: dict[str, float]) -> list[str]:
    return [
        k for k in TOX_FLAGS
        if values.get(k) is not None and values[k] >= FLAG_THRESHOLD
        and percentiles.get(k) is not None and percentiles[k] >= FLAG_PERCENTILE
    ]


def profile_for(n_flags: int) -> str:
    return "Favorable" if n_flags == 0 else ("Moderate" if n_flags == 1 else "Concern")


def predict(smiles_list: list[str], threads: int = 2) -> list[dict]:
    """Predict ADMET endpoints; returns one dict per input (same order).

    The model's own DataFrame is indexed BY SMILES, so a duplicate SMILES in the input makes
    `df.loc[that_smiles]` return more than one row instead of one — looking results up by
    position instead avoids that. The model can also silently drop a molecule it considers
    invalid (ADMETModel._filter_valid_molecules), which would then misalign a purely positional
    read; every SMILES reaching this function has already parsed with RDKit upstream (library
    standardisation, SA scoring), so that should not happen, but it is checked explicitly rather
    than risking a silent mismatch between input and output.
    """
    import torch

    with _lock:
        torch.set_num_threads(max(1, threads))
        df = _get_model().predict(smiles=smiles_list)
    if len(df) != len(smiles_list):
        raise RuntimeError(
            f"ADMET model returned {len(df)} rows for {len(smiles_list)} input molecules "
            "(some were likely rejected as invalid); cannot match results back to inputs."
        )
    out = []
    for i in range(len(smiles_list)):
        row = df.iloc[i]
        values = {k: round(float(row[k]), 4) for k in ENDPOINTS if k in row}
        pct = {k: round(float(row[f"{k}_drugbank_approved_percentile"]), 1)
               for k in ENDPOINTS if f"{k}_drugbank_approved_percentile" in row}
        flags = evaluate_flags(values, pct)
        out.append({
            "values": values,
            "percentiles": pct,
            "tox_flags": flags,
            "n_flags": len(flags),
            "profile": profile_for(len(flags)),
            "model": MODEL_NAME,
            "flag_rule": (
                f"probability >= {FLAG_THRESHOLD} and approved-drug percentile >= {FLAG_PERCENTILE:g} "
                f"on: {', '.join(TOX_FLAGS)}"
            ),
        })
    return out
