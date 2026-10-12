# Scientific decisions and limits

**Docking (Vina).** Ligands: RDKit ETKDGv3 conformer, MMFF clean-up, Meeko PDBQT. Receptor: selected chain,
heteroatoms/waters removed, PDBFixer adds missing atoms and hydrogens at pH 7.4, rigid. Box from the
reference ligand extent + 6 Å padding (16–40 Å). Score = Vina predicted ΔG (kcal/mol) of the best pose; it is
not an experimental affinity and Vina is known to over-predict for large ligands (the 75-atom 7RPZ ligand
scores −14.7 kcal/mol, far beyond its measured affinity). **Validation:** re-docking the crystal ligand 6IC into
7RPZ gives a best-pose RMSD of 0.51 Å (success criterion < 2 Å). Cofactors (GDP/Mg) are removed for Vina.

**HYDE.** Run with `-p protein.pdb -r reference_ligand.sdf --keep-input-sd-tags --write-atom-scores`.
Parsed: the vendor's affinity lower/upper bounds, ligand/lipophilic efficiency, clash and H-bond counts.
The scored quantities follow the company's own extractor (`HYDE_SCORER.html`, "HYDE SDF Extractor"): per-atom values
of `BIOSOLVEIT.HYDE_ATOM_SCORES` are summed into Total, Rec-Desolv, Rec-IA, Lig-Desolv and Lig-IA (kJ/mol, negative =
favourable), plus the affinity bounds, LE/LLE, clogP, MW, TPSA, H-bonds, unfavourable torsions and the three traffic
lights. The implementation was checked against the extractor's own JavaScript run on real hydescorer output (totals
agree), and the sum is consistent with the vendor's range: −26.77 kJ/mol ⇒ Kd ≈ 20 µM (RT ln Kd at 298 K), inside the
vendor's 2.05–204 µM. The UI offers the extractor's kJ/mol ↔ kcal/mol switch (÷ 4.184), and each campaign writes
`hyde/hyde_scored_all.sdf`, which loads directly into the extractor.
HYDE silently drops poses failing its quality criteria; these appear as failed molecules with that reason.
For multi-pose FlexX output the pose with the lowest cumulative score is kept. The reference-ligand file only
needs connectivity; the scored ligands need correct bond orders.

**Docking (FlexX).** `flexx -i ligands3d.sdf -p protein.pdb -r reference_ligand.sdf -o poses.sdf --max-nof-conf N
--thread-count N --keep-input-sd-tags`; ligands get an RDKit 3D start geometry (FlexX skips molecules without 3D).
Observed with the real binary: poses are written as `<name>_<n>` with `BIOSOLVEIT.DOCKING_SCORE` (more negative = better;
the scale is FlexX's own and not comparable with Vina or HYDE). All poses are kept and re-scored by HYDE, which picks the
best pose per molecule. FlexX and HYDE rank the same molecules differently (e.g. FlexX's best score was HYDE's 7th), which is
why the ranking measure is an explicit choice. Speed on the 7RPZ pocket (a large, open site): about 13 s/molecule wall time
at 2 batches × 4 threads, much slower than the vendor's "< 1 s/ligand" for small pockets.

**Analogue generation (FastGrow, optional).** After ranking, up to 3 top candidates are used as parents. For each, the
best available pose (HYDE-optimised, else docking) gets hydrogens; up to 2 *exit vectors* are chosen automatically:
hydrogens with at least 2.8 Å of free space and at least 8 protein atoms within 6 Å (so the new group has room and context),
on different atoms. The hydrogen is turned into FastGrow's linker atom `R`, which keeps the exact C-H direction and leaves
the ring system untouched (replacing an aromatic CH atom itself fails to parse). FastGrow then screens a fragment
database and returns grown 3D molecules scored by `BIOSOLVEIT.FASTGROW_OPTIMIZED_SCORE` (more negative = better; its own
scale, unrelated to docking/HYDE and not an affinity; occasional large positive values are poor fits). Fragment source:
`FASTGROW_DB` (e.g. BioSolveIT's 120k library, a login-gated download) if set; otherwise BRICS mono-attachment fragments
(2-12 heavy atoms) of the campaign library + known drugs + a seeded 1,500-molecule ZINC20 sample, built with
FastGrowDBCreator and cached (about 1,200 fragments). Outputs are neutralised, de-duplicated and checked against the campaign
library; they are labelled **generated, not catalogue**, never enter the ranked list, and their novelty versus the
literature is not checked. Exit-vector choice is a heuristic, not design intent.

**SA.** RDKit Contrib SA_Score (Ertl & Schuffenhauer 2009), 1–10, default limit 5.

**ADMET.** ADMET-AI v2 (41 TDC endpoints). A toxicity *alert* requires probability ≥ 0.5 **and** a
DrugBank-approved percentile ≥ 90 for AMES, hERG, DILI, ClinTox or carcinogenicity. Reason: a bare 0.5 cut
flagged 93% of a ZINC sample and 52% (hERG) / 58% (DILI) of known drugs; with the percentile rule 69% of
ZINC and 71% of known drugs have no alert. This is a triage heuristic, not a safety assessment; the model has no
applicability-domain estimate.

**Library.** ZINC20 "Deep Docking" SMILES (20 chunk files of ~450 MB / ~10 M molecules each). At the start of a run
the first `ZINC_PREFIX_MB` (default 3) MB of one chunk — chosen by the seed — is fetched by HTTP range request, cached
in `<DATA_DIR>/library_cache` (not in the database; `scripts/prefetch_zinc.py` fills it ahead of time or deeper) and
sampled with the seed: the same seed gives the same molecules. Catalogue compounds are not novel molecules. The archive is ordered by ID, so the sample is
reproducible but not uniformly random over ZINC. Filters: 10–40 heavy atoms, MW 150–600, ≤ 12 rotatable bonds,
C/H/N/O/F/P/S/Cl/Br/I, largest fragment, neutralised, de-duplicated by InChIKey.

**Ranking.** One named measure (default HYDE cumulative score), ties by SA; mocked values never participate.

**GPU.** See `docs/GPU.md`: only ADMET-AI can use a GPU; Vina, FlexX, HYDE, FastGrow and RDKit are CPU programs. On this machine the NVIDIA driver is not installed, so everything runs on CPU.
RTX 4050 is present, but no NVIDIA driver is installed on the development machine, and CPU inference is fast enough.
