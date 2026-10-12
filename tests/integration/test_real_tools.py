"""
Tests that execute the REAL tools. Skipped when a tool/licence is unavailable.
Run:  pytest -m real -v      (tools found via .env.local or the environment)

These are the acceptance evidence for "genuinely executed". The FlexX test is
the one that must be run on a machine that has the FlexX executable.
"""
from __future__ import annotations

import pytest
from rdkit import Chem
from rdkit.Chem import rdMolAlign

from backend.science import receptor as recep
from backend.science import site as sitelib
from backend.science.engines.common import DockInput, DockSettings
from backend.tools import registry

pytestmark = pytest.mark.real


def need(key):
    s = registry.get(key)
    if not s.available:
        pytest.skip(f"{s.name} unavailable: {s.reason}")
    return s


@pytest.fixture(scope="module")
def prepared(tmp_path_factory, ):
    import os
    from backend.config import APP_ROOT
    pdb = APP_ROOT / "data" / "fixtures" / "demo_kras_g12d_7rpz.pdb"
    d = tmp_path_factory.mktemp("prep")
    ref = sitelib.find_site(pdb, "6IC:A:202")
    recep.write_chain_pdb(pdb, d / "protein.pdb", "A", ref)
    recep.write_reference_ligand(pdb, d / "ref_ligand.sdf", ref)
    return d, ref, sitelib.box_from_ligand(ref)


def test_license_diagnostics_report_a_valid_key():
    s = need("hydescorer")
    assert s.license_ok and "Valid key" in s.license_detail


def test_hyde_scores_the_crystal_ligand_of_7rpz_with_a_sub_nanomolar_estimate(prepared, tmp_path):
    from backend.science.engines.hyde import HydeEngine
    from rdkit.Chem import AllChem
    need("hydescorer")
    d, ref, box = prepared
    # The pocket-defining reference only needs connectivity; the ligand being SCORED needs correct
    # bond orders, so assign them from the CCD SMILES of 6IC.
    raw = next(Chem.SDMolSupplier(str(d / "ref_ligand.sdf"), removeHs=False, sanitize=False))
    tmpl = Chem.MolFromSmiles("C#Cc1c(ccc2c1c(cc(c2)O)c3c(c4c(cn3)c(nc(n4)OC[C@@]56CCCN5C[C@@H](C6)F)N7C[C@H]8CC[C@@H](C7)N8)F)F")
    crystal = Chem.AddHs(AllChem.AssignBondOrdersFromTemplate(tmpl, Chem.RemoveHs(raw, sanitize=False)), addCoords=True)
    crystal.SetProp("_Name", "6IC_crystal")
    w = Chem.SDWriter(str(tmp_path / "crystal.sdf"))
    w.write(crystal)
    w.close()
    out = HydeEngine().score(tmp_path / "crystal.sdf", protein_pdb=d / "protein.pdb", ref_ligand_sdf=d / "ref_ligand.sdf",
                             workdir=tmp_path, log_path=tmp_path / "h.log", threads=2, timeout=120)
    s = next(iter(out.values()))
    assert s.cumulative_kj_mol < -50          # strong, as expected for a picomolar-class inhibitor (MRTX1133)
    assert s.affinity_upper_nm < 10 and s.inter_clash == 0


def test_vina_redocking_of_the_7rpz_ligand_reproduces_the_crystal_pose(prepared, tmp_path):
    """Standard docking validation: RMSD < 2 Å is the accepted success criterion."""
    from backend.science.engines.vina import VinaEngine
    from rdkit.Chem import AllChem
    need("vina")
    d, ref, box = prepared
    rec = recep.prepare_vina_receptor(d / "protein.pdb", tmp_path, box["center"], box["size"])
    crystal = next(Chem.SDMolSupplier(str(d / "ref_ligand.sdf"), removeHs=False, sanitize=False))
    # SMILES of the CCD ligand 6IC (RCSB) -> docking input
    smi = "C#Cc1c(ccc2c1c(cc(c2)O)c3c(c4c(cn3)c(nc(n4)OC[C@@]56CCCN5C[C@@H](C6)F)N7C[C@H]8CC[C@@H](C7)N8)F)F"
    res = VinaEngine().dock_batch([DockInput("1", "6IC", smi)], receptor_pdbqt=rec, center=box["center"], size=box["size"],
                                  workdir=tmp_path / "w", log_path=tmp_path / "v.log",
                                  cfg=DockSettings(cpu=4, exhaustiveness=8, num_modes=3))[0]
    assert res.ok and res.score < -8
    pose = next(Chem.SDMolSupplier(res.pose_sdf and str(_w(tmp_path, res.pose_sdf)), removeHs=True))
    tmpl = Chem.RemoveHs(AllChem.AssignBondOrdersFromTemplate(Chem.MolFromSmiles(smi), Chem.RemoveHs(crystal, sanitize=False)))
    assert rdMolAlign.CalcRMS(pose, tmpl) < 2.0


def _w(tmp, text):
    p = tmp / "pose.sdf"
    p.write_text(text)
    return p


def test_admet_ai_predicts_known_properties():
    from backend.science import admet
    r = admet.predict(["CC(=O)Oc1ccccc1C(=O)O", "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"], threads=2)
    assert r[0]["values"]["HIA_Hou"] > 0.9            # aspirin: well absorbed
    assert r[1]["values"]["BBB_Martins"] > 0.5        # caffeine crosses the blood-brain barrier
    assert len(r[0]["values"]) >= 25


def test_admet_ai_handles_duplicate_smiles_without_crashing():
    """Regression test: the model's own result frame is indexed BY SMILES, so a duplicate input
    SMILES used to make a label lookup (`df.loc[smi]`) return several rows instead of one and
    crash with a TypeError deep inside pandas. Results must come back one-per-input, in order,
    not one-per-unique-SMILES."""
    from backend.science import admet
    aspirin = "CC(=O)Oc1ccccc1C(=O)O"
    caffeine = "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"
    r = admet.predict([aspirin, caffeine, aspirin, aspirin], threads=2)
    assert len(r) == 4
    assert r[0]["values"] == r[2]["values"] == r[3]["values"]
    assert r[1]["values"] != r[0]["values"]


def test_flexx_docks_and_scores_a_small_batch_FOR_LICENSED_EXECUTION(prepared, tmp_path):
    """Runs only where the FlexX executable + valid licence exist (e.g. the company server)."""
    from backend.science.engines.flexx import FlexxEngine
    need("flexx")
    d, ref, box = prepared
    items = [DockInput(str(i), f"T{i}", s) for i, s in enumerate(
        ["CC(=O)Oc1ccccc1C(=O)O", "c1ccc2[nH]ccc2c1", "CC(C)Cc1ccc(cc1)C(C)C(=O)O"])]
    res = FlexxEngine().dock_batch(items, protein_pdb=d / "protein.pdb", ref_ligand_sdf=d / "ref_ligand.sdf",
                                   workdir=tmp_path / "fx", log_path=tmp_path / "f.log",
                                   cfg=DockSettings(cpu=2, num_modes=3), timeout=600)
    assert sum(r.ok for r in res) >= 2
    from backend.science import sdf as sdflib
    for r, it in zip(res, items):
        if r.ok:
            poses = sdflib.parse_sdf(r.pose_sdf)
            assert len(poses) == r.n_poses <= 3 and all(p.name == it.display_id for p in poses)
            assert r.score == min(float(p.props["FLEXX_DOCKING_SCORE"]) for p in poses) and r.score < 0
            assert Chem.MolFromMolBlock(poses[0].molblock, removeHs=False) is not None


def test_fastgrow_grows_a_fragment_from_the_7rpz_ligand(prepared, tmp_path):
    """Builds a small fragment DB with FastGrowDBCreator and grows from a hydrogen of the crystal ligand."""
    from backend.science.engines import fastgrow as fg
    from rdkit.Chem import AllChem
    need("fastgrow")
    need("fastgrow_db_creator")
    d, ref, box = prepared
    smi = "C#Cc1c(ccc2c1c(cc(c2)O)c3c(c4c(cn3)c(nc(n4)OC[C@@]56CCCN5C[C@@H](C6)F)N7C[C@H]8CC[C@@H](C7)N8)F)F"
    raw = next(Chem.SDMolSupplier(str(d / "ref_ligand.sdf"), removeHs=False, sanitize=False))
    pose = Chem.AddHs(AllChem.AssignBondOrdersFromTemplate(Chem.MolFromSmiles(smi), Chem.RemoveHs(raw, sanitize=False)),
                      addCoords=True)
    pool = ["CC(=O)Nc1ccc(O)cc1", "CC(C)Cc1ccc(cc1)C(C)C(=O)O", "COc1ccc(CCN)cc1", "OCCN1CCOCC1", "CN1CCN(CC1)c1ccccc1",
            "CC(=O)OC1=CC=CC=C1C(=O)O", "NC(=O)c1ccccn1", "CCOC(=O)c1ccc(N)cc1", "Cc1ccc(S(N)(=O)=O)cc1", "OC(=O)CCc1ccccc1",
            "CC(C)NCC(O)COc1cccc2ccccc12", "c1ccc2[nH]ccc2c1", "O=C(O)c1ccccc1O", "CCN(CC)CC", "OCC1OC(O)C(O)C(O)C1O",
            "CC1=CC=C(C=C1)C(=O)N", "FC(F)(F)c1ccccc1", "ClC1=CC=CC=C1", "COC(=O)c1ccccc1", "CC(N)C(=O)O", "c1ccncc1C(=O)N"]
    db, n = fg.build_fragment_db(pool, tmp_path / "db", tmp_path / "creator.log", threads=2)
    assert n >= 20 and db.stat().st_size > 0
    evs = fg.choose_exit_vectors(pose, d / "protein.pdb", n=3)
    assert evs, "no open growth position found on the crystal ligand"
    found = 0
    for k, ev in enumerate(evs):
        core = fg.core_with_linker(pose, ev)
        cp = tmp_path / f"core{k}.sdf"
        w = Chem.SDWriter(str(cp)); w.write(core); w.close()
        found += len(fg.grow(cp, d / "protein.pdb", db, tmp_path / f"g{k}.sdf", tmp_path / f"fg{k}.log", 10, 2, 300))
    assert found >= 1
