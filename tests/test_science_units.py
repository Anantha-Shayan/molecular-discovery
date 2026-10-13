"""Unit tests of parsing, rules and safety code. No external tool is executed
(except trivial system commands); fixtures are synthetic - see tests/fixtures/README.md."""
from __future__ import annotations

import time

import pytest

from backend.campaign import params as P
from backend.science import admet, library, sdf, site
from backend.science.engines import flexx as flexx_engine
from backend.science.engines import hyde
from backend.tools import proc, registry

ETHANOL_MOL = """ethanol
  fixture

  3  2  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.5000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.0000    1.4000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  2  3  1  0
M  END
"""


def record(name, props):
    body = ETHANOL_MOL.replace("ethanol", name, 1)
    for k, v in props.items():
        body += f"> <{k}>\n{v}\n\n"
    return body + "$$$$\n"


# ----------------------------------------------------------- subprocesses ---
def test_timeout_kills_the_whole_process_group(tmp_path):
    t0 = time.monotonic()
    with pytest.raises(proc.ToolTimeout):
        proc.run(["sh", "-c", "sleep 30 & sleep 30"], log_path=tmp_path / "l.log", timeout=1)
    assert time.monotonic() - t0 < 10


def test_nonzero_exit_is_a_tool_error_with_output_tail(tmp_path):
    with pytest.raises(proc.ToolError) as e:
        proc.run(["sh", "-c", "echo bad input >&2; exit 3"], log_path=tmp_path / "l.log", timeout=10)
    assert e.value.returncode == 3 and "bad input" in e.value.tail


def test_no_shell_interpretation_of_arguments(tmp_path):
    proc.run(["echo", "$(touch pwned); ; |"], log_path=tmp_path / "l.log", timeout=10)
    assert not (tmp_path / "pwned").exists()


def test_child_environment_excludes_application_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secret@h/db")
    proc.run(["sh", "-c", "env"], log_path=tmp_path / "l.log", timeout=10)
    assert "secret" not in (tmp_path / "l.log").read_text()


def test_missing_tool_is_reported_unavailable_with_a_reason(monkeypatch):
    import dataclasses
    monkeypatch.setattr(registry, "settings", dataclasses.replace(registry.settings, flexx_bin="/nonexistent/flexx"))
    s = registry._probe("flexx")
    assert not s.available and "not found" in s.reason.lower()


# --------------------------------------------------------------------- SDF ---
def test_sdf_parse_reads_names_and_tags():
    recs = sdf.parse_sdf(record("a", {"X": "1"}) + record("b", {"X": "2"}))
    assert [r.name for r in recs] == ["a", "b"] and recs[1].props["X"] == "2"


def test_truncated_sdf_is_rejected_not_silently_accepted():
    with pytest.raises(sdf.SdfError):
        sdf.parse_sdf(record("a", {}) + record("b", {})[:-5])


# -------------------------------------------------------------------- HYDE ---
ATOMS = ("# Atom-ID; Total; Rec-Desolv; Rec-IA; Lig-Desolv; Lig-IA\n"
         "1;-1.5;0;0;0;0\n2;-2.0;0;0;0;0\n3;0.5;0;0;0;0")


def hyde_rec(name, atoms, lo, hi):
    return record(name, {
        hyde.TAG_ATOMS: atoms, hyde.TAG_LOWER: lo, hyde.TAG_UPPER: hi, hyde.TAG_LE: "0.2",
        "BIOSOLVEIT.INTER_CLASH range: 0=green, 1=yellow, 2=red": "2",
        "BIOSOLVEIT.NUMBER_OF_H_BONDS": "3"})


def test_hyde_cumulative_score_is_the_sum_of_atom_totals():
    assert hyde.cumulative_from_atom_scores(ATOMS) == -3.0


def test_hyde_keeps_the_best_pose_per_molecule_and_parses_tags():
    better = ATOMS.replace("1;-1.5", "1;-9.5")
    out = hyde.parse_hyde_output(hyde_rec("m1", ATOMS, "5", "50") + hyde_rec("m1", better, "1", "10"))
    assert out["m1"].cumulative_kj_mol == -11.0 and out["m1"].affinity_upper_nm == 10.0
    assert hyde.clash_label(out["m1"].inter_clash) == "major" and out["m1"].h_bonds == 3


def test_hyde_corrupt_output_is_an_error():
    with pytest.raises(sdf.SdfError):
        hyde.parse_hyde_output(hyde_rec("m1", ATOMS, "1", "2")[:-6])


# ------------------------------------------------------------------- FlexX ---
def test_flexx_output_parser_groups_poses_named_name_underscore_n():
    """Record naming `<name>_<n>` taken from a real FlexX 6.5.3 run; scores here are synthetic."""
    text = (record("MDP-1_1", {flexx_engine.TAG_SCORE: "-12.5"}) + record("MDP-1_2", {flexx_engine.TAG_SCORE: "-30.1"})
            + record("MDP-2_1", {flexx_engine.TAG_SCORE: "-3.0"}) + record("unknown_1", {flexx_engine.TAG_SCORE: "-99"}))
    out = flexx_engine.parse_flexx_output(text, {"MDP-1": "id1", "MDP-2": "id2"}, "FlexX")
    assert out["id1"].score == -30.1 and out["id1"].n_poses == 2 and out["id2"].ok and "unknown" not in out
    first = sdf.parse_sdf(out["id1"].pose_sdf)
    assert [r.name for r in first] == ["MDP-1", "MDP-1"] and first[0].props["FLEXX_DOCKING_SCORE"] == "-30.1"


# -------------------------------------------------------------------- site ---
def test_ligand_sites_found_in_the_demo_structure(demo_pdb_bytes, tmp_path):
    f = tmp_path / "x.pdb"
    f.write_bytes(demo_pdb_bytes)
    keys = {s.het_name for s in site.list_ligand_sites(f)}
    assert "6IC" in keys and "HOH" not in keys and "MG" not in keys
    box = site.box_from_ligand(site.find_site(f, "6IC:A:202"))
    assert all(site.BOX_MIN <= v <= site.BOX_MAX for v in box["size"])


@pytest.mark.parametrize("center,size", [([0, 0], [20, 20, 20]), ([0, 0, float("nan")], [20, 20, 20]),
                                         ([2.5, 5.1, -23], [5, 20, 20]), ([2.5, 5.1, -23], [20, 20, 90])])
def test_manual_box_validation_rejects_bad_input(demo_pdb_bytes, tmp_path, center, size):
    f = tmp_path / "x.pdb"
    f.write_bytes(demo_pdb_bytes)
    with pytest.raises(site.SiteError):
        site.validate_manual_box(center, size, f)


def test_manual_box_on_the_pocket_is_accepted(demo_pdb_bytes, tmp_path):
    f = tmp_path / "x.pdb"
    f.write_bytes(demo_pdb_bytes)
    assert site.validate_manual_box([2.56, 5.15, -23.0], [24, 24, 24], f)["protein_atoms_in_box"] > 50


# ----------------------------------------------------------------- library ---
def test_standardize_rejects_bad_molecules_with_reasons():
    f = library.Filters()
    for smi, why in [("not a smiles", "parsed"), ("C", "heavy"), ("[Na+].[Cl-]", "heavy"),
                     ("C[Si](C)(C)OC1=CC=CC=C1CCCCCC", "elements")]:
        with pytest.raises(ValueError, match=why):
            library.standardize(smi, f)


def test_standardize_keeps_the_largest_fragment_and_neutralises():
    r = library.standardize("CC(=O)Oc1ccccc1C(=O)[O-].[Na+]", library.Filters())
    assert "Na" not in r["smiles"] and r["smiles"].endswith("C(=O)O")


def test_upload_is_deduplicated_and_reports_rejections():
    text = "CC(=O)Oc1ccccc1C(=O)O aspirin\nOC(=O)c1ccccc1OC(C)=O aspirin-again\nnonsense bad\n"
    mols, meta = library.parse_upload(text, 10, library.Filters())
    assert len(mols) == 1 and meta["rejected"]["duplicate"] == 1 and meta["rejected"]["invalid_or_filtered"] == 1


# -------------------------------------------------------------------- ADMET ---
def test_admet_alert_needs_probability_and_approved_drug_percentile():
    v = {"hERG": 0.9, "AMES": 0.9, "DILI": 0.2, "ClinTox": 0.6}
    pct = {"hERG": 95.0, "AMES": 40.0, "DILI": 99.0, "ClinTox": 90.0}
    assert admet.evaluate_flags(v, pct) == ["hERG", "ClinTox"]
    assert admet.profile_for(0) == "Favorable" and admet.profile_for(1) == "Moderate" and admet.profile_for(2) == "Concern"


# ------------------------------------------------------------------- params ---
@pytest.mark.parametrize("bad", [{"engine": "gold"}, {"library_count": 0}, {"library_count": 10**6},
                                 {"exhaustiveness": 0}, {"sa_threshold": 0}, {"dock_keep_fraction": 0},
                                 {"rank_by": "magic"}, {"library_source": "chembl"}])
def test_invalid_campaign_parameters_are_rejected(bad):
    with pytest.raises(P.ParamError):
        P.resolve(bad)


def test_rank_by_hyde_falls_back_when_hyde_is_off():
    assert P.resolve({"enable_hyde": False, "rank_by": "hyde"})["rank_by"] == "docking"


# ------------------------------------------------------------ orphans / SDF ---
def test_orphaned_tool_processes_from_a_previous_run_are_killed(tmp_path):
    import subprocess
    marker = str(tmp_path / "runs")
    child = subprocess.Popen(["sh", "-c", "sleep 60; true", marker], start_new_session=True)  # marker appears in its argv
    try:
        assert proc.kill_orphans(marker) >= 1
        child.wait(timeout=5)
        assert child.returncode is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_final_sdf_record_round_trips_through_rdkit_with_separate_labelled_metrics():
    from rdkit import Chem
    from backend.campaign import results
    mol = type("M", (), {"display_id": "MDP-T-0001", "smiles": "CC(=O)Oc1ccccc1C(=O)O", "source": "zinc20", "external_id": "ZINC1"})()
    by = {"docking": {"engine": "vina", "score": -6.1, "score_unit": "kcal/mol"}, "sa": {"sa_score": 1.6, "molecular_weight": 180.2},
          "hyde": {"cumulative_kj_mol": -12.3, "affinity_lower_nm": 5.0, "affinity_upper_nm": 50.0},
          "affinity": {"kd_nm": 9.9}}
    import tempfile, pathlib
    text = results.sdf_record(pathlib.Path(tempfile.gettempdir()), mol, by, 1) * 2
    sup = Chem.SDMolSupplier(); sup.SetData(text)
    mols = [m for m in sup]
    assert len(mols) == 2 and all(m is not None for m in mols)
    p = mols[0].GetPropsAsDict()
    assert p["DOCKING_SCORE"] == -6.1 and p["HYDE_CUMULATIVE_KJ_MOL"] == -12.3 and p["SA_SCORE"] == 1.6
    assert "AFFINITY_KD_NM_MOCKED" in p and "AFFINITY_KD_NM" not in p, "placeholder values must be labelled in the file"
    assert "NOT comparable" in p["PROVENANCE"]


# ------------------------------------------------------------------ FastGrow ---
def test_fragments_keep_only_single_attachment_pieces_of_sensible_size():
    from backend.science.engines import fastgrow as fg
    frags = fg.fragments_from_smiles(["CC(=O)Nc1ccc(O)cc1", "CC(C)Cc1ccc(cc1)C(C)C(=O)O"])
    assert frags and all(f.count("[R]") == 1 for f in frags)
    from rdkit import Chem
    assert all(2 <= Chem.MolFromSmiles(f.replace("[R]", "*")).GetNumHeavyAtoms() - 1 <= 12 for f in frags)


def test_exit_vectors_need_room_and_pocket_context_and_become_the_linker(tmp_path):
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from backend.science.engines import fastgrow as fg
    mol = Chem.AddHs(Chem.MolFromSmiles("c1ccccc1"))
    AllChem.EmbedMolecule(mol, randomSeed=1)
    conf = mol.GetConformer()
    # a synthetic "wall" of protein atoms 3.5 A above the ring plane only on one side
    lines = []
    for i in range(40):
        x, y = (i % 8 - 3.5) * 1.6, (i // 8 - 2) * 1.6
        lines.append(f"ATOM  {i+1:5d}  C   ALA A{i+1:4d}    {x:8.3f}{y:8.3f}{3.5:8.3f}  1.00  0.00           C")
    pdb = tmp_path / "wall.pdb"
    pdb.write_text("\n".join(lines) + "\n")
    evs = fg.choose_exit_vectors(mol, pdb, n=2)
    assert len(evs) <= 2 and all(e.nearest_A >= 2.8 and e.contacts_6A >= 8 for e in evs)
    if evs:
        core = fg.core_with_linker(mol, evs[0])
        assert core.GetAtomWithIdx(evs[0].h_index).GetAtomicNum() == 0
        assert core.GetNumAtoms() == mol.GetNumAtoms()


# --------------------------------------------- HYDE components / library cache ---
def test_hyde_components_follow_the_company_extractor_rules():
    """Rules ported from HYDE_SCORER.html: skip '#' lines, need >= 6 fields, bad numbers count as 0."""
    block = ("# Atom-ID; Total; Rec-Desolv; Rec-IA; Lig-Desolv; Lig-IA\n"
             "1;-1.0;2.0;-3.0;4.0;-5.0\n2;0.5;x;0;0;1\nshort;1;2\n\n")
    c = hyde.atom_score_components(block)
    assert c == {"total": -0.5, "rec_desolv": 2.0, "rec_ia": -3.0, "lig_desolv": 4.0, "lig_ia": -4.0}
    assert hyde.atom_score_components("# only a header\n") is None


def test_hyde_parse_exposes_all_extractor_properties():
    rec = record("m", {hyde.TAG_ATOMS: ATOMS, hyde.TAG_LOWER: "1", hyde.TAG_UPPER: "9",
                       "BIOSOLVEIT.LOGP": "1.5", "BIOSOLVEIT.TPSA": "70.2", "BIOSOLVEIT.MOLECULAR_WEIGHT": "300.1",
                       "BIOSOLVEIT.NUMBER_OF_UNFAVORABLE_TORSIONS": "2",
                       "BIOSOLVEIT.INTRA_CLASH range: 0=green, 1=yellow, 2=red": "1"})
    s = hyde.parse_hyde_output(rec)["m"]
    assert (s.clogp, s.tpsa, s.molecular_weight, s.unfavorable_torsions, s.intra_clash) == (1.5, 70.2, 300.1, 2, 1)
    assert s.components_kj_mol["total"] == s.cumulative_kj_mol == -3.0


def test_zinc_cache_status_reads_only_the_disk(tmp_path, monkeypatch):
    import dataclasses
    from backend.science import library as lib
    (tmp_path / "zinc20_chunk4_3000000.smi").write_text("CCO ZINC1\nCCN ZINC2\n")
    (tmp_path / "unrelated.txt").write_text("x")
    monkeypatch.setattr(lib, "settings", dataclasses.replace(lib.settings, zinc_cache_dir=tmp_path))
    st = lib.zinc_cache_status()
    assert st["cached_molecules"] == 2 and st["cached"][0]["chunk"] == 4 and st["chunks_total"] == 20


def test_same_seed_gives_the_same_sample_and_a_different_seed_a_different_one(tmp_path, monkeypatch):
    import dataclasses
    from backend.science import library as lib
    lines = "".join(f"{s} ZINC{i}_1\n" for i, s in enumerate(
        ["CC(=O)Nc1ccc(O)cc1", "CC(C)Cc1ccc(cc1)C(C)C(=O)O", "COc1ccc(CCN)cc1", "OCCN1CCOCC1", "CN1CCN(CC1)c1ccccc1"] * 200
        + [f"C{'C' * (k % 9)}c1ccccc1O{'C' * (k % 5)}N" for k in range(400)]))
    monkeypatch.setattr(lib, "settings", dataclasses.replace(lib.settings, zinc_cache_dir=tmp_path))
    for chunk in range(1, lib.ZINC_CHUNKS + 1):
        (tmp_path / f"zinc20_chunk{chunk}_{lib.zinc_prefix_bytes()}.smi").write_text(lines)
    a, _ = lib.fetch_zinc20(10, 7, lib.Filters())
    b, _ = lib.fetch_zinc20(10, 7, lib.Filters())
    c, _ = lib.fetch_zinc20(10, 8, lib.Filters())
    ids = lambda r: [m["external_id"] for m in r]  # noqa: E731
    assert ids(a) == ids(b)
    assert ids(a) != ids(c)


# ------------------------------------------------------------------ ChemAIRS SAScore ---
def test_chemairs_create_payload_uses_documented_fields_and_defaults():
    from backend.science import chemairs
    body = chemairs.build_create_payload(["CCO", "c1ccccc1"])
    assert body["smiles"] == ["CCO", "c1ccccc1"]
    assert body["useELNData"] is False and body["mode"] in (2, 4) and body["materialSource"] in (0, 1)
    assert "callbackUrl" not in body
    assert chemairs.build_create_payload(["CCO"], callback_url="https://x/cb")["callbackUrl"] == "https://x/cb"
    with pytest.raises(ValueError):
        chemairs.build_create_payload([])


def test_chemairs_parses_the_documented_results_page_shape():
    """Payload shape taken verbatim from APIInterfaceDocumentationEN.pdf section 1.2."""
    from backend.science import chemairs
    payload = {
        "totalCount": 2,
        "progress": "2 / 2",
        "searchResponseDtos": [
            {"remark": "Available for purchase", "isError": False, "code": "1", "smiles": "c1ccccc2[nH]1",
             "saScore": "0", "feasibility": "N/A", "feasibilityScore": "0", "status": 2},
            {"remark": "some problem", "isError": True, "code": "2", "smiles": "bad",
             "saScore": None, "status": 500},
        ],
    }
    total, progress, rows = chemairs.parse_results_page(payload)
    assert total == 2 and progress == "2 / 2" and len(rows) == 2
    assert rows[0].row_number == 1 and rows[0].is_error is False and rows[0].sa_score == 0.0
    assert rows[1].row_number == 2 and rows[1].is_error is True and rows[1].sa_score is None


def test_chemairs_score_batch_rejects_when_not_configured(monkeypatch):
    from backend.science import chemairs
    import dataclasses
    monkeypatch.setattr(chemairs, "settings", dataclasses.replace(chemairs.settings, chemairs_api_key=None))
    with pytest.raises(chemairs.ChemAIRSError):
        chemairs.create_task(["CCO"])


def test_sa_stage_falls_back_to_rdkit_when_chemairs_not_configured():
    from backend.stages import sa_scoring
    assert sa_scoring.active_method() == sa_scoring.RDKIT_METHOD  # no key set in the test environment
    out = sa_scoring.score_batch_for_stage(["CCO", "not-a-smiles(("])
    assert out[0]["method"] == sa_scoring.RDKIT_METHOD and 1 <= out[0]["sa_score"] <= 10
    assert "error" in out[1]


def test_sa_stage_batch_is_duplicate_smiles_safe():
    """Same class of bug fixed in backend/science/admet.py: results must correlate by
    position, never by SMILES value, since a batch can contain the same SMILES twice."""
    from backend.stages import sa_scoring
    out = sa_scoring.score_batch_for_stage(["CCO", "c1ccccc1", "CCO"])
    assert len(out) == 3
    assert out[0]["sa_score"] == out[2]["sa_score"]  # same molecule, same score, but still two entries
    assert out[0] is not out[2]


# ------------------------------------------------------------------ library filters ---
def test_filters_reactivity_levels_are_progressively_stricter():
    from backend.science.library import Filters, standardize
    base = dict(min_heavy=1, max_heavy=100, min_mw=1, max_mw=1000, min_logp=-10, max_logp=10,
                max_hbd=20, max_hba=20, max_tpsa=500, max_rotatable=20)
    quinone = "O=C1C=CC(=O)c2ccccc12"  # a real Michael-acceptor / PAINS-pattern molecule
    assert standardize(quinone, Filters(reactivity="mild", **base))["smiles"]
    with pytest.raises(ValueError, match="reactive group"):
        standardize(quinone, Filters(reactivity="clean", **base))
    with pytest.raises(ValueError, match="reactive group"):
        standardize(quinone, Filters(reactivity="anodyne", **base))
    plain = "c1ccc(cc1)C(=O)Nc1ccccc1"
    for level in ("mild", "clean", "anodyne"):
        assert standardize(plain, Filters(reactivity=level, **base))["smiles"]


def test_filters_reject_invalid_reactivity_level():
    from backend.science.library import Filters
    with pytest.raises(ValueError):
        Filters(reactivity="not-a-real-level")


def test_filters_logp_hbd_hba_tpsa_are_enforced():
    from backend.science.library import Filters, standardize
    base = dict(min_heavy=1, max_heavy=100, min_mw=1, max_mw=1000, max_rotatable=20)
    sucrose = "OC[C@H]1O[C@@](CO)(O[C@@H]2O[C@H](CO)[C@@H](O)[C@H](O)[C@H]2O)[C@@H](O)[C@@H]1O"  # 8 HBD, 11 HBA
    with pytest.raises(ValueError, match="H-bond donors"):
        standardize(sucrose, Filters(min_logp=-10, max_logp=10, max_hbd=5, max_hba=20, max_tpsa=500, **base))
    with pytest.raises(ValueError, match="H-bond acceptors"):
        standardize(sucrose, Filters(min_logp=-10, max_logp=10, max_hbd=20, max_hba=5, max_tpsa=500, **base))
    with pytest.raises(ValueError, match="TPSA"):
        standardize(sucrose, Filters(min_logp=-10, max_logp=10, max_hbd=20, max_hba=20, max_tpsa=10, **base))
    with pytest.raises(ValueError, match="logP"):
        standardize("CCCCCCCCCCCCCCCCCCCCCC", Filters(min_logp=-10, max_logp=0, max_hbd=20, max_hba=20, max_tpsa=500, **base))


def test_filters_from_params_matches_campaign_defaults():
    from backend.campaign import params as P
    from backend.science.library import Filters
    p = P.resolve({"engine": "vina", "site": {}})
    f = Filters.from_params(p)
    assert (f.min_heavy, f.max_heavy, f.reactivity) == (10, 40, "mild")
    assert f.describe()  # does not raise, mentions the honesty disclaimer
    assert "purchasability" in f.describe().lower()


def test_params_validates_filter_bounds_and_reactivity():
    from backend.campaign import params as P
    p = P.resolve({"engine": "vina", "site": {}, "filter_min_heavy": 50, "filter_max_heavy": 5})
    assert p["filter_max_heavy"] == 50  # clamped up to not be below min, never silently inverted-wrong
    with pytest.raises(P.ParamError):
        P.resolve({"engine": "vina", "site": {}, "filter_reactivity": "bogus"})
