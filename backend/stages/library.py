"""
Deterministic demo compound library.

WHAT THIS IS: a small, fixed, hand-checked list of well-known molecular
structures used as the *output* of the mocked chemical-space screening
stage. It stands in for the candidate set that a real ultra-large virtual
screen would return.

WHAT THIS IS NOT: a screening result. Nothing here was selected by
screening anything against any protein. These structures were not chosen
for affinity to any target, and their presence in a run says nothing
about their activity against it.

Why a public, recognisable set rather than invented SMILES:
  - every entry parses in RDKit, so the one REAL stage (SA scoring) runs
    on genuine chemistry rather than on strings that merely look valid;
  - the SA scores are therefore real numbers a reviewer can reproduce;
  - the spread is deliberate. Roughly a quarter of the list is
    structurally complex enough to score above the default SA threshold
    of 5.0, so the synthetic-accessibility filter visibly *rejects*
    candidates instead of passing everything through.

The `sa_reference` values below were computed with RDKit's Ertl &
Schuffenhauer implementation and are recorded only as documentation /
regression anchors — the pipeline always recomputes them for real and
never reads these numbers.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class LibraryEntry:
    name: str
    smiles: str
    sa_reference: float  # documentation only; pipeline recomputes with RDKit


# Ordered so that slicing to 10 / 25 / 40 always yields a deterministic and
# *representative* subset: each tier keeps a mix of readily-synthesisable and
# harder structures, so the funnel reduces at every library size.
DEMO_LIBRARY: list[LibraryEntry] = [
    # --- tier 1: first 10 ------------------------------------------------
    LibraryEntry("Acrylamide-quinazoline scaffold", "C=CC(=O)N1CCN(CC1)C2=NC=NC3=C2C=C(C(=C3F)C4=C(C=CC=C4F)O)C#N", 2.96),
    LibraryEntry("Aspirin", "CC(=O)OC1=CC=CC=C1C(=O)O", 1.58),
    LibraryEntry("Ibuprofen", "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O", 2.19),
    LibraryEntry("Imatinib", "CC1=C(C=C(C=C1)NC(=O)C2=CC=C(C=C2)CN3CCN(CC3)C)NC4=NC=CC(=N4)C5=CN=CC=C5", 2.33),
    LibraryEntry("Erythromycin", "CCC1C(C(C(C(=O)C(CC(C(C(C(C(C(=O)O1)C)OC2CC(C(C(O2)C)O)(C)OC)C)OC3C(C(CC(O3)C)N(C)C)O)(C)O)C)C)O)(C)O", 6.09),
    LibraryEntry("Celecoxib", "CC1=CC=C(C=C1)C2=CC(=NN2C3=CC=C(C=C3)S(=O)(=O)N)C(F)(F)F", 2.14),
    LibraryEntry("Caffeine", "CN1C=NC2=C1C(=O)N(C(=O)N2C)C", 2.30),
    LibraryEntry("Digoxigenin", "CC12CCC(CC1)C3C(C2)C4(C(CC3)C5(CCC(C5(CC4)C)C6=CC(=O)OC6)C)O", 5.58),
    LibraryEntry("Gefitinib", "COC1=C(C=C2C(=C1)N=CN=C2NC3=CC(=C(C=C3)F)Cl)OCCCN4CCOCC4", 2.34),
    LibraryEntry("Sucrose", "C(C1C(C(C(C(O1)OC2(C(C(C(O2)CO)O)O)CO)O)O)O)O", 4.48),

    # --- tier 2: next 15 -------------------------------------------------
    LibraryEntry("Erlotinib", "COCCOC1=C(C=C2C(=C1)C(=NC=N2)NC3=CC=CC(=C3)C#C)OCCOC", 2.48),
    LibraryEntry("Naproxen", "CC(C1=CC2=C(C=C1)C=C(C=C2)OC)C(=O)O", 2.21),
    LibraryEntry("Quinine", "COC1=CC2=C(C=CN=C2C=C1)C(C3CC4CCN3CC4C=C)O", 4.52),
    LibraryEntry("Diclofenac", "C1=CC=C(C(=C1)CC(=O)O)NC2=C(C=CC=C2Cl)Cl", 1.87),
    LibraryEntry("Sildenafil", "CCCC1=NN(C2=C1N=C(NC2=O)C3=C(C=CC(=C3)S(=O)(=O)N4CCN(CC4)C)OCC)C", 2.74),
    LibraryEntry("Digoxin", "CC1C(C(CC(O1)OC2C(OC(CC2O)OC3C(OC(CC3O)OC4CCC5(C(C4)CCC6C5CCC7(C6(CCC7C8=CC(=O)OC8)O)C)C)C)C)O)O", 5.98),
    LibraryEntry("Propranolol", "CC(C)NCC(COC1=CC=CC2=CC=CC=C21)O", 2.30),
    LibraryEntry("Palbociclib", "CC1=C(C(=O)N(C2=NC(=NC=C12)NC3=NC=C(C=C3)N4CCNCC4)C5CCCC5)C(=O)C", 2.87),
    LibraryEntry("Cholesterol", "CC(C)CCCC(C)C1CCC2C1(CCC3C2CC=C4C3(CCC(C4)O)C)C", 4.16),
    LibraryEntry("Losartan", "CCCCC1=NC(=C(N1CC2=CC=C(C=C2)C3=CC=CC=C3C4=NNN=N4)CO)Cl", 2.49),
    LibraryEntry("Azithromycin", "CCC1C(C(C(N(CC(CC(C(C(C(C(C(=O)O1)C)OC2CC(C(C(O2)C)O)(C)OC)C)OC3C(C(CC(O3)C)N(C)C)O)(C)O)C)C)C)O)(C)C", 6.15),
    LibraryEntry("Tofacitinib", "CC1CCN(CC1N(C)C2=NC=NC3=C2C=CN3)C(=O)CC#N", 3.67),
    LibraryEntry("Stachyose", "C(C1C(C(C(C(O1)OCC2C(C(C(C(O2)OCC3C(C(C(C(O3)OC4(C(C(C(O4)CO)O)O)CO)O)O)O)O)O)O)O)O)O)O", 5.36),
    LibraryEntry("Atenolol", "CC(C)NCC(COC1=CC=C(C=C1)CC(=O)N)O", 2.44),
    LibraryEntry("Osimertinib", "CN1C=C(C2=CC=CC=C21)C3=NC(=NC=C3)NC4=C(C=C(C(=C4)NC(=O)C=C)N(C)CCN(C)C)OC", 2.92),

    # --- tier 3: final 15 ------------------------------------------------
    LibraryEntry("Ruxolitinib", "C1CCC(C1)C(CC#N)N2C=C(C=N2)C3=C4C=CNC4=NC=N3", 3.40),
    LibraryEntry("Ascorbic acid", "C(C(C1C(=C(C(=O)O1)O)O)O)O", 3.68),
    LibraryEntry("Stevioside", "CC12CCC3(CC1CCC4(C3(CCC5C4(CCCC5(C)C(=O)OC6C(C(C(C(O6)CO)O)O)O)C)C)C2=C)OC7C(C(C(C(O7)CO)O)OC8C(C(C(C(O8)CO)O)O)O)O", 7.87),
    LibraryEntry("Benzimidazole ethylamine", "COC1=CC2=C(C=C1)N(C=N2)CCN", 2.09),
    LibraryEntry("Oleandomycin", "CC1CC(C(C(C)C(C(C)C(=O)C(C(C(=O)O1)C)OC2CC(C(C(O2)C)O)(C)OC)O)OC3C(C(CC(O3)C)N(C)C)O)C4(CO4)C", 6.18),
    LibraryEntry("Cyclohexyl benzamide", "O=C(NC1CCCCC1)C1=CC=CC=C1", 1.39),
    LibraryEntry("Piperazinyl-pyrimidine", "C1CN(CCN1)C2=NC=CC=N2", 2.13),
    LibraryEntry("Terbutaline", "CC(C)(C)NCC(O)C1=CC(O)=CC(O)=C1", 2.83),
    LibraryEntry("Metformin", "CN(C)C(=N)N=C(N)N", 3.21),
    LibraryEntry("Lenalidomide", "C1CC(=O)NC(=O)C1N2CC3=C(C2=O)C=CC=C3N", 2.97),
    LibraryEntry("Fluoxetine", "CNCCC(C1=CC=CC=C1)OC2=CC=C(C=C2)C(F)(F)F", 2.45),
    LibraryEntry("Quinazolinone scaffold", "C=CC(=O)N1CCN(CC1)C2=NC(=O)N(C3=NC(=C(C=C32)F)C4=C(C=CC=C4F)O)C5=C(C=CN=C5C(C)C)C", 3.27),
    LibraryEntry("Reserpine", "COC1=CC2=C(C=C1)C3=C(N2)C4CC5C(CC(C(C5OC)C(=O)OC)OC(=O)C6=CC(=C(C(=C6)OC)OC)OC)CN4CC3", 4.41),
    LibraryEntry("Ibrutinib", "C=CC(=O)N1CCCC(C1)N2C3=NC=NC(=C3C(=N2)C4=CC=C(C=C4)OC5=CC=CC=C5)N", 3.02),
    LibraryEntry("alpha-Cyclodextrin", "C(C1C2C(C(C(O1)OC3C(OC(C(C3O)O)OC4C(OC(C(C4O)O)OC5C(OC(C(C5O)O)OC6C(OC(C(C6O)O)OC7C(OC(O2)C(C7O)O)CO)CO)CO)CO)CO)O)O)O", 7.84),
]

LIBRARY_SIZES = [10, 25, 40]
DEFAULT_LIBRARY_SIZE = 25


def get_library(limit: int = DEFAULT_LIBRARY_SIZE) -> list[LibraryEntry]:
    """Deterministic slice of the demo library — same input, same output."""
    if limit <= 0:
        return []
    return DEMO_LIBRARY[:limit]


def library_size() -> int:
    return len(DEMO_LIBRARY)
