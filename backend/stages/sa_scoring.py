"""
Stage 03 — Synthetic Accessibility (SA) scoring.

This is the one stage implemented with REAL science in the MVP: RDKit
ships the original Ertl & Schuffenhauer (2009) SAscore implementation as
a contrib script. We call it directly rather than reimplementing it.

Score range: 1 (easy to synthesize) to 10 (very difficult) — NOT 1-100.
Reference: Ertl P, Schuffenhauer A. J Cheminform. 2009;1:8.

is_mocked is always False for this stage's StageResult rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import Descriptors, RDConfig

_SA_SCORE_DIR = Path(RDConfig.RDContribDir) / "SA_Score"
if str(_SA_SCORE_DIR) not in sys.path:
    sys.path.append(str(_SA_SCORE_DIR))

import sascorer  # noqa: E402  (import after sys.path manipulation, by design)


def score_molecule(smiles: str) -> dict:
    """Return real SA score + a couple of real, cheap descriptors for free.

    Raises ValueError if the SMILES does not parse — callers should treat
    that as an input-validation failure, not a pipeline/service failure.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Could not parse SMILES: {smiles!r}")

    sa_score = round(float(sascorer.calculateScore(mol)), 2)
    return {
        "sa_score": sa_score,
        "scale": "1 (easy) - 10 (hard)",
        "molecular_weight": round(Descriptors.MolWt(mol), 1),
        "num_rings": Descriptors.RingCount(mol),
        "method": "Ertl & Schuffenhauer SAscore (RDKit Contrib/SA_Score)",
    }
