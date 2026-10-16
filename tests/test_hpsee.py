"""
BioSolveIT HPSee integration: client details, result parsing on REAL HPSee
output (tests/fixtures/hpsee_*.json, recorded from a licensed HPSee 2.4.0), and
the campaign workflows - library docking and Chemical Space Docking with its
anchor review - against a programmable fake of the HPSee REST API.

The fake replaces only the HTTP client; the runner, database, API and parsers are
the real ones. Records the fake returns use the tag names and title format
observed on the real server. Nothing here claims scientific validity; the real
server is exercised by tests/integration/test_real_hpsee.py (`-m real`).
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import os
import threading
import time

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem
from sqlalchemy import select

from backend import models
from backend.campaign import hpsee_flow
from backend.campaign import runner as rn
from backend.database import get_session
from backend.science.engines import hpsee as H
from backend.tools import registry

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


# --------------------------------------------------------------- parsing ---
def test_real_docking_entries_parse_with_verified_tags():
    entries = [H.parse_entry(e["entryId"], e["entry"]) for e in _fixture("hpsee_docking_entries.json")]
    assert [e.name for e in entries] == ["MDP-TEST-0001_01", "MDP-TEST-0001_02", "MDP-TEST-0002_01", "MDP-TEST-0002_02"]
    assert all(e.complete and e.n_linkers == 0 and e.smiles for e in entries)
    assert entries[0].docking_score == -16.79 and entries[0].hyde is None


def test_real_scoring_entries_carry_hyde_range_but_no_atom_scores():
    entries = [H.parse_entry(e["entryId"], e["entry"]) for e in _fixture("hpsee_scoring_entries.json")]
    h = entries[0].hyde
    assert h is not None and h.cumulative_kj_mol is None, "HPSee writes no BIOSOLVEIT.HYDE_ATOM_SCORES"
    assert h.affinity_lower_nm == pytest.approx(47262698.827879) and h.inter_clash == 0
    best = sorted(entries, key=H.Entry.sort_key)[0]
    assert best.name == "MDP-TEST-0002_02", "best = smallest HYDE affinity lower bound"


def test_hyde_parser_picks_best_pose_by_affinity_when_no_atom_scores():
    """Two real HPSee-scored poses of one molecule, given the same name as the HYDE stage does."""
    from backend.science.engines.hyde import parse_hyde_output
    recs = _fixture("hpsee_scoring_entries.json")[:2]          # MDP-TEST-0001_01 and _02
    text = "".join("MDP-X" + r["entry"][r["entry"].index("\n"):] for r in recs)
    best = parse_hyde_output(text)
    assert list(best) == ["MDP-X"]
    assert best["MDP-X"].affinity_lower_nm == pytest.approx(2480015.498572), "pose _02 has the better HYDE range"


def test_flexx_parser_groups_real_hpsee_pose_names():
    from backend.science.engines.flexx import parse_flexx_output
    text = "".join(e["entry"] for e in _fixture("hpsee_docking_entries.json"))
    out = parse_flexx_output(text, {"MDP-TEST-0001": "a", "MDP-TEST-0002": "b"}, "HPSee")
    assert out["a"].score == -16.79 and out["a"].n_poses == 2 and out["b"].score == -17.21


def test_link_atoms_mark_incomplete_synthons():
    rec = _record("[U]c1ccccc1C(=O)N", "SYN_1", score=-9.0, link=1)
    e = H.parse_entry(7, rec)
    assert e.n_linkers == 1 and not e.complete


def test_workflow_and_error_summaries():
    info = _fixture("hpsee_workflow_docking_successful.json")
    tasks = H.summarise_tasks(info)
    assert [t["type"] for t in tasks] == ["Docking", "Annotation"] and all(t["progress"] == 100 for t in tasks)
    text, retryable = H.describe_errors([{"category": 3, "categoryDescription": "Process crashed", "messages": "segfault"}])
    assert "Process crashed" in text and retryable
    _, retryable = H.describe_errors([{"category": 5, "categoryDescription": "License error"}])
    assert not retryable


def test_error_bodies_and_multipart():
    assert H._message(b'"Create Docking task failed.\\n Invalid License"').startswith("Create Docking")
    assert "bad" in H._message(b'{"title": "bad", "errors": {"x": ["y"]}}')
    body, ctype = H._multipart([("A", "1"), ("F", ("f.sdf", b"DATA"))])
    assert ctype.startswith("multipart/form-data; boundary=") and b'filename="f.sdf"' in body and b"DATA" in body


def test_not_configured_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(H, "settings", dataclasses.replace(H.settings, hpsee_url=None))
    st = H._probe()
    assert not st.configured and not st.public()["available"] and "Not configured" in st.reason
    with pytest.raises(H.HPSeeError) as exc:
        H.HPSeeClient()
    assert exc.value.category == "not_configured"


def test_job_creation_refused_when_hpsee_is_not_configured(client, monkeypatch):
    monkeypatch.setattr(H, "status", lambda refresh=False: H.HPSeeStatus(False, reason="Not configured: set HPSEE_URL"))
    tid = client.post("/api/targets/demo").json()["target_id"]
    ref = client.get(f"/api/targets/{tid}/sites").json()["sites"][0]["key"]
    r = client.post("/api/jobs", json={"target_id": tid, "engine": "hpsee", "site": {"mode": "ligand", "reference": ref},
                                       "library_source": "demo"})
    assert r.status_code == 422 and "configured" in r.json()["detail"]
    r = client.post("/api/jobs", json={"target_id": tid, "engine": "flexx", "site": {"mode": "ligand", "reference": ref}})
    assert r.status_code == 422, "local FlexX is no longer offered for new campaigns"
    assert client.get("/api/tools").json()["services"]["hpsee"]["configured"] is False


# ------------------------------------------------------------ fake server ---
def _record(smiles: str, name: str, *, score: float | None, link: int = 0, hyde_lo: float | None = None) -> str:
    mol = Chem.MolFromSmiles(smiles)
    AllChem.Compute2DCoords(mol)
    mol.SetProp("_Name", name)
    block = Chem.MolToMolBlock(mol)
    tags = {"BIOSOLVEIT.NUMBER_OF_LINK_ATOMS": link, "BIOSOLVEIT.USMILES": smiles}
    if score is not None:
        tags["BIOSOLVEIT.DOCKING_SCORE"] = score
    if hyde_lo is not None:
        tags.update({"BIOSOLVEIT.HYDE_ESTIMATED_AFFINITY_LOWER_BOUNDARY [nM]": hyde_lo,
                     "BIOSOLVEIT.HYDE_ESTIMATED_AFFINITY_UPPER_BOUNDARY [nM]": hyde_lo * 100,
                     "BIOSOLVEIT.HYDE_LIGAND_EFFICIENCY": 0.3,
                     "BIOSOLVEIT.INTER_CLASH range: 0=green, 1=yellow, 2=red": 0})
    return block + "".join(f">  <{k}>\n{v}\n\n" for k, v in tags.items()) + "$$$$\n"


ANCHORS = [("[U]c1ccc(F)cc1", -12.0, 900.0), ("[U]C1CCN(C)CC1", -10.0, 5000.0), ("[U]c1cccnc1", -8.0, None)]
PRODUCTS = ["O=C(Nc1ccc(F)cc1)c1ccccc1", "CC(=O)Nc1ccc(F)cc1", "CN1CCC(NC(=O)c2ccccc2)CC1"]


class FakeHPSee:
    """In-memory HPSee: workflows finish after `polls` status calls."""

    def __init__(self):
        self.ids = itertools.count(1000)
        self.workflows: dict[str, dict] = {}
        self.libraries: dict[str, list[tuple[int, str]]] = {}
        self.csds: dict[str, dict] = {}
        self.submitted: list[tuple[str, str]] = []   # (kind, description)
        self.cancelled: list[str] = []
        self.polls = 2
        self.fail_next: str | None = None            # kind whose next workflow fails
        self.hold = threading.Event()
        self.hold.set()
        self.extensions: list[list[int]] = []
        self.ext_open = False                        # extension results still carry link atoms (3-component space)

    # helpers
    def _wf(self, kind: str, description: str, records: list[str], task: str) -> dict:
        wid = f"wf-{next(self.ids)}"
        lib = f"lib-{next(self.ids)}"
        self.libraries[lib] = [(next(self.ids), r) for r in records]
        fail = self.fail_next == kind
        self.fail_next = None if fail else self.fail_next
        self.workflows[wid] = {"workflowId": wid, "description": description, "left": self.polls, "fail": fail,
                               "lib": lib, "task": task, "deleted": False}
        self.submitted.append((kind, description))
        return self.workflow(wid, count=False)

    def workflow(self, wid, count=True):
        w = self.workflows[wid]
        if count and self.hold.is_set() and w["left"] > 0:
            w["left"] -= 1
        done = w["left"] == 0
        status = (H.FAILED if w["fail"] else H.SUCCESSFUL) if done else H.RUNNING
        prog = 100 if done else 0
        return {"workflowId": wid, "status": status, "statusDescription": H.STATUS_TEXT[status],
                "description": w["description"],
                "result": {"libraryId": w["lib"] if done and not w["fail"] else None,
                           "librarySize": len(self.libraries[w["lib"]]) if done and not w["fail"] else 0},
                "taskInfos": [{"type": w["task"], "status": status, "statusDescription": H.STATUS_TEXT[status],
                               "progress": prog, "result": {}}]}

    # API surface used by the app
    def recent_workflows(self, limit=200):
        return [{"workflowId": k, "description": v["description"]} for k, v in self.workflows.items()]

    def workflow_errors(self, wid, limit=50):
        return [{"category": 3, "categoryDescription": "Process crashed", "messages": "simulated worker crash"}]

    def retry_workflow(self, wid):
        w = self.workflows[wid]
        w["fail"], w["left"] = False, self.polls

    def cancel_workflow(self, wid):
        self.cancelled.append(wid)
        self.workflows[wid]["deleted"] = True

    def library(self, lib):
        return {"properties": [{"name": "BIOSOLVEIT.DOCKING_SCORE", "type": 4}]}

    def library_entries(self, lib, *, limit, sorting=""):
        rows = self.libraries[lib]
        if sorting:
            rows = sorted(rows, key=lambda r: H.parse_entry(*r).docking_score or 0)
        for eid, rec in rows[:limit]:
            yield {"entryId": eid, "entry": rec}

    def start_docking(self, protein, ref, lib, *, description):
        recs = []
        for i, m in enumerate(Chem.SDMolSupplier(str(lib))):
            name = m.GetProp("_Name")
            for pose in (1, 2):
                recs.append(_record(Chem.MolToSmiles(m), f"{name}_{pose:02d}", score=-10.0 - i - pose))
        return self._wf("docking", description, recs, "Docking")

    def start_scoring(self, protein, ref, poses, *, description):
        recs = []
        for i, m in enumerate(Chem.SDMolSupplier(str(poses))):
            recs.append(_record(Chem.MolToSmiles(m), m.GetProp("_Name"), score=float(m.GetProp("FLEXX_DOCKING_SCORE")),
                                hyde_lo=1000.0 * (i + 1)))
        return self._wf("scoring", description, recs, "Scoring")

    def create_csd(self, space_id, description):
        cid = f"csd-{next(self.ids)}"
        self.csds[cid] = {"chemicalSpaceDockingId": cid, "description": description, "anchoringWorkflow": None,
                          "extensionWorkflows": []}
        return self.csds[cid]

    def recent_csds(self, limit=400):
        return list(self.csds.values())

    def csd(self, cid):
        return self.csds[cid]

    def start_anchoring(self, cid, flexx, hyde, *, description, filters=""):
        info = self._wf("anchoring", description, [_record(s, f"anchor_{i}", score=sc, link=1, hyde_lo=h)
                                                   for i, (s, sc, h) in enumerate(ANCHORS)], "Docking")
        self.csds[cid]["anchoringWorkflow"] = info["workflowId"]
        return info

    def start_extension(self, cid, ids, flexx, hyde, *, description, filters=""):
        self.extensions.append(list(ids))
        link = 1 if self.ext_open else 0
        recs = [_record(s if not link else s.replace("c1ccccc1", "c1ccc([U])cc1", 1), f"product_{i}", score=-15.0 - i,
                        link=link, hyde_lo=100.0 * (i + 1)) for i, s in enumerate(PRODUCTS)]
        recs.append(_record(PRODUCTS[0], "product_0_dup", score=-9.0, link=link, hyde_lo=9999.0))  # same molecule, worse pose
        info = self._wf("extension", description, recs, "Docking")
        self.csds[cid]["extensionWorkflows"].append(info["workflowId"])
        return info


@pytest.fixture
def hpsee(monkeypatch, tmp_path):
    fake = FakeHPSee()
    monkeypatch.setattr(hpsee_flow, "_client", lambda: fake)
    ready = H.HPSeeStatus(True, reachable=True, authenticated=True, version="2.4.0",
                          licenses={w: {"licensed": True, "expires": "2026-11-15"} for w in
                                    (H.WF_DOCKING, H.WF_SCORING, H.WF_ANCHORING, H.WF_EXTENSION)})
    monkeypatch.setattr(H, "status", lambda refresh=False: ready)
    monkeypatch.setattr(hpsee_flow, "settings", dataclasses.replace(hpsee_flow.settings, hpsee_poll_interval_seconds=0.02))
    monkeypatch.setattr(H.HPSeeClient, "chemical_space", lambda self, sid: {
        "spaceId": sid, "spaceName": "TestSpace", "preparationWorkflowInfo": {"status": H.SUCCESSFUL}})
    monkeypatch.setattr(H.HPSeeClient, "__init__", lambda self, *a, **k: None)
    real = registry.tool_statuses()
    monkeypatch.setattr(registry, "tool_statuses", lambda refresh=False: {
        k: dataclasses.replace(v, available=True, license_ok=True) for k, v in real.items()})
    monkeypatch.setattr(rn, "_stage_prepare", _fake_prepare)
    return fake


def _fake_prepare(job_id):
    from backend import storage
    from backend.campaign import params as P
    with get_session() as s:
        p = P.resolve(s.get(models.Job, job_id).params)
    prep = storage.ensure_dir("runs", job_id, "prepare")
    for f in ("protein.pdb", "ref_ligand.sdf"):
        (prep / f).write_text("fake\n")
    if P.is_csd(p):
        a, b = hpsee_flow._csd_files(job_id)
        a.write_text("flexx definition"), b.write_text("hydescorer definition")
    rn._set_stage(job_id, "prepare", "succeeded", finish=True, counts=(1, 1, 0), detail="fake")
    return "ok"


def _target(client):
    tid = client.post("/api/targets/demo").json()["target_id"]
    return tid, client.get(f"/api/targets/{tid}/sites").json()["sites"][0]["key"]


def _definitions(client):
    a = client.post("/api/hpsee/definitions", data={"kind": "flexx"}, files={"file": ("pocket.flexx", b"FLEXX")}).json()
    b = client.post("/api/hpsee/definitions", data={"kind": "hydescorer"},
                    files={"file": ("pocket.hydescorer", b"HYDE")}).json()
    return a["definition_id"], b["definition_id"]


def _start_csd(client, **over):
    tid, ref = _target(client)
    dock, score = _definitions(client)
    body = {"target_id": tid, "engine": "hpsee", "hpsee_mode": "chemical_space", "hpsee_space_id": "space-1",
            "hpsee_docking_definition": dock, "hpsee_scoring_definition": score,
            "site": {"mode": "ligand", "reference": ref}, "enable_admet": False, "sa_threshold": 10.0}
    body.update(over)
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 201, r.text
    return r.json()["job_id"]


def wait(client, job_id, statuses=("COMPLETE", "FAILED", "CANCELLED", "AWAITING_REVIEW"), timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        d = client.get(f"/api/jobs/{job_id}").json()
        if d["status"] in statuses:
            return d
        time.sleep(0.05)
    raise AssertionError(f"campaign did not reach {statuses}: {d['status']} at {d['current_stage']}")


def _review(client, job):
    h = client.get(f"/api/jobs/{job}/hpsee").json()
    assert h["review"], h
    entries = client.get(f"/api/jobs/{job}/hpsee/runs/{h['review']['run_id']}/entries").json()
    return h["review"], entries["items"]


# ------------------------------------------------------------ library mode ---
def test_library_mode_docks_and_scores_on_hpsee(client, hpsee):
    tid, ref = _target(client)
    r = client.post("/api/jobs", json={"target_id": tid, "engine": "hpsee", "hpsee_mode": "library",
                                       "site": {"mode": "ligand", "reference": ref}, "library_source": "demo",
                                       "library_count": 6, "enable_admet": False, "dock_keep_fraction": 1.0,
                                       "sa_threshold": 10.0})
    assert r.status_code == 201, r.text
    job = r.json()["job_id"]
    d = wait(client, job)
    assert d["status"] == "COMPLETE", d["failure_reason"]
    st = {s["key"]: s for s in d["stages"]}
    assert "HPSee" in st["docking"]["tool"] and "HPSee" in st["hyde"]["tool"]
    assert st["docking"]["batches"]["total"] == 1, "one HPSee workflow per batch"
    assert [k for k, _ in hpsee.submitted] == ["docking", "scoring"]
    assert all(f"[mdp:" in desc for _, desc in hpsee.submitted), "workflows carry the recovery marker"
    c = client.get(f"/api/jobs/{job}/candidates").json()
    assert "affinity, lower bound" in c["rank_policy"]
    lows = [x["hyde_affinity_lower_nm"] for x in c["candidates"]]
    assert lows == sorted(lows) and c["candidates"][0]["docking_unit"] == "FlexX score"
    runs = client.get(f"/api/jobs/{job}/hpsee").json()["runs"]
    assert {r["status"] for r in runs} == {"succeeded"} and all(r["tasks"][0]["progress"] == 100 for r in runs)


def test_library_mode_failed_workflow_is_retried_then_reported(client, hpsee, monkeypatch):
    monkeypatch.setattr(rn, "settings", dataclasses.replace(rn.settings, max_batch_attempts=1))
    hpsee.fail_next = "docking"
    tid, ref = _target(client)
    job = client.post("/api/jobs", json={"target_id": tid, "engine": "hpsee", "site": {"mode": "ligand", "reference": ref},
                                         "library_source": "demo", "library_count": 4, "enable_admet": False}).json()["job_id"]
    d = wait(client, job)
    assert d["status"] == "FAILED"
    b = client.get(f"/api/jobs/{job}/batches", params={"stage": "docking"}).json()["batches"][0]
    assert "simulated worker crash" in b["error"] and "HPSee workflow" in b["error"]
    run = client.get(f"/api/jobs/{job}/hpsee").json()["runs"][0]
    assert run["status"] == "failed" and run["retryable"]
    assert client.post(f"/api/jobs/{job}/retry", json={"batch_ids": [b["batch_id"]]}).status_code == 200
    assert wait(client, job)["status"] == "COMPLETE"


def test_restart_reattaches_to_a_running_workflow_instead_of_resubmitting(client, hpsee):
    hpsee.hold.clear()   # workflows never progress until released
    tid, ref = _target(client)
    job = client.post("/api/jobs", json={"target_id": tid, "engine": "hpsee", "site": {"mode": "ligand", "reference": ref},
                                         "library_source": "demo", "library_count": 4, "enable_admet": False,
                                         "enable_hyde": False, "rank_by": "docking"}).json()["job_id"]
    end = time.monotonic() + 20
    while not hpsee.submitted and time.monotonic() < end:
        time.sleep(0.05)
    # Simulate a crash before the workflow id was saved: forget it locally, keep it on "HPSee".
    with get_session() as s:
        run = s.execute(select(models.HPSeeRun).where(models.HPSeeRun.job_id == job)).scalar_one()
        run.workflow_id, run.status = None, "submitting"
    rn.runner.stop()
    end = time.monotonic() + 20
    while rn.runner._active and time.monotonic() < end:   # let the interrupted campaign thread wind down
        time.sleep(0.05)
    rn.recover_interrupted()
    rn.runner.start()
    hpsee.hold.set()
    d = wait(client, job)
    assert d["status"] == "COMPLETE", d["failure_reason"]
    assert [k for k, _ in hpsee.submitted] == ["docking"], "found again by its marker; never submitted twice"


# ------------------------------------------------------- chemical space ---
def test_csd_pauses_for_review_and_extends_only_confirmed_anchors(client, hpsee):
    job = _start_csd(client)
    d = wait(client, job)
    assert d["status"] == "AWAITING_REVIEW" and d["awaiting_review"]
    st = {s["key"]: s for s in d["stages"]}
    assert [s["key"] for s in d["stages"]][:3] == ["prepare", "csd_anchoring", "csd_extension"]
    assert "library" not in st and "docking" not in st, "no enumerated library in chemical-space mode"
    assert st["csd_extension"]["status"] == "awaiting_review" and "Nothing is extended until you confirm" in st["csd_extension"]["detail"]
    assert [k for k, _ in hpsee.submitted] == ["anchoring"], "extension never starts on its own"
    review, items = _review(client, job)
    assert review["kind"] == "anchoring" and len(items) == 3
    assert [i["name"] for i in items] == ["anchor_0", "anchor_1", "anchor_2"], "best HYDE first, unscored last"
    assert all(i["open_attachment_points"] == 1 and i["structure_svg"] for i in items)
    url = f"/api/jobs/{job}/hpsee/review"
    chosen = [items[0]["entry_id"], items[1]["entry_id"]]
    # Explicit confirmation and real entry ids are required.
    assert client.post(url, json={"run_id": review["run_id"], "entry_ids": chosen}).status_code == 409
    assert client.post(url, json={"run_id": review["run_id"], "entry_ids": [1], "confirm": True}).status_code == 409
    assert client.post(url, json={"run_id": review["run_id"], "action": "finish", "confirm": True}).status_code == 409
    assert client.post(url, json={"run_id": "other", "entry_ids": chosen, "confirm": True}).status_code == 409
    r = client.post(url, json={"run_id": review["run_id"], "entry_ids": chosen, "confirm": True})
    assert r.status_code == 200, r.text
    d = wait(client, job, statuses=("COMPLETE", "FAILED", "CANCELLED"))
    assert d["status"] == "COMPLETE", d["failure_reason"]
    assert hpsee.extensions == [sorted(chosen)], "exactly the confirmed anchors were extended"
    st = {s["key"]: s for s in d["stages"]}
    assert st["csd_extension"]["count_out"] == 3 and "duplicate" in st["csd_extension"]["detail"]
    c = client.get(f"/api/jobs/{job}/candidates").json()["candidates"]
    assert {x["source"] for x in c} == {"hpsee_csd"} and len(c) == 3
    assert c[0]["hyde_affinity_lower_nm"] == 100.0 and c[0]["docking_score"] == -15.0, "best pose of the duplicate kept"
    detail = client.get(f"/api/candidates/{c[0]['molecule_id']}").json()
    assert detail["has_pose"] and detail["stage_payloads"]["docking"]["engine"] == "hpsee"
    assert any(p["stage"] == "csd_extension" and p["has_result"] for p in detail["provenance"])


def test_csd_three_component_space_pauses_again_and_can_be_finished(client, hpsee):
    hpsee.ext_open = True   # extension results still carry attachment points
    job = _start_csd(client)
    wait(client, job)
    review, items = _review(client, job)
    client.post(f"/api/jobs/{job}/hpsee/review", json={"run_id": review["run_id"], "confirm": True,
                                                       "entry_ids": [items[0]["entry_id"]]})
    end = time.monotonic() + 30
    while time.monotonic() < end:   # QUEUED -> RUNNING -> AWAITING_REVIEW again, for round 1's results
        h = client.get(f"/api/jobs/{job}/hpsee").json()
        if h["review"] and h["review"]["kind"] == "extension":
            break
        time.sleep(0.05)
    review2, items2 = _review(client, job)
    assert review2["kind"] == "extension" and review2["round"] == 1 and not review2["can_finish"]
    assert all(i["open_attachment_points"] for i in items2)
    hpsee.ext_open = False
    r = client.post(f"/api/jobs/{job}/hpsee/review", json={"run_id": review2["run_id"], "confirm": True,
                                                           "entry_ids": [items2[0]["entry_id"]]})
    assert r.status_code == 200, r.text
    d = wait(client, job, statuses=("COMPLETE", "FAILED"))
    assert d["status"] == "COMPLETE", d["failure_reason"]
    assert len(hpsee.extensions) == 2


def test_cancel_while_awaiting_review_and_resume_returns_to_review(client, hpsee):
    job = _start_csd(client)
    assert wait(client, job)["status"] == "AWAITING_REVIEW"
    assert client.post(f"/api/jobs/{job}/cancel").status_code == 200
    d = client.get(f"/api/jobs/{job}").json()
    assert d["status"] == "CANCELLED" and not hpsee.cancelled, "nothing was running remotely"
    assert client.post(f"/api/jobs/{job}/resume").status_code == 200
    assert wait(client, job)["status"] == "AWAITING_REVIEW"
    assert [k for k, _ in hpsee.submitted] == ["anchoring"], "anchoring is not repeated"


def test_cancel_during_anchoring_cancels_the_hpsee_workflow(client, hpsee):
    hpsee.hold.clear()
    job = _start_csd(client)
    end = time.monotonic() + 20
    while not hpsee.submitted and time.monotonic() < end:
        time.sleep(0.05)
    time.sleep(0.1)
    assert client.post(f"/api/jobs/{job}/cancel").status_code == 200
    d = wait(client, job, statuses=("CANCELLED",))
    assert d["status"] == "CANCELLED" and len(hpsee.cancelled) == 1
    run = client.get(f"/api/jobs/{job}/hpsee").json()["runs"][0]
    assert run["status"] == "cancelled" and "running on a worker finish" in run["error"]
    hpsee.hold.set()


def test_failed_anchoring_resumes_by_requeueing_on_hpsee(client, hpsee):
    hpsee.fail_next = "anchoring"
    job = _start_csd(client)
    d = wait(client, job)
    assert d["status"] == "FAILED" and "simulated worker crash" in d["failure_reason"]
    assert client.post(f"/api/jobs/{job}/resume").status_code == 200
    assert wait(client, job)["status"] == "AWAITING_REVIEW"
    assert [k for k, _ in hpsee.submitted] == ["anchoring"], "HPSee's own retry was used; nothing resubmitted"


def test_csd_creation_requires_space_and_both_definitions(client, hpsee):
    tid, ref = _target(client)
    dock, score = _definitions(client)
    base = {"target_id": tid, "engine": "hpsee", "hpsee_mode": "chemical_space", "site": {"mode": "ligand", "reference": ref}}
    r = client.post("/api/jobs", json={**base, "hpsee_docking_definition": dock, "hpsee_scoring_definition": score})
    assert r.status_code == 422 and "Chemical Space" in r.json()["detail"]
    r = client.post("/api/jobs", json={**base, "hpsee_space_id": "s", "hpsee_docking_definition": dock})
    assert r.status_code == 422 and "hydescorer" in r.json()["detail"]
    r = client.post("/api/jobs", json={**base, "hpsee_space_id": "s", "hpsee_docking_definition": score,
                                       "hpsee_scoring_definition": dock})
    assert r.status_code == 422, "definition kinds are checked"
    bad = client.post("/api/hpsee/definitions", data={"kind": "flexx"}, files={"file": ("x.sdf", b"x")})
    assert bad.status_code == 422 and ".flexx" in bad.json()["detail"]
    empty = client.post("/api/hpsee/definitions", data={"kind": "flexx"}, files={"file": ("x.flexx", b"  ")})
    assert empty.status_code == 422
