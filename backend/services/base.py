"""
Scientific-service adapter boundary.

Each pipeline stage is reached through a small adapter object rather than
by calling a function directly. The point is the *seam*: when a real
partner engine becomes available, you replace one adapter class and the
orchestrator, data model, API and UI stay untouched.

Every adapter declares what it actually is:

    is_mocked = True   -> output is synthesised by this repository
    is_mocked = False  -> output comes from a real computation

That flag is the single source of truth. It propagates to
`StageResult.is_mocked` in the database, to the stage badges in the API,
and to the engine table in the UI, so the three can never disagree about
which numbers are real.

TargetContext is passed to every stage so adapters receive the user's
actual protein, not just a molecule. The mocked adapters derive their
deterministic values from it (see services/demo.py), which is what makes
the pipeline genuinely *consume* the target rather than merely display
its name.
"""
from __future__ import annotations

import dataclasses
from typing import Protocol


@dataclasses.dataclass(frozen=True)
class TargetContext:
    """The protein a stage is operating against."""

    target_id: str
    name: str
    pdb_id: str | None
    chain: str | None
    checksum: str | None
    structure_path: str | None
    residue_count: int | None = None
    atom_count: int | None = None

    @property
    def seed(self) -> str:
        """Stable per-target seed for deterministic demo output.

        Uses the structure checksum, so two different uploaded files give
        genuinely different results while the same file always reproduces.
        """
        return f"{self.checksum or self.target_id}:{self.chain or '-'}"


@dataclasses.dataclass(frozen=True)
class ServiceInfo:
    """What this adapter is, for honest display in the API and UI."""

    key: str
    name: str
    is_mocked: bool
    backend: str          # e.g. "RDKit (in-process)" / "Demo adapter"
    note: str             # one line a reviewer can read and trust


class StageService(Protocol):
    """Conceptual contract shared by real and mocked stage engines.

    Deliberately not forced into a submit/poll/fetch shape: the real
    engines behind these stages would have genuinely different contracts
    (an in-process library call vs. a containerised batch job), and
    pretending otherwise would be architecture theatre. What every
    adapter *does* share is: describe yourself honestly, and run.
    """

    info: ServiceInfo

    def describe(self) -> dict: ...
