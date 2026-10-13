"""
Tests the REAL ChemAIRS SAScore API. Skipped unless CHEMAIRS_BASE_URL and
CHEMAIRS_API_KEY are set in the environment (.env.local works, same as the
BioSolveIT tests in test_real_tools.py).

Run once you have a working key:
    pytest -m real tests/integration/test_chemairs.py -v

IMPORTANT: read backend/science/chemairs.py's module docstring first. The
SAScore numeric scale/direction and the task-completion status values used
here are INFERRED from the documentation's examples, not confirmed against
a live account (no working key was available while writing this). The
first test below exists specifically to surface what a real response
actually looks like — read its printed output, not just its pass/fail.
"""
from __future__ import annotations

import pytest

from backend.config import settings

pytestmark = pytest.mark.real


def need_chemairs():
    if not settings.chemairs_configured:
        pytest.skip("CHEMAIRS_BASE_URL / CHEMAIRS_API_KEY not set")


def test_chemairs_scores_two_known_molecules_INSPECT_THE_PRINTED_OUTPUT():
    """Aspirin (simple, should score easy) vs. a contrived, heavily decorated molecule
    (should score harder) — a weak sanity check on direction, not a strict assertion,
    since the scale/direction is not documented. READ THE PRINTED SCORES."""
    need_chemairs()
    from backend.stages import sa_scoring
    aspirin = "CC(=O)Oc1ccccc1C(=O)O"
    complex_mol = "CC1=CC2=NC(=CS2)C(=O)N[C@@H](CC3=CC=C(C=C3)OC4=CC=CC=C4Cl)C(=O)N[C@@H](CCCCN)C(=O)O"
    out = sa_scoring.score_batch_for_stage([aspirin, complex_mol])
    print("\n--- ChemAIRS SAScore real response ---")
    for smi, r in zip([aspirin, complex_mol], out):
        print(smi, "->", r)
    assert all("error" not in r for r in out), out
    assert all(r["sa_score"] is not None for r in out)
    assert sa_scoring.active_method() == sa_scoring.CHEMAIRS_METHOD


def test_chemairs_reports_a_batch_level_error_for_a_bad_key(monkeypatch):
    need_chemairs()
    from backend.science import chemairs
    monkeypatch.setattr(chemairs.settings, "chemairs_api_key", "sk-definitely-not-a-real-key")
    with pytest.raises(chemairs.ChemAIRSError):
        chemairs.score_batch(["CCO"])
