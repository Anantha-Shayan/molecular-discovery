# Demo fixtures

Files here are **demo fixtures** bundled so the application's full flow —
target intake, validation, job creation, pipeline execution — works with no
network access and no proprietary partner software.

## `demo_kras_g12d_7rpz.pdb`

| | |
|---|---|
| Source | RCSB PDB, `https://files.rcsb.org/download/7RPZ.pdb` |
| PDB ID | 7RPZ |
| sha256 | `6dae3273f94ab03890b77a28c5440416da6df9065eae8e69cb19b35a17333dff` |
| Retrieved | 2026-10-05, unmodified |

Everything below is read **from the file's own header and coordinate
records** by `backend/targets/structure.py`. None of it is an estimate
produced by this application:

- Title: `KRAS G12D IN COMPLEX WITH MRTX-1133`
- Method: X-RAY DIFFRACTION, resolution 1.30 Å (`REMARK 2`)
- Chain A, 168 residues, residue numbering 1–169
- 1,690 atom records in the first conformer (1,342 ATOM, 348 HETATM)
- Non-solvent HETATM residues: `6IC` (the bound inhibitor), `GDP`, `MG`

### Why this structure

The earlier version of this project labelled its target "KRAS G12D / PDB
8AZX". That was wrong: **8AZX is KRAS G12C** (in complex with BI-2865).
7RPZ is a genuine KRAS **G12D** structure — residue 12 of chain A is `ASP`,
which you can verify directly:

```bash
awk '/^ATOM/ && substr($0,23,4)+0==12 && substr($0,13,4)==" CA "' \
    data/fixtures/demo_kras_g12d_7rpz.pdb
```

### What this fixture does and does not demonstrate

**Does:** that the platform can read a real crystallographic structure,
extract chains/residues/atoms/metadata from it, store it as a run artifact,
and carry it through the pipeline.

**Does not:** validate any scientific claim about druggability, pocket
tractability, or the suitability of this target for the compounds this demo
produces. The pipeline's screening, ADMET, affinity and kinetics stages are
mocked — see `docs/DEMO_DATA.md`.

The structure is used here purely as realistic input data. It is not an
endorsement of, nor a result about, KRAS G12D as a drug target.
