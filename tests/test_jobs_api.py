"""Job API: creation contract, listing, stage cards, candidate rows."""
from __future__ import annotations


def make_job(client, **config):
    target = client.post("/api/targets/demo").json()
    response = client.post(
        "/api/jobs",
        json={"target_id": target["target_id"], "stage_delay_seconds": 0, **config},
    )
    assert response.status_code == 200
    return response.json()


def test_create_job_returns_job_id_and_status(client):
    body = make_job(client, library_limit=10)
    assert body["job_id"]
    assert body["status"] in ("SUBMITTED", "SCREENING", "COMPLETE")
    assert body["label"].startswith("RUN-")


def test_legacy_request_shape_still_works(client):
    """The original contract (target_name + pdb_id) must not break."""
    response = client.post(
        "/api/jobs",
        json={
            "target_name": "KRAS G12D",
            "pdb_id": "8AZX",
            "run_tag": "0894",
            "library_limit": 10,
            "sa_threshold": 5.0,
            "stage_delay_seconds": 0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target_name"] == "KRAS G12D"
    assert body["pdb_id"] == "8AZX"

    # Original response fields are all still present.
    for field in ("job_id", "status", "current_stage", "stages", "retained"):
        assert field in body

    candidates = client.get(f"/api/jobs/{body['job_id']}/candidates").json()
    assert candidates["candidates"]


def test_job_status_exposes_target_config_and_engines(client):
    job = make_job(client, library_limit=25, sa_threshold=4.5, affinity_shortlist=5)
    status = client.get(f"/api/jobs/{job['job_id']}").json()

    assert status["target"]["pdb_id"] == "7RPZ"
    assert status["target"]["residue_count"] == 168
    assert status["config"]["sa_threshold"] == 4.5
    assert status["config"]["affinity_shortlist"] == 5
    assert status["created_at"]

    engines = {engine["key"]: engine["is_mocked"] for engine in status["engines"]}
    assert engines["sa"] is False
    assert engines["admet"] is True


def test_stage_cards_cover_the_full_funnel(client):
    job = make_job(client, library_limit=10)
    stages = client.get(f"/api/jobs/{job['job_id']}").json()["stages"]

    keys = [stage["key"] for stage in stages]
    assert keys == ["target", "screening", "sa", "admet", "affinity", "kinetics", "candidates"]

    by_key = {stage["key"]: stage for stage in stages}
    assert by_key["sa"]["is_mocked"] is False
    assert by_key["screening"]["is_mocked"] is True
    # The scale note separates the conceptual figure from what actually ran.
    assert "10T+" in by_key["screening"]["scale_note"]
    assert "10-compound demo library" in by_key["screening"]["scale_note"]
    # Stage detail reports real in → out counts.
    assert "in →" in by_key["sa"]["detail"]


def test_list_jobs_returns_summaries(client):
    job = make_job(client, library_limit=10)
    jobs = client.get("/api/jobs").json()["jobs"]

    assert jobs, "expected at least one job"
    ids = {row["job_id"] for row in jobs}
    assert job["job_id"] in ids

    row = next(r for r in jobs if r["job_id"] == job["job_id"])
    assert row["target_name"] == "KRAS G12D"
    assert row["target_source"] == "demo"
    assert row["candidate_count"] >= 1
    assert row["status"] == "COMPLETE"


def test_candidate_rows_and_detail_are_consistent(client):
    job = make_job(client, library_limit=25)
    candidates = client.get(f"/api/jobs/{job['job_id']}/candidates").json()["candidates"]
    assert candidates

    # Ranked by residence time, descending, starting at 1.
    assert [c["rank"] for c in candidates] == list(range(1, len(candidates) + 1))
    times = [c["residence_time_min"] for c in candidates]
    assert times == sorted(times, reverse=True)

    first = candidates[0]
    assert first["structure_svg"].startswith("<svg") or "<svg" in first["structure_svg"]
    assert first["molecular_weight"] > 0
    assert 1.0 <= first["sa_score"] <= 10.0
    assert first["status"] in ("Nominated", "Shortlisted", "Review")

    detail = client.get(f"/api/candidates/{first['molecule_id']}").json()
    assert detail["display_id"] == first["display_id"]
    assert detail["smiles"] == first["smiles"]
    assert detail["sa_score"] == first["sa_score"]
    assert detail["kd_nm"] == first["kd_nm"]

    # Provenance names the real target and labels mocked stages.
    labels = [step["label"] for step in detail["provenance"]]
    assert any("KRAS G12D" in label for label in labels)
    mocked = [step["label"] for step in detail["provenance"] if step["is_mocked"]]
    assert any("ADMET" in label for label in mocked)
    assert any("Affinity" in label for label in mocked)
    sa_step = next(step for step in detail["provenance"] if "SA Filter" in step["label"])
    assert sa_step["is_mocked"] is False


def test_unknown_ids_return_404(client):
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/candidates").status_code == 404
    assert client.get("/api/candidates/nope").status_code == 404


def test_frontend_is_served(client):
    """The UI is served by the API, so the demo is a single process."""
    assert client.get("/", follow_redirects=False).status_code in (302, 307)
    page = client.get("/app/discoveries.html")
    assert page.status_code == 200
    assert "Discoveries" in page.text
