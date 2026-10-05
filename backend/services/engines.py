"""
Concrete stage adapters.

One class per pipeline stage. Four wrap the deterministic demo logic in
`stages/mocks.py`; one (SA) wraps genuine RDKit chemistry. All of them
declare `is_mocked` honestly through `ServiceInfo`, and `REGISTRY` below
is the single list that the pipeline, the API and the UI all read, so no
screen can claim an engine is real when it isn't.

Replacing a mock with a real partner integration means rewriting one
`run_*` body here. Nothing in pipeline.py, models.py, main.py or the
frontend needs to change.
"""
from __future__ import annotations

import time

from ..config import settings
from ..stages import mocks, sa_scoring
from .base import ServiceInfo, TargetContext

# Mocked stages return instantly, which makes the pipeline finish before the
# UI can render a single state change. A small deliberate delay makes the
# orchestration visible during a demo. It applies ONLY to mocked stages —
# real SA scoring is never artificially slowed — and tests set it to 0.


def pace(delay: float | None) -> None:
    """Sleep once for a mocked stage. Called by the orchestrator per STAGE,
    not per molecule — pacing every call would make run time scale with
    library size and turn a few-second demo into a minute-long one."""
    seconds = settings.stage_delay_seconds if delay is None else delay
    if seconds > 0:
        time.sleep(seconds)


class ChemicalSpaceScreeningService:
    """Stage 02 — stands in for ultra-large virtual screening."""

    info = ServiceInfo(
        key="screening",
        name="Chemical Space Screening",
        is_mocked=True,
        backend="Demo adapter (in-process)",
        note=(
            "Returns a fixed demo library. No screening of any chemical space "
            "is performed, and the compounds are not selected for this target."
        ),
    )

    def describe(self) -> dict:
        return _describe(self.info)

    def run(self, target: TargetContext, limit: int, delay: float | None = None) -> list[dict]:
        pace(delay)  # screening is a single call, so it paces itself
        return [
            {"name": name, "smiles": smiles, "source": "demo_library"}
            for name, smiles in mocks.screen_chemical_space_detailed(limit)
        ]


class SAService:
    """Stage 03 — REAL. RDKit's Ertl & Schuffenhauer SAscore."""

    info = ServiceInfo(
        key="sa",
        name="Synthetic Accessibility",
        is_mocked=False,
        backend="RDKit (in-process)",
        note=(
            "Genuine computation: RDKit's bundled Ertl & Schuffenhauer (2009) "
            "SAscore implementation, scale 1 (easy) to 10 (hard)."
        ),
    )

    def describe(self) -> dict:
        return _describe(self.info)

    def run(self, target: TargetContext, smiles: str) -> dict:
        # No pacing: this is real work and is timed as it actually runs.
        return sa_scoring.score_molecule(smiles)


class ADMETService:
    """Stage 04 — mocked ADMET prediction."""

    info = ServiceInfo(
        key="admet",
        name="ADMET Prediction",
        is_mocked=True,
        backend="Demo adapter (in-process)",
        note=(
            "Four illustrative deterministic values and a profile label. Not a "
            "real ADMET model; the 220-property figure describes the scale the "
            "production stage would operate at, not what is computed here."
        ),
    )

    def describe(self) -> dict:
        return _describe(self.info)

    def run(self, target: TargetContext, smiles: str, delay: float | None = None) -> dict:
        return mocks.predict_admet(smiles, target_seed=target.seed)


class BindingAffinityService:
    """Stage 05 — mocked Kd / ΔG."""

    info = ServiceInfo(
        key="affinity",
        name="Binding Affinity",
        is_mocked=True,
        backend="Demo adapter (CPU/Docker in production)",
        note=(
            "Deterministic placeholder Kd and ΔG derived from the structure "
            "checksum and ligand SMILES. No docking, scoring function or free-"
            "energy calculation is performed."
        ),
    )

    def describe(self) -> dict:
        return _describe(self.info)

    def run(self, target: TargetContext, smiles: str, delay: float | None = None) -> dict:
        return mocks.predict_binding_affinity(smiles, target_seed=target.seed)


class UnbindingKineticsService:
    """Stage 06 — mocked koff / residence time."""

    info = ServiceInfo(
        key="kinetics",
        name="Unbinding Kinetics",
        is_mocked=True,
        backend="Demo adapter (GPU/Docker in production)",
        note=(
            "Deterministic placeholder koff and residence time. No molecular "
            "dynamics, enhanced sampling or trajectory analysis is performed."
        ),
    )

    def describe(self) -> dict:
        return _describe(self.info)

    def run(
        self,
        target: TargetContext,
        smiles: str,
        kd_nm: float,
        delay: float | None = None,
    ) -> dict:
        return mocks.predict_unbinding_kinetics(smiles, kd_nm, target_seed=target.seed)


def _describe(info: ServiceInfo) -> dict:
    return {
        "key": info.key,
        "name": info.name,
        "is_mocked": info.is_mocked,
        "backend": info.backend,
        "note": info.note,
    }


# Instantiated once; these adapters are stateless.
screening_service = ChemicalSpaceScreeningService()
sa_service = SAService()
admet_service = ADMETService()
affinity_service = BindingAffinityService()
kinetics_service = UnbindingKineticsService()

REGISTRY = [
    screening_service,
    sa_service,
    admet_service,
    affinity_service,
    kinetics_service,
]


def describe_all() -> list[dict]:
    """Engine inventory for the API/UI. The one place this is declared."""
    return [service.describe() for service in REGISTRY]
