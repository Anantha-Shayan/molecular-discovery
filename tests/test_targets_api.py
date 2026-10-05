"""Target intake API: upload, demo fixture, persistence, error reporting."""
from __future__ import annotations

import os

from backend import models
from backend.database import get_session


def test_demo_target_is_created_and_persisted(client):
    response = client.post("/api/targets/demo")
    assert response.status_code == 201

    body = response.json()
    assert body["name"] == "KRAS G12D"
    assert body["pdb_id"] == "7RPZ"
    assert body["source"] == "demo"
    assert body["is_demo"] is True
    assert body["validation_status"] == "PASSED"
    assert body["residue_count"] == 168
    assert body["atom_count"] == 1690
    assert body["selected_chain"] == "A"
    assert body["resolution_a"] == 1.3

    # Persisted, with the structure file actually written to disk.
    with get_session() as session:
        target = session.get(models.Target, body["target_id"])
        assert target is not None
        assert target.structure_path and os.path.exists(target.structure_path)
        assert target.artifact_id is not None
        artifact = session.get(models.Artifact, target.artifact_id)
        assert artifact.kind == "pdb"
        assert os.path.exists(artifact.storage_path)


def test_demo_target_is_deterministic(client):
    first = client.post("/api/targets/demo").json()
    second = client.post("/api/targets/demo").json()

    assert first["target_id"] != second["target_id"]  # distinct records
    assert first["checksum"] == second["checksum"]    # identical structure
    assert first["atom_count"] == second["atom_count"]
    assert first["residue_count"] == second["residue_count"]


def test_upload_accepts_a_valid_pdb(client, demo_pdb_bytes):
    response = client.post(
        "/api/targets/upload",
        files={"file": ("my_structure.pdb", demo_pdb_bytes, "chemical/x-pdb")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "upload"
    assert body["original_filename"] == "my_structure.pdb"
    assert body["is_demo"] is False
    # Metadata is derived from the uploaded content, not from the filename.
    assert body["pdb_id"] == "7RPZ"
    assert body["atom_count"] == 1690


def test_upload_rejects_a_file_with_no_atoms(client):
    response = client.post(
        "/api/targets/upload",
        files={"file": ("broken.pdb", b"HEADER  nothing\nEND\n", "chemical/x-pdb")},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "no atom" in detail["message"].lower()
    # The failing gate is reported so the UI can show which check failed.
    failed = [check for check in detail["checks"] if not check["passed"]]
    assert failed and failed[0]["key"] == "atoms"


def test_upload_rejects_an_empty_file(client):
    response = client.post(
        "/api/targets/upload", files={"file": ("empty.pdb", b"", "chemical/x-pdb")}
    )
    assert response.status_code == 422
    assert "empty" in response.json()["detail"]["message"].lower()


def test_upload_rejects_an_unsupported_file_type(client):
    response = client.post(
        "/api/targets/upload",
        files={"file": ("notes.txt", b"ATOM whatever", "text/plain")},
    )
    assert response.status_code == 422
    assert ".pdb" in response.json()["detail"]["message"]


def test_structure_endpoint_returns_the_original_text(client, demo_pdb_bytes):
    target = client.post("/api/targets/demo").json()
    response = client.get(f"/api/targets/{target['target_id']}/structure")
    assert response.status_code == 200
    assert response.text.startswith("HEADER")
    assert "7RPZ" in response.text
    assert len(response.text) == len(demo_pdb_bytes.decode("utf-8"))


def test_unknown_target_returns_404(client):
    assert client.get("/api/targets/does-not-exist").status_code == 404


def test_invalid_pdb_id_shape_is_rejected(client):
    response = client.post("/api/targets/pdb-id", json={"pdb_id": "NOT-A-PDB-ID"})
    assert response.status_code == 422
    assert "4-character" in response.json()["detail"]["message"]


def test_pdb_id_route_serves_the_demo_id_without_network(client):
    """7RPZ resolves to the bundled fixture, so the demo never needs wifi."""
    response = client.post("/api/targets/pdb-id", json={"pdb_id": "7rpz"})
    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "pdb_id"
    assert body["pdb_id"] == "7RPZ"
    assert body["atom_count"] == 1690


def test_engines_endpoint_reports_real_and_mocked(client):
    engines = client.get("/api/engines").json()
    by_key = {engine["key"]: engine for engine in engines}

    assert by_key["sa"]["is_mocked"] is False
    assert "RDKit" in by_key["sa"]["backend"]
    for key in ("screening", "admet", "affinity", "kinetics"):
        assert by_key[key]["is_mocked"] is True
        assert by_key[key]["note"]
