"""
Stage 03 — Synthetic Accessibility (SA) scoring.

Two engines:
  - RDKit (default, offline, free): the bundled Ertl & Schuffenhauer (2009)
    SAscore contrib script. Score range 1 (easy) to 10 (hard).
  - ChemAIRS SAScore API (commercial, backend/science/chemairs.py): used
    automatically for the main "sa" pipeline stage once CHEMAIRS_BASE_URL
    and CHEMAIRS_API_KEY are set. See chemairs.py's module docstring for
    the unresolved scale/direction caveat before trusting its numbers.

RDKit stays the only path for `score_molecule` (single molecule) — used by
FastGrow's generated-analogue scoring, which stays fast/offline/free by
design rather than spending a commercial API call per grown fragment.
`score_batch_for_stage` is what the main SA pipeline stage calls; it is the
one that switches to ChemAIRS when configured.

is_mocked is always False for this stage's StageResult rows, on either engine.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import Descriptors, RDConfig

from ..config import settings

_SA_SCORE_DIR = Path(RDConfig.RDContribDir) / "SA_Score"
if str(_SA_SCORE_DIR) not in sys.path:
    sys.path.append(str(_SA_SCORE_DIR))

import sascorer  # noqa: E402  (import after sys.path manipulation, by design)

RDKIT_METHOD = "Ertl & Schuffenhauer SAscore (RDKit Contrib/SA_Score)"
CHEMAIRS_METHOD = "ChemAIRS SAScore API"


def score_molecule(smiles: str) -> dict:
    """RDKit SAscore for one molecule. Always offline, always available.

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
        "method": RDKIT_METHOD,
    }


def active_method() -> str:
    """What `score_batch_for_stage` will actually use right now."""
    return CHEMAIRS_METHOD if settings.chemairs_configured else RDKIT_METHOD


def score_batch_for_stage(smiles_list: list[str]) -> list[dict]:
    """SA-score a whole batch for the main pipeline stage.

    Returns one result dict per input, SAME ORDER AS smiles_list (never keyed by the
    SMILES string itself — a batch can contain the same SMILES twice, and ChemAIRS's
    own result rows are matched back by position/RowNumber for exactly that reason;
    see backend/science/admet.py for the same class of bug this avoids). A
    molecule-level failure (bad SMILES locally, or isError=true from ChemAIRS) comes
    back as a dict with an 'error' key rather than raising, so the caller can turn
    individual molecules into failed outcomes without losing the rest of the batch.
    A batch-level problem (ChemAIRS unreachable, bad key, no valid rows at all)
    raises, matching every other engine in this codebase.
    """
    if not settings.chemairs_configured:
        out = []
        for smi in smiles_list:
            try:
                out.append(score_molecule(smi))
            except ValueError as exc:
                out.append({"error": str(exc)})
        return out

    from ..science import chemairs
    rows = chemairs.score_batch(smiles_list)  # keyed by 1-based row number, request order
    out = []
    for i, smi in enumerate(smiles_list, start=1):
        r = rows.get(i)
        if r is None:
            out.append({"error": "ChemAIRS returned no row for this molecule"})
        elif r.is_error:
            out.append({"error": r.remark or "ChemAIRS reported an error for this molecule"})
        else:
            mol = Chem.MolFromSmiles(smi)
            out.append({
                "sa_score": r.sa_score,
                "scale": "unknown (confirm with ChemAIRS before trusting sa_limit filtering)",
                "molecular_weight": round(Descriptors.MolWt(mol), 1) if mol else None,
                "num_rings": Descriptors.RingCount(mol) if mol else None,
                "method": CHEMAIRS_METHOD,
                "chemairs_feasibility_score": r.feasibility_score,
                "chemairs_remark": r.remark,
            })
    return out
