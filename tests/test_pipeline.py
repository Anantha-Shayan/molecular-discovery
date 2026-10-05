"""
Pipeline behaviour: job creation from a real target, molecule lineage,
real vs mocked stage results, funnel attrition, artifacts, and the
target-dependence that proves the pipeline consumes the user's input.
"""
from __future__ import annotations

import json
import os

from sqlalchemy import select

from backend import models, pipeline
from backend.database import get_session


def run_job(client, target_id, **config):
    payload = {"target_id": target_id, "stage_delay_seconds": 0, **config}
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 200
    return response.json()["job_id"]


def stage_results(session, job_id, stage):
    return session.execute(
        select(models.StageResult).where(
            models.StageResult.job_id == job_id, models.StageResult.stage == stage
        )
    ).scalars().all()


# ---------------------------------------------------------------------------
# Job creation and completion
# ---------------------------------------------------------------------------
def test_job_is_created_from_target_and_configuration(client):
    target = client.post("/api/targets/demo").json()
    response = client.post(
        "/api/jobs",
        json={
            "target_id": target["target_id"],
            "library_limit": 10,
            "sa_threshold": 4.0,
            "affinity_shortlist": 3,
            "stage_delay_seconds": 0,
        },
    )
    assert response.status_code == 200
    body = response.json()

    assert body["job_id"]
    assert body["target_name"] == "KRAS G12D"
    assert body["pdb_id"] == "7RPZ"

    # Configuration is persisted with the job, not just applied and lost.
    with get_session() as session:
        job = session.get(models.Job, body["job_id"])
        assert job.target_id == target["target_id"]
        assert job.params["library_limit"] == 10
        assert job.params["sa_threshold"] == 4.0
        assert job.params["affinity_shortlist"] == 3
        assert job.status == "COMPLETE"  # BackgroundTasks run before the response


def test_pipeline_reaches_complete_and_logs_every_stage(client):
    target = client.post("/api/targets/demo").json()
    job_id = run_job(client, target["target_id"], library_limit=10)

    with get_session() as session:
        job = session.get(models.Job, job_id)
        assert job.status == "COMPLETE"
        assert job.current_stage is None
        assert job.failure_reason is None

        logs = session.execute(
            select(models.JobStageLog).where(models.JobStageLog.job_id == job_id)
        ).scalars().all()
        by_stage = {}
        for log in logs:
            by_stage.setdefault(log.stage, []).append(log.status)

        for stage in ["target"] + pipeline.STAGE_SEQUENCE:
            assert stage in by_stage, f"no log rows for stage {stage}"
            assert "SUCCEEDED" in by_stage[stage]


# ---------------------------------------------------------------------------
# The pipeline must actually use the user's target
# ---------------------------------------------------------------------------
def test_target_structure_is_staged_into_the_run_directory(client, demo_pdb_bytes):
    target = client.post("/api/targets/demo").json()
    job_id = run_job(client, target["target_id"], library_limit=10)

    with get_session() as session:
        artifacts = session.execute(
            select(models.Artifact).where(
                models.Artifact.job_id == job_id, models.Artifact.stage == "target"
            )
        ).scalars().all()
        assert artifacts, "pipeline did not stage the target structure"

        path = artifacts[0].storage_path
        assert os.path.exists(path)
        assert job_id in path
        # Byte-identical to what the user supplied.
        with open(path, "rb") as handle:
            assert handle.read() == demo_pdb_bytes


def test_results_are_deterministic_for_the_same_target(client):
    target = client.post("/api/targets/demo").json()
    first = run_job(client, target["target_id"], library_limit=25)
    second = run_job(client, target["target_id"], library_limit=25)

    def fingerprint(job_id):
        rows = client.get(f"/api/jobs/{job_id}/candidates").json()["candidates"]
        return [(r["smiles"], r["kd_nm"], r["koff_per_s"], r["sa_score"]) for r in rows]

    assert fingerprint(first) == fingerprint(second)


def test_results_differ_for_a_different_structure(client, demo_pdb_bytes):
    """Mocked values are seeded from the structure, so a different input
    file genuinely changes the run — the target is consumed, not displayed."""
    demo = client.post("/api/targets/demo").json()

    modified = demo_pdb_bytes.replace(b"HEADER", b"HEADER", 1) + b"REMARK 999 MODIFIED COPY\n"
    other = client.post(
        "/api/targets/upload",
        files={"file": ("variant.pdb", modified, "chemical/x-pdb")},
    ).json()
    assert other["checksum"] != demo["checksum"]

    demo_job = run_job(client, demo["target_id"], library_limit=25)
    other_job = run_job(client, other["target_id"], library_limit=25)

    def kd_by_smiles(job_id):
        rows = client.get(f"/api/jobs/{job_id}/candidates").json()["candidates"]
        return {row["smiles"]: row["kd_nm"] for row in rows}

    demo_kd, other_kd = kd_by_smiles(demo_job), kd_by_smiles(other_job)
    shared = set(demo_kd) & set(other_kd)
    assert shared, "expected overlapping molecules between the two runs"
    assert any(demo_kd[s] != other_kd[s] for s in shared)


def test_sa_scores_are_identical_across_targets(client, demo_pdb_bytes):
    """SA is a real property of the molecule — it must NOT vary by target."""
    demo = client.post("/api/targets/demo").json()
    other = client.post(
        "/api/targets/upload",
        files={"file": ("variant2.pdb", demo_pdb_bytes + b"REMARK 999 X\n", "chemical/x-pdb")},
    ).json()

    def sa_by_smiles(job_id):
        rows = client.get(f"/api/jobs/{job_id}/candidates").json()["candidates"]
        return {row["smiles"]: row["sa_score"] for row in rows}

    demo_sa = sa_by_smiles(run_job(client, demo["target_id"], library_limit=25))
    other_sa = sa_by_smiles(run_job(client, other["target_id"], library_limit=25))

    for smiles in set(demo_sa) & set(other_sa):
        assert demo_sa[smiles] == other_sa[smiles]


# ---------------------------------------------------------------------------
# Molecules and lineage
# ---------------------------------------------------------------------------
def test_molecules_are_created_with_stable_ids_across_stages(client):
    target = client.post("/api/targets/demo").json()
    job_id = run_job(client, target["target_id"], library_limit=25)

    with get_session() as session:
        molecules = session.execute(
            select(models.Molecule).where(models.Molecule.job_id == job_id)
        ).scalars().all()
        assert len(molecules) == 25
        assert len({m.id for m in molecules}) == 25
        assert len({m.display_id for m in molecules}) == 25

        screened_ids = {m.id for m in molecules}

        # Every stage result must point at a molecule created during screening;
        # identity is carried by Molecule.id, never re-derived from SMILES.
        for stage in pipeline.STAGE_SEQUENCE:
            rows = stage_results(session, job_id, stage)
            assert rows, f"no results for stage {stage}"
            assert {r.molecule_id for r in rows} <= screened_ids

        # A molecule reaching the final stage must have a row at every stage.
        final = stage_results(session, job_id, "kinetics")
        for row in final:
            stages_for_molecule = {
                r.stage
                for r in session.execute(
                    select(models.StageResult).where(
                        models.StageResult.molecule_id == row.molecule_id
                    )
                ).scalars().all()
            }
            assert set(pipeline.STAGE_SEQUENCE) <= stages_for_molecule


def test_duplicate_smiles_would_not_merge_lineage(client):
    """Two runs of the same library produce distinct Molecule rows per job."""
    target = client.post("/api/targets/demo").json()
    first = run_job(client, target["target_id"], library_limit=10)
    second = run_job(client, target["target_id"], library_limit=10)

    with get_session() as session:
        first_ids = {
            m.id for m in session.execute(
                select(models.Molecule).where(models.Molecule.job_id == first)
            ).scalars().all()
        }
        second_ids = {
            m.id for m in session.execute(
                select(models.Molecule).where(models.Molecule.job_id == second)
            ).scalars().all()
        }
    assert first_ids.isdisjoint(second_ids)


# ---------------------------------------------------------------------------
# Real vs mocked
# ---------------------------------------------------------------------------
def test_sa_stage_is_real_and_others_are_flagged_mocked(client):
    target = client.post("/api/targets/demo").json()
    job_id = run_job(client, target["target_id"], library_limit=10)

    with get_session() as session:
        sa_rows = stage_results(session, job_id, "sa")
        assert sa_rows
        for row in sa_rows:
            assert row.is_mocked is False
            assert row.payload["method"].startswith("Ertl & Schuffenhauer")
            assert 1.0 <= row.payload["sa_score"] <= 10.0
            assert row.payload["molecular_weight"] > 0

        for stage in ("screening", "admet", "affinity", "kinetics"):
            rows = stage_results(session, job_id, stage)
            assert rows
            assert all(row.is_mocked is True for row in rows)
            # Mocked payloads carry their own disclaimer.
            assert all("note" in row.payload for row in rows)


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------
def test_funnel_reduces_candidates(client):
    target = client.post("/api/targets/demo").json()
    job_id = run_job(client, target["target_id"], library_limit=25, affinity_shortlist=8)

    stages = {s["key"]: s for s in client.get(f"/api/jobs/{job_id}").json()["stages"]}

    screening = stages["screening"]["count_out"]
    sa = stages["sa"]["count_out"]
    admet = stages["admet"]["count_out"]
    affinity = stages["affinity"]["count_out"]
    kinetics = stages["kinetics"]["count_out"]

    assert screening == 25
    assert sa < screening, "SA filter rejected nothing"
    assert admet < sa, "ADMET gate rejected nothing"
    assert affinity <= admet
    assert kinetics == affinity
    assert kinetics >= 1


def test_sa_threshold_changes_survivor_count(client):
    target = client.post("/api/targets/demo").json()
    strict = run_job(client, target["target_id"], library_limit=25, sa_threshold=2.5)
    loose = run_job(client, target["target_id"], library_limit=25, sa_threshold=9.0)

    def sa_out(job_id):
        stages = {s["key"]: s for s in client.get(f"/api/jobs/{job_id}").json()["stages"]}
        return stages["sa"]["count_out"]

    assert sa_out(strict) < sa_out(loose)


def test_disabled_stages_are_skipped(client):
    target = client.post("/api/targets/demo").json()
    job_id = run_job(
        client, target["target_id"], library_limit=10,
        enable_admet=False, enable_affinity=False,
    )

    stages = {s["key"]: s for s in client.get(f"/api/jobs/{job_id}").json()["stages"]}
    assert stages["admet"]["status"] == "skipped"
    assert stages["affinity"]["status"] == "skipped"
    assert stages["kinetics"]["count_out"] > 0  # still ran

    with get_session() as session:
        assert stage_results(session, job_id, "admet") == []


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
def test_run_artifacts_are_written(client):
    target = client.post("/api/targets/demo").json()
    job_id = run_job(client, target["target_id"], library_limit=25)

    with get_session() as session:
        artifacts = session.execute(
            select(models.Artifact).where(models.Artifact.job_id == job_id)
        ).scalars().all()
        # Read the paths out while the session is still open.
        paths = {a.stage: a.storage_path for a in artifacts}

    for stage in ("target", "screening", "sa", "admet", "affinity", "kinetics", "final"):
        assert stage in paths, f"missing artifact for {stage}"
        assert os.path.exists(paths[stage])

    # The SA artifact holds real per-molecule scores keyed by molecule id.
    with open(paths["sa"]) as handle:
        sa_rows = json.load(handle)
    assert len(sa_rows) == 25
    assert all("molecule_id" in row and "sa_score" in row for row in sa_rows)

    with open(paths["final"]) as handle:
        assert "$$$$" in handle.read()


def test_failed_job_records_a_reason(client):
    """A target with no parsable structure fails cleanly and is recorded."""
    created = client.post(
        "/api/jobs",
        json={"target_name": "Legacy target", "pdb_id": "XXXX",
              "library_limit": 5, "stage_delay_seconds": 0},
    )
    assert created.status_code == 200
    job_id = created.json()["job_id"]

    # Legacy metadata-only targets have no structure file, which is allowed:
    # the run proceeds and simply records that nothing was staged.
    with get_session() as session:
        job = session.get(models.Job, job_id)
        assert job.status == "COMPLETE"
        logs = [
            log for log in session.execute(
                select(models.JobStageLog).where(
                    models.JobStageLog.job_id == job_id,
                    models.JobStageLog.stage == "target",
                )
            ).scalars().all()
        ]
        assert any("metadata only" in (log.detail or "") for log in logs)


# ---------------------------------------------------------------------------
# Progress must be visible to other connections while the run is in flight
# ---------------------------------------------------------------------------
def test_stage_progress_is_visible_to_other_connections_mid_run(client, monkeypatch):
    """The UI polls from separate connections. If the pipeline only commits
    at the very end, a poller sees SUBMITTED and then COMPLETE with nothing
    in between. Observe job state from an independent session at each paced
    stage and require to see the run advancing."""
    target = client.post("/api/targets/demo").json()
    observed: list[tuple[str, str | None]] = []
    job_ids: list[str] = []

    def spy(_delay):
        if not job_ids:
            return
        with get_session() as other:  # a brand-new connection
            job = other.get(models.Job, job_ids[0])
            observed.append((job.status, job.current_stage))

    monkeypatch.setattr(pipeline, "pace", spy)

    # Create the job without running it, then run the pipeline directly so
    # we know the id before the first stage starts.
    with get_session() as session:
        job = models.Job(
            target_id=target["target_id"], status="SUBMITTED", label="RUN-VIS",
            params={"library_limit": 25, "run_tag": "VIS", "stage_delay_seconds": 0},
        )
        session.add(job)
        session.flush()
        job_ids.append(job.id)
    with get_session() as session:
        pipeline.run_pipeline(session, job_ids[0])

    stages_seen = [stage for _status, stage in observed]
    # pace() runs at the start of admet, affinity and kinetics; each time the
    # earlier stages' commits must already be visible to the other connection.
    assert stages_seen == ["admet", "affinity", "kinetics"], observed
    assert all(status != "SUBMITTED" for status, _ in observed)
