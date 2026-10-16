from __future__ import annotations

import dataclasses
import tarfile
from pathlib import Path

import pytest

from backend.science.engines import koffee
from backend.tools import proc
from backend.campaign import params, results


def _protein(path: Path) -> Path:
    lines = []
    for i in range(1, 22):
        atom, element = (" H  ", " H") if i == 1 else (" CA ", " C")
        lines.append(f"ATOM  {i:5d} {atom} ALA A   1    {i:8.3f}{0:8.3f}{0:8.3f}  1.00  0.00          {element}")
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def _pose(path: Path, name: str = "pose") -> Path:
    from rdkit import Chem
    from rdkit.Chem import AllChem
    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    mol.SetProp("_Name", name)
    writer = Chem.SDWriter(str(path)); writer.write(mol); writer.close()
    return path


def test_archive_is_safe_and_has_stable_explicit_mapping(tmp_path):
    protein = _protein(tmp_path / "protein.pdb")
    one, two = _pose(tmp_path / "one.sdf"), _pose(tmp_path / "two.sdf")
    archive, mapping = koffee.build_input_archive(protein, [
        koffee.KoffeeInput("candidate/one", "one", one),
        koffee.KoffeeInput("candidate two", "two", two),
    ], tmp_path / "run")
    assert list(mapping.values()) == ["candidate/one", "candidate two"]
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert "molecules.tsv" in names and "protein.pdb" in names
        assert all(not n.startswith("/") and ".." not in Path(n).parts for n in names)
        rows = tar.extractfile("molecules.tsv").read().decode().splitlines()
    assert rows[0] == "protein\tligand"
    assert rows[1].split("\t")[1] in mapping


def test_archive_rejects_pose_without_explicit_hydrogens(tmp_path):
    from rdkit import Chem
    from rdkit.Chem import AllChem
    mol = Chem.MolFromSmiles("CCO")
    AllChem.EmbedMolecule(mol, randomSeed=7)
    pose = tmp_path / "no_h.sdf"
    writer = Chem.SDWriter(str(pose)); writer.write(mol); writer.close()
    with pytest.raises(koffee.KoffeeInputError, match="explicit hydrogens"):
        koffee.build_input_archive(_protein(tmp_path / "protein.pdb"),
                                   [koffee.KoffeeInput("a", "a", pose)], tmp_path / "run")


def test_results_map_by_ligand_path_and_allow_partial_success(tmp_path):
    mapping = {"ligands/00001_a.sdf": "a", "ligands/00002_b.sdf": "b"}
    output = tmp_path / "results.tsv"
    output.write_text("system_id\tprotein\tligand\tscore_kcal_mol\tligand_strain_kcal_mol\n"
                      "system-a\tprotein.pdb\tligands/00001_a.sdf\t-8.25\t1.5\n")
    found, missing = koffee.parse_results(output, mapping)
    assert found["a"].score_kcal_mol == -8.25
    assert found["a"].ligand_strain_kcal_mol == 1.5
    assert missing == {"b"}


@pytest.mark.parametrize("contents", [
    "protein\tligand\nprotein.pdb\tligands/a.sdf\n",
    "protein\tligand\tscore_kcal_mol\nprotein.pdb\tligands/a.sdf\tnot-a-number\n",
])
def test_results_reject_missing_or_nonnumeric_score(tmp_path, contents):
    path = tmp_path / "results.tsv"; path.write_text(contents)
    with pytest.raises(proc.ToolError):
        koffee.parse_results(path, {"ligands/a.sdf": "a"})


def test_docker_argv_uses_list_and_readonly_license_mount(tmp_path, monkeypatch):
    protein = _protein(tmp_path / "protein.pdb")
    pose = _pose(tmp_path / "pose.sdf")
    license_file = tmp_path / "license.txt"; license_file.write_text("test license")
    seen = {}
    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        work = Path(next(x.split(":", 1)[0] for x in argv if x.endswith(":/host")))
        work.joinpath("results.tsv").write_text(
            "system_id\tprotein\tligand\tscore_kcal_mol\tligand_strain_kcal_mol\n"
            "x\tprotein.pdb\tligands/00001_a.sdf\t-7.0\t\n")
        return proc.ProcResult(0, 0.01, "")
    monkeypatch.setattr(koffee.proc, "run", fake_run)
    monkeypatch.setattr(koffee, "settings", dataclasses.replace(koffee.settings, koffee_license_file=license_file))
    result, missing, _, output = koffee.KoffeeEngine().run(
        protein=protein, inputs=[koffee.KoffeeInput("a", "a", pose)], workdir=tmp_path / "work",
        log_path=tmp_path / "tool.log", cpus=2, timeout=5,
    )
    assert result["a"].score_kcal_mol == -7 and not missing and output.is_file()
    assert isinstance(seen["argv"], list)
    assert "--volume" in seen["argv"] and any(x.endswith(":/license:ro") for x in seen["argv"])
    assert "--cpus" in seen["argv"] and "--no-ligand-strain-in-score" in seen["argv"]
    assert seen["kwargs"]["display_argv"] != seen["argv"]


def test_koffee_is_real_optional_stage_and_metrics_stay_separate():
    p = params.resolve({"enable_affinity": True, "enable_kinetics": False})
    assert ("affinity", True) in params.enabled_stages(p)
    assert params.STAGES["affinity"]["mocked"] is False
    metrics = results.flat_metrics({"affinity": {
        "score_kcal_mol": -9.1, "ligand_strain_kcal_mol": 1.2, "strain_included_in_score": False,
    }})
    assert metrics["koffee_score_kcal_mol"] == -9.1
    assert "koffee_score_kcal_mol" in results.results_csv([]).splitlines()[0]
