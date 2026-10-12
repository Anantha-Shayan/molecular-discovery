"""
Workflow behaviour of the campaign engine: batching, persistence, retries,
failure inspection, recovery, cancellation, timeouts, duplicate prevention.

These tests replace ONLY the scientific executors (and receptor preparation)
with deterministic fakes, so they exercise the real runner, database and API
without needing Vina/HYDE. Nothing here claims scientific validity; real-tool
behaviour is covered by tests/integration (skipped when a tool is missing).
"""
from __future__ import annotations

import dataclasses
import hashlib
import threading
import time

import pytest
from sqlalchemy import select

from backend import models
from backend.campaign import executors as ex
from backend.campaign import runner as rn
from backend.database import get_session
from backend.tools import proc, registry


class Fake:
    """Programmable fake scientific backend."""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []      # (stage, batch-first-molecule display id)
        self.fail_batches: dict[str, set[int]] = {}  # stage -> molecule positions that raise
        self.fail_molecules: set[str] = set()
        self.block = threading.Event()
        self.block.set()
        self.live = 0
        self.max_live = 0
        self.lock = threading.Lock()
        self.sleep = 0.0

    def make(self, stage):
        def run(ctx, mols, batch_dir, log_path, cancel, progress, timeout):
            with self.lock:
                self.live += 1
                self.max_live = max(self.max_live, self.live)
                self.calls.append((stage, mols[0].display_id))
            try:
                t0 = time.monotonic()
                while time.monotonic() - t0 < self.sleep:
                    if cancel.is_set():
                        raise proc.ToolCancelled("Cancelled")
                    time.sleep(0.02)
                if not self.block.is_set():
                    while not self.block.wait(0.05):
                        if cancel.is_set():
                            raise proc.ToolCancelled("Cancelled")
                if any(m.display_id in self.fail_batches.get(stage, set()) for m in mols):
                    raise proc.ToolError("simulated tool crash", returncode=2, tail="boom line")
                out = []
                for m in mols:
                    if m.display_id in self.fail_molecules:
                        out.append(ex.MolOutcome(m.id, False, error="simulated ligand failure"))
                        continue
                    h = int(hashlib.sha256(m.display_id.encode()).hexdigest()[:6], 16)
                    if stage == "docking":
                        payload = {"engine": "vina", "score": -5.0 - (h % 700) / 100, "score_unit": "kcal/mol"}
                    else:
                        payload = {"cumulative_kj_mol": -(h % 3000) / 100, "affinity_lower_nm": 1.0,
                                   "affinity_upper_nm": 10.0}
                    out.append(ex.MolOutcome(m.id, True, payload))
                progress(sum(o.ok for o in out), sum(not o.ok for o in out))
                return out
            finally:
                with self.lock:
                    self.live -= 1
        return run


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setitem(ex.EXECUTORS, "docking", f.make("docking"))
    monkeypatch.setitem(ex.EXECUTORS, "hyde", f.make("hyde"))

    def fake_prepare(job_id):
        rn._set_stage(job_id, "prepare", "succeeded", finish=True, counts=(1, 1, 0), detail="fake")
        return "ok"
    monkeypatch.setattr(rn, "_stage_prepare", fake_prepare)

    real = registry.tool_statuses()
    avail = {k: dataclasses.replace(v, available=True, license_ok=True) for k, v in real.items()}
    monkeypatch.setattr(registry, "tool_statuses", lambda refresh=False: avail)
    monkeypatch.setattr(rn, "RETRY_BACKOFF_SECONDS", 0.05)
    monkeypatch.setattr(rn, "settings", dataclasses.replace(rn.settings, max_batch_attempts=2, max_parallel_batches=2))
    return f


def _site_ref(client, tid):
    return client.get(f"/api/targets/{tid}/sites").json()["sites"][0]["key"]


def start(client, **over):
    tid = client.post("/api/targets/demo").json()["target_id"]
    body = {"target_id": tid, "engine": "vina", "site": {"mode": "ligand", "reference": _site_ref(client, tid)},
            "library_source": "demo", "library_count": 12, "batch_size": 4, "enable_admet": False,
            "enable_hyde": True, "dock_keep_fraction": 1.0, "sa_threshold": 10.0}
    body.update(over)
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 201, r.text
    return r.json()["job_id"]


def wait(client, job_id, statuses=("COMPLETE", "FAILED", "CANCELLED"), timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        d = client.get(f"/api/jobs/{job_id}").json()
        if d["status"] in statuses:
            return d
        time.sleep(0.1)
    raise AssertionError(f"campaign did not reach {statuses}: {d['status']} at {d['current_stage']}")


def batches(client, job_id, stage):
    return client.get(f"/api/jobs/{job_id}/batches", params={"stage": stage}).json()["batches"]


def test_happy_path_persists_every_stage_and_batch(client, fake):
    job = start(client)
    d = wait(client, job)
    assert d["status"] == "COMPLETE", d["failure_reason"]
    keys = [s["key"] for s in d["stages"]]
    assert keys == ["prepare", "library", "docking", "hyde", "sa", "admet", "affinity", "kinetics", "rank", "grow"]
    st = {s["key"]: s for s in d["stages"]}
    assert st["admet"]["status"] == "skipped" and st["grow"]["status"] == "skipped"
    assert st["docking"]["batches"]["total"] == 3 and st["docking"]["batches"]["succeeded"] == 3
    assert st["affinity"]["mocked"] and st["kinetics"]["mocked"] and not st["docking"]["mocked"]
    cands = client.get(f"/api/jobs/{job}/candidates").json()
    assert cands["candidates"] and not cands["provisional"]
    scores = [c["hyde_cumulative_kj_mol"] for c in cands["candidates"]]
    assert scores == sorted(scores), "ranking policy: most negative HYDE score first"
    assert [c["rank"] for c in cands["candidates"]] == list(range(1, len(scores) + 1))


def test_mocked_stages_never_change_who_is_a_candidate(client, fake):
    job = start(client)
    wait(client, job)
    mols = client.get(f"/api/jobs/{job}/molecules", params={"outcome": "filtered"}).json()["molecules"]
    assert all(m["exit_stage"] not in ("affinity", "kinetics") for m in mols)


def test_failed_batch_is_retried_then_left_failed_and_inspectable(client, fake):
    job = start(client)
    fake.fail_batches["docking"] = {f"MDP-{job[:4].upper()}-0005"}
    d = wait(client, job)
    assert d["status"] == "FAILED"
    bs = batches(client, job, "docking")
    failed = [b for b in bs if b["status"] == "failed"]
    ok = [b for b in bs if b["status"] == "succeeded"]
    assert len(failed) == 1 and len(ok) == 2
    assert failed[0]["attempt"] == 2, "automatic retries must be bounded by MAX_BATCH_ATTEMPTS"
    assert "simulated tool crash" in failed[0]["error"]
    # Failure reason is human readable and names the stage.
    assert "Docking" in d["failure_reason"]
    # Later stages never ran on a partial funnel.
    assert {s["key"]: s["status"] for s in d["stages"]}["hyde"] == "pending"


def test_retrying_only_the_failed_batch_does_not_repeat_finished_work(client, fake):
    job = start(client)
    fake.fail_batches["docking"] = {f"MDP-{job[:4].upper()}-0005"}
    assert wait(client, job)["status"] == "FAILED"
    calls_before = list(fake.calls)
    fake.fail_batches.clear()
    bid = [b for b in batches(client, job, "docking") if b["status"] == "failed"][0]["batch_id"]
    r = client.post(f"/api/jobs/{job}/retry", json={"batch_ids": [bid]})
    assert r.status_code == 200 and r.json()["queued_batches"] == 1
    d = wait(client, job)
    assert d["status"] == "COMPLETE"
    new_docking = [c for c in fake.calls[len(calls_before):] if c[0] == "docking"]
    assert len(new_docking) == 1, "only the failed batch may run again"
    assert all(b["status"] == "succeeded" for b in batches(client, job, "docking"))


def test_retry_rejects_foreign_batches_and_running_campaigns(client, fake):
    job = start(client)
    other = start(client)
    wait(client, job)
    wait(client, other)
    foreign = batches(client, other, "docking")[0]["batch_id"]
    assert client.post(f"/api/jobs/{job}/retry", json={"batch_ids": [foreign]}).status_code == 409
    assert client.post(f"/api/jobs/{job}/retry", json={}).status_code == 409  # nothing failed


def test_molecule_level_failure_does_not_fail_the_batch_and_can_be_retried(client, fake):
    job = start(client)
    victim = f"MDP-{job[:4].upper()}-0002"
    fake.fail_molecules = {victim}
    d = wait(client, job)
    assert d["status"] == "COMPLETE"
    m = [x for x in client.get(f"/api/jobs/{job}/molecules").json()["molecules"] if x["display_id"] == victim][0]
    assert m["outcome"] == "failed" and m["exit_stage"] == "docking"
    assert "simulated ligand failure" in m["exit_reason"]
    assert all(b["status"] == "succeeded" for b in batches(client, job, "docking"))
    fake.fail_molecules = set()
    r = client.post(f"/api/jobs/{job}/retry", json={"failed_molecules": True})
    assert r.status_code == 200
    d = wait(client, job)
    assert d["status"] == "COMPLETE"
    m = [x for x in client.get(f"/api/jobs/{job}/molecules").json()["molecules"] if x["display_id"] == victim][0]
    assert m["outcome"] in ("active", "filtered") and m["exit_stage"] != "docking"


def test_interrupted_campaign_resumes_without_repeating_completed_batches(client, fake):
    job = start(client)
    wait(client, job)
    # Simulate a crash mid-docking: put the campaign back in the state a kill -9 would leave.
    with get_session() as s:
        j = s.get(models.Job, job)
        j.status = "RUNNING"
        stages = {x.stage: x for x in s.execute(select(models.JobStage).where(models.JobStage.job_id == job)).scalars()}
        for name in ("docking", "hyde", "sa", "affinity", "kinetics", "rank"):
            stages[name].status = "pending" if name != "docking" else "running"
        bs = s.execute(select(models.Batch).where(models.Batch.job_id == job, models.Batch.stage == "docking")
                       .order_by(models.Batch.index)).scalars().all()
        bs[2].status = "running"
        for name in ("hyde", "sa", "affinity", "kinetics"):
            s.execute(models.Batch.__table__.delete().where(models.Batch.job_id == job, models.Batch.stage == name))
        s.execute(models.StageResult.__table__.delete().where(
            models.StageResult.job_id == job, models.StageResult.stage.in_(("hyde", "sa", "affinity", "kinetics", "rank"))))
        s.execute(models.StageResult.__table__.delete().where(
            models.StageResult.batch_id == bs[2].id))
        for m in s.execute(select(models.Molecule).where(models.Molecule.job_id == job)).scalars():
            if m.exit_stage != "docking":
                m.outcome, m.exit_stage, m.exit_reason = "active", None, None
    calls_before = len(fake.calls)
    assert rn.recover_interrupted() >= 1
    d = wait(client, job)
    assert d["status"] == "COMPLETE"
    docking_again = [c for c in fake.calls[calls_before:] if c[0] == "docking"]
    assert len(docking_again) == 1, "only the interrupted batch is re-run"


def test_cancel_stops_running_work_and_resume_finishes_it(client, fake):
    fake.block.clear()
    job = start(client)
    wait(client, job, statuses=("RUNNING",))
    end = time.monotonic() + 20
    while not fake.calls and time.monotonic() < end:
        time.sleep(0.05)
    assert client.post(f"/api/jobs/{job}/cancel").status_code == 200
    d = wait(client, job, statuses=("CANCELLED",))
    assert {b["status"] for b in batches(client, job, "docking")} <= {"cancelled", "succeeded"}
    fake.block.set()
    assert client.post(f"/api/jobs/{job}/resume").status_code == 200
    assert wait(client, job)["status"] == "COMPLETE"


def test_batch_timeout_marks_batch_failed_with_reason(client, fake, monkeypatch):
    monkeypatch.setattr(rn, "settings", dataclasses.replace(rn.settings, batch_timeout_seconds=1, max_batch_attempts=1))
    fake.sleep = 5.0
    job = start(client, library_count=4)
    d = wait(client, job)
    assert d["status"] == "FAILED"
    b = batches(client, job, "docking")[0]
    assert b["status"] == "failed" and "Timed out" in b["error"]


def test_concurrency_never_exceeds_the_configured_bound(client, fake):
    fake.sleep = 0.2
    job = start(client, library_count=12, batch_size=2)
    wait(client, job)
    assert 1 <= fake.max_live <= 2


def test_a_batch_cannot_be_claimed_twice(client, fake):
    job = start(client)
    wait(client, job)
    bid = batches(client, job, "docking")[0]["batch_id"]
    n = len(fake.calls)
    with get_session() as s:
        b = s.get(models.Batch, bid)
        b.status = "pending"
    p = rn.P.resolve(models.Job and {})  # defaults are enough: executor is faked
    threads = [threading.Thread(target=rn._execute_batch, args=(job, "docking", bid, {**p, "site": {}}, 1,
               threading.Event(), threading.Event())) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([c for c in fake.calls[n:] if c[0] == "docking"]) == 1


def test_creation_is_rejected_for_unavailable_tools_and_bad_sites(client, monkeypatch):
    real = registry.tool_statuses()
    off = {k: dataclasses.replace(v, available=(k != "flexx"), reason="Executable not found." if k == "flexx" else None)
           for k, v in real.items()}
    monkeypatch.setattr(registry, "tool_statuses", lambda refresh=False: off)
    tid = client.post("/api/targets/demo").json()["target_id"]
    ref = _site_ref(client, tid)
    base = {"target_id": tid, "site": {"mode": "ligand", "reference": ref}, "library_source": "demo"}
    r = client.post("/api/jobs", json={**base, "engine": "flexx"})
    assert r.status_code == 422 and "not available" in r.json()["detail"]
    r = client.post("/api/jobs", json={**base, "site": {"mode": "ligand", "reference": "XXX:A:1"}})
    assert r.status_code == 422
    r = client.post("/api/jobs", json={**base, "site": {"mode": "manual", "center": [900, 900, 900], "size": [20, 20, 20]}})
    assert r.status_code == 422 and "protein" in r.json()["detail"]
    r = client.post("/api/jobs", json={**base, "library_count": 999999})
    assert r.status_code in (422,)
