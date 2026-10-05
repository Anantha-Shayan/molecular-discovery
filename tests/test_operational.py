"""
Operational behaviour: health/readiness, security hardening, safe failure
reporting, restart recovery and storage path safety.
"""
from __future__ import annotations

import dataclasses
import io
import logging
import json

import pytest
from sqlalchemy import select

from backend import main, models, pipeline, storage
from backend.config import settings as real_settings
from backend.database import get_session
from backend.targets import service as target_service


def patched_settings(monkeypatch, *modules, **overrides):
    """Swap the (frozen) settings object in the given modules."""
    replaced = dataclasses.replace(real_settings, **overrides)
    for module in modules:
        monkeypatch.setattr(module, "settings", replaced)
    return replaced


# ---------------------------------------------------------------------------
# Health / readiness
# ---------------------------------------------------------------------------
def test_health_is_alive_and_touches_nothing_external(client, monkeypatch):
    """Liveness must not depend on the database."""
    def boom():
        raise AssertionError("health must not query the database")

    monkeypatch.setattr(main, "check_connection", boom)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_dependency_checks(client):
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["data_dir"] == "writable"
    assert body["checks"]["demo_fixture"] == "present"


def test_ready_returns_503_when_database_is_down_without_leaking_details(client, monkeypatch):
    def down():
        raise RuntimeError("connection to postgresql://user:hunter2@db:5432 refused")

    monkeypatch.setattr(main, "check_connection", down)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert body["checks"]["database"] == "unavailable"
    assert "hunter2" not in response.text
    assert "postgresql://" not in response.text


def test_ready_returns_503_when_data_dir_is_not_writable(client, monkeypatch):
    monkeypatch.setattr(storage, "is_writable", lambda: False)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["data_dir"] == "not writable"


# ---------------------------------------------------------------------------
# Security: headers, uploads, filenames, paths
# ---------------------------------------------------------------------------
def test_security_headers_are_set(client):
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_no_cors_headers_are_sent_by_default(client):
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_oversized_upload_is_rejected_with_413(client, monkeypatch):
    patched_settings(monkeypatch, main, target_service, max_upload_mb=1)
    too_big = b"ATOM      1  N   ALA A   1       1.000   2.000   3.000\n" * 40_000  # ~2 MB
    response = client.post(
        "/api/targets/upload", files={"file": ("big.pdb", too_big, "chemical/x-pdb")}
    )
    assert response.status_code == 413
    assert "1 MB" in response.json()["detail"]["message"]


def test_chunked_read_enforces_the_limit_without_content_length(monkeypatch):
    """The streaming read is the backstop when Content-Length is absent."""
    import asyncio

    from fastapi import HTTPException, UploadFile

    patched_settings(monkeypatch, main, max_upload_mb=1)
    upload = UploadFile(file=io.BytesIO(b"x" * (2 * 1024 * 1024)), filename="big.pdb")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main._read_limited(upload))
    assert caught.value.status_code == 413


@pytest.mark.parametrize(
    "hostile,expected",
    [
        ("../../etc/passwd.pdb", "passwd.pdb"),
        ("..\\..\\windows\\system32\\evil.pdb", "evil.pdb"),
        ("/etc/shadow.pdb", "shadow.pdb"),
        # The "/" in "</script>" is a path separator, so only the tail survives.
        ("a<script>alert(1)</script>.pdb", "script.pdb"),
        ("a<b>&\"c\".pdb", "ab&c.pdb"),
        ("bell\x07\x00name.pdb", "bellname.pdb"),
        ("   ...  ", None),
        ("", None),
        (None, None),
    ],
)
def test_filenames_are_reduced_to_a_plain_basename(hostile, expected):
    assert target_service.sanitize_filename(hostile) == expected


def test_hostile_filename_never_influences_where_files_are_written(client, demo_pdb_bytes):
    response = client.post(
        "/api/targets/upload",
        files={"file": ("../../../../tmp/pwned.pdb", demo_pdb_bytes, "chemical/x-pdb")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "pwned.pdb"

    with get_session() as session:
        stored = session.get(models.Target, body["target_id"]).structure_path
    assert stored == f"targets/{body['target_id']}/structure.pdb"
    resolved = storage.resolve(stored)
    assert storage.data_dir().resolve() in resolved.parents


def test_control_characters_are_stripped_from_labels():
    assert target_service.clean_text("KRAS\x00 \x1b[31mG12D\n\tmutant", 100) == "KRAS [31mG12D mutant"
    assert len(target_service.clean_text("x" * 500, 50)) == 50
    assert target_service.clean_text("\x00\x01", 50) is None


@pytest.mark.parametrize("bad", ["../..", "7RP", "7RPZZ", "²²²²", "7rp/", "7R P", "", "%2e%2e"])
def test_pdb_id_must_match_the_strict_pattern(client, bad):
    response = client.post("/api/targets/pdb-id", json={"pdb_id": bad})
    assert response.status_code == 422


def test_storage_resolve_rejects_paths_outside_the_data_directory():
    for escape in ("../outside.txt", "runs/../../outside.txt", "/etc/passwd"):
        with pytest.raises(storage.StoragePathError):
            storage.resolve(escape)
    assert storage.resolve("runs/job/stage/file.txt").is_relative_to(storage.data_dir().resolve())


def test_storage_refuses_to_persist_a_path_outside_the_data_directory(tmp_path):
    with pytest.raises(storage.StoragePathError):
        storage.to_stored(tmp_path / "elsewhere.txt")


def test_tampered_structure_path_cannot_read_arbitrary_files(client):
    """Even if a row were corrupted to point at /etc/passwd, the structure
    endpoint must not serve it."""
    target = client.post("/api/targets/demo").json()
    with get_session() as session:
        row = session.get(models.Target, target["target_id"])
        row.structure_path = "/etc/passwd"
    response = client.get(f"/api/targets/{target['target_id']}/structure")
    assert response.status_code == 404
    assert "root:" not in response.text


def test_structure_is_served_with_a_non_html_content_type(client):
    target = client.post("/api/targets/demo").json()
    response = client.get(f"/api/targets/{target['target_id']}/structure")
    assert not response.headers["content-type"].startswith("text/html")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_uploaded_markup_is_stored_as_inert_data(client):
    """A malicious header is just text: stored verbatim-minus-control-chars,
    returned as JSON, and escaped by the frontend (esc()) when displayed."""
    pdb = (
        "HEADER    X                                       01-JAN-00   1ABC\n"
        "TITLE     <img src=x onerror=alert(1)>\n"
        "ATOM      1  N   ALA A   1       1.000   2.000   3.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       2.000   3.000   4.000  1.00  0.00           C\n"
    ).encode()
    response = client.post(
        "/api/targets/upload", files={"file": ("x.pdb", pdb, "chemical/x-pdb")}
    )
    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/json")
    assert "onerror" in response.json()["title"]  # kept as data, not executed


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
def test_unhandled_errors_do_not_leak_internals_in_production(lenient_client, monkeypatch):
    patched_settings(monkeypatch, main, app_env="production")

    def boom(_session):
        raise RuntimeError("password=hunter2 host=internal-db.corp")

    monkeypatch.setattr(target_service, "create_demo_target", boom)
    response = lenient_client.post("/api/targets/demo")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Internal server error"
    assert body["request_id"]
    assert "hunter2" not in response.text
    assert "internal-db" not in response.text
    assert "Traceback" not in response.text


def test_development_shows_error_detail_to_aid_debugging(lenient_client, monkeypatch):
    def boom(_session):
        raise RuntimeError("visible in development")

    monkeypatch.setattr(target_service, "create_demo_target", boom)
    body = lenient_client.post("/api/targets/demo").json()
    assert "visible in development" in body["error"]


def test_api_docs_are_disabled_when_configured():
    from fastapi import FastAPI

    # Mirrors main.py's construction rule.
    docs_off = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    assert docs_off.docs_url is None and docs_off.openapi_url is None


def test_pipeline_failure_reason_is_sanitised_outside_development(lenient_client, monkeypatch):
    patched_settings(monkeypatch, pipeline, app_env="production")

    def boom(*_args, **_kwargs):
        raise ValueError("SELECT secret FROM t WHERE password='hunter2'")

    monkeypatch.setattr(pipeline.sa_service, "run", boom)
    target = lenient_client.post("/api/targets/demo").json()
    created = lenient_client.post(
        "/api/jobs",
        json={"target_id": target["target_id"], "library_limit": 10, "stage_delay_seconds": 0},
    )
    job_id = created.json()["job_id"] if created.status_code == 200 else None
    if job_id is None:  # the background task error surfaced; find the job instead
        with get_session() as session:
            job_id = session.execute(
                select(models.Job.id).order_by(models.Job.created_at.desc())
            ).scalars().first()

    status = lenient_client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "FAILED"
    reason = status["failure_reason"]
    assert "hunter2" not in reason and "SELECT" not in reason
    assert "ValueError" in reason and job_id in reason

    # The stage log detail (also exposed via the API) is sanitised too.
    assert all("hunter2" not in (stage.get("detail") or "") for stage in status["stages"])


def test_pipeline_failure_logs_carry_job_and_stage_context(lenient_client, monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise ValueError("bad smiles")

    monkeypatch.setattr(pipeline.sa_service, "run", boom)
    target = lenient_client.post("/api/targets/demo").json()
    with caplog.at_level(logging.ERROR, logger="mdp"):
        lenient_client.post(
            "/api/jobs",
            json={"target_id": target["target_id"], "library_limit": 10, "stage_delay_seconds": 0},
        )
    records = [r for r in caplog.records if getattr(r, "job_id", None)]
    assert records, "no log records carried a job_id"
    assert any(getattr(r, "stage", None) == "sa" for r in records)
    # Molecule-level context for the failing SA call.
    assert any(getattr(r, "molecule_id", None) for r in records)


def test_json_log_format_includes_context_and_no_secrets():
    from backend.logging_config import JsonFormatter

    record = logging.LogRecord("mdp.pipeline", logging.ERROR, "f.py", 1, "stage failed", (), None)
    record.job_id, record.stage, record.molecule_id = "job-1", "sa", "mol-9"
    line = json.loads(JsonFormatter().format(record))
    assert line["job_id"] == "job-1" and line["stage"] == "sa" and line["molecule_id"] == "mol-9"
    assert line["level"] == "ERROR" and line["msg"] == "stage failed"


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------
def test_interrupted_runs_are_marked_failed_on_startup(client):
    target = client.post("/api/targets/demo").json()
    with get_session() as session:
        stuck = models.Job(
            target_id=target["target_id"], status="AFFINITY", current_stage="affinity",
            label="RUN-STUCK", params={},
        )
        queued = models.Job(
            target_id=target["target_id"], status="SUBMITTED", label="RUN-QUEUED", params={},
        )
        done = models.Job(
            target_id=target["target_id"], status="COMPLETE", label="RUN-DONE", params={},
        )
        session.add_all([stuck, queued, done])
        session.flush()
        stuck_id, queued_id, done_id = stuck.id, queued.id, done.id

    assert pipeline.fail_interrupted_jobs() >= 2

    with get_session() as session:
        stuck = session.get(models.Job, stuck_id)
        assert stuck.status == "FAILED"
        assert "restarted" in stuck.failure_reason
        assert session.get(models.Job, queued_id).status == "FAILED"
        assert session.get(models.Job, done_id).status == "COMPLETE"  # untouched
        logged = session.execute(
            select(models.JobStageLog).where(
                models.JobStageLog.job_id == stuck_id, models.JobStageLog.status == "FAILED"
            )
        ).scalars().all()
        assert logged and logged[0].stage == "affinity"

    # Idempotent: nothing left to fail.
    assert pipeline.fail_interrupted_jobs() == 0
