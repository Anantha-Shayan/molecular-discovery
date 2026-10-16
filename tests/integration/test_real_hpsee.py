"""
Tests against a REAL BioSolveIT HPSee server. Skipped unless HPSEE_URL,
HPSEE_USERNAME and HPSEE_PASSWORD are set (environment or .env.local).

    pytest -m real tests/integration/test_real_hpsee.py -v

The docking test submits a real 2-molecule FlexX workflow followed by HYDE
scoring (about 2 minutes on a single-node HPSee) and checks the result format
this application relies on. Chemical Space Docking is not exercised here: it
needs a .space file on the server and SeeSAR-exported definition files.
"""
from __future__ import annotations

import time

import pytest
from rdkit import Chem

from backend.config import settings
from backend.science import receptor as recep
from backend.science import site as sitelib
from backend.science.engines import hpsee as H
from backend.science.engines.common import embed_3d

pytestmark = pytest.mark.real


@pytest.fixture(scope="module")
def client():
    if not settings.hpsee_configured:
        pytest.skip("HPSEE_URL / HPSEE_USERNAME / HPSEE_PASSWORD not set")
    return H.HPSeeClient()


def _wait(client, wid, limit=1200):
    end = time.time() + limit
    while time.time() < end:
        info = client.workflow(wid)
        if info["status"] in H.TERMINAL:
            return info
        time.sleep(5)
    client.cancel_workflow(wid)
    pytest.fail(f"workflow {wid} did not finish in {limit}s")


def test_connection_login_and_licences(client):
    st = H.status(refresh=True)
    assert st.reachable and st.authenticated, st.reason
    assert st.version
    print("HPSee", st.version, {k: v for k, v in st.licenses.items()})


def test_real_docking_then_hyde_scoring_produce_the_tags_we_parse(client, tmp_path):
    st = H.status(refresh=True)
    if not (st.docking_ready and st.scoring_ready):
        pytest.skip(f"Docking/Scoring not licensed on HPSee: {st.reason}")
    from backend.config import APP_ROOT
    pdb = APP_ROOT / "data" / "fixtures" / "demo_kras_g12d_7rpz.pdb"
    ref = sitelib.find_site(pdb, "6IC:A:202")
    recep.write_chain_pdb(pdb, tmp_path / "protein.pdb", "A", ref)
    recep.write_reference_ligand(pdb, tmp_path / "ref_ligand.sdf", ref)
    w = Chem.SDWriter(str(tmp_path / "lib.sdf"))
    for i, smi in enumerate(["CC(=O)Oc1ccccc1C(=O)O", "Cn1cnc2c1c(=O)n(C)c(=O)n2C"], start=1):
        m = embed_3d(smi)
        m.SetProp("_Name", f"MDP-REAL-{i:04d}")
        w.write(m)
    w.close()
    info = _wait(client, client.start_docking(tmp_path / "protein.pdb", tmp_path / "ref_ligand.sdf", tmp_path / "lib.sdf",
                                              description="MDP integration test (safe to delete)")["workflowId"])
    assert info["status"] == H.SUCCESSFUL, info
    lib = info["result"]["libraryId"]
    assert H.result_sorting(client.library(lib)["properties"]) == "Properties.BIOSOLVEIT.DOCKING_SCORE:number asc"
    entries = [H.parse_entry(e["entryId"], e["entry"]) for e in client.library_entries(lib, limit=100,
                                                                                   sorting=H.result_sorting([{"name": H.TAG_DOCKING_SCORE}]))]
    assert entries and all(e.docking_score is not None and e.complete for e in entries)
    assert [e.docking_score for e in entries] == sorted(e.docking_score for e in entries), "server-side sort"
    assert {e.name.rsplit("_", 1)[0] for e in entries} == {"MDP-REAL-0001", "MDP-REAL-0002"}, "poses titled <name>_NN"
    (tmp_path / "poses.sdf").write_text("".join(e.record for e in entries))
    info = _wait(client, client.start_scoring(tmp_path / "protein.pdb", tmp_path / "ref_ligand.sdf", tmp_path / "poses.sdf",
                                              description="MDP integration test (safe to delete)")["workflowId"])
    assert info["status"] == H.SUCCESSFUL, info
    scored = [H.parse_entry(e["entryId"], e["entry"]) for e in client.library_entries(info["result"]["libraryId"], limit=100)]
    assert scored and all(e.hyde and e.hyde.affinity_lower_nm and e.hyde.affinity_upper_nm for e in scored)
    assert all(e.hyde.cumulative_kj_mol is None for e in scored), \
        "HPSee started writing per-atom HYDE scores: switch HPSee campaigns back to the cumulative score"
