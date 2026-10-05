"""
SDF export.

The structure data in these files is genuine RDKit output — the test
parses it back with RDKit rather than string-matching, so a malformed
molblock would fail. Mocked property values must stay labelled as such
once the file leaves the application.
"""
from __future__ import annotations

import io

from rdkit import Chem


def completed_job(client, **config):
    target = client.post("/api/targets/demo").json()
    response = client.post(
        "/api/jobs",
        json={"target_id": target["target_id"], "stage_delay_seconds": 0, **config},
    )
    return response.json()["job_id"]


def parse_sdf(text):
    supplier = Chem.ForwardSDMolSupplier(io.BytesIO(text.encode()))
    return [mol for mol in supplier if mol is not None]


def test_single_candidate_sdf_is_valid(client):
    job_id = completed_job(client, library_limit=25)
    candidate = client.get(f"/api/jobs/{job_id}/candidates").json()["candidates"][0]

    response = client.get(f"/api/candidates/{candidate['molecule_id']}/sdf")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert candidate["display_id"] in response.headers["content-disposition"]

    mols = parse_sdf(response.text)
    assert len(mols) == 1
    mol = mols[0]
    assert mol.GetNumAtoms() > 0
    assert mol.GetProp("_Name") == candidate["display_id"]
    # The exported structure is the candidate's actual structure.
    assert Chem.MolToSmiles(mol) == Chem.MolToSmiles(
        Chem.MolFromSmiles(candidate["smiles"])
    )


def test_sdf_properties_label_mocked_values(client):
    job_id = completed_job(client, library_limit=25)
    candidate = client.get(f"/api/jobs/{job_id}/candidates").json()["candidates"][0]
    mol = parse_sdf(client.get(f"/api/candidates/{candidate['molecule_id']}/sdf").text)[0]

    props = mol.GetPropsAsDict()

    # Real values carry no mocked suffix.
    assert float(props["SA_SCORE"]) == candidate["sa_score"]
    assert float(props["MOLECULAR_WEIGHT"]) == candidate["molecular_weight"]
    assert "1 (easy)" in props["SA_SCALE"]

    # Mocked values are suffixed, so provenance survives outside the app.
    assert props["ADMET_PROFILE_mocked"] == candidate["admet_profile"]
    assert float(props["KD_NM_mocked"]) == candidate["kd_nm"]
    assert float(props["KOFF_PER_S_mocked"]) == candidate["koff_per_s"]
    assert float(props["RESIDENCE_TIME_MIN_mocked"]) == candidate["residence_time_min"]
    assert "DELTA_G_KCAL_MOL_mocked" in props

    # And an explicit provenance statement travels with the file.
    assert "real (RDKit)" in props["PROVENANCE"]
    assert "mocked" in props["PROVENANCE"]

    assert not any(key.startswith("KD_NM") and not key.endswith("_mocked") for key in props)


def test_job_level_sdf_contains_every_final_candidate(client):
    job_id = completed_job(client, library_limit=25)
    candidates = client.get(f"/api/jobs/{job_id}/candidates").json()["candidates"]

    response = client.get(f"/api/jobs/{job_id}/candidates.sdf")
    assert response.status_code == 200
    assert response.text.count("$$$$") == len(candidates)

    mols = parse_sdf(response.text)
    assert len(mols) == len(candidates)
    assert {mol.GetProp("_Name") for mol in mols} == {c["display_id"] for c in candidates}


def test_job_sdf_artifact_matches_the_endpoint(client):
    """The SDF written to disk during the run is the same real output."""
    import os

    from sqlalchemy import select

    from backend import models
    from backend.database import get_session

    job_id = completed_job(client, library_limit=25)
    with get_session() as session:
        artifact_path = session.execute(
            select(models.Artifact.storage_path).where(
                models.Artifact.job_id == job_id, models.Artifact.stage == "final"
            )
        ).scalars().first()

    assert artifact_path and os.path.exists(artifact_path)
    with open(artifact_path) as handle:
        on_disk = parse_sdf(handle.read())

    from_api = parse_sdf(client.get(f"/api/jobs/{job_id}/candidates.sdf").text)
    assert {m.GetProp("_Name") for m in on_disk} == {m.GetProp("_Name") for m in from_api}


def test_job_sdf_404s_when_there_are_no_candidates(client):
    assert client.get("/api/jobs/unknown-job/candidates.sdf").status_code == 404
