"""Structure parsing and input validation (backend/targets/structure.py)."""
from __future__ import annotations

import pytest

from backend.targets import structure as S


# ---------------------------------------------------------------------------
# Valid input
# ---------------------------------------------------------------------------
def test_valid_pdb_is_accepted(demo_pdb_bytes):
    result = S.parse_structure(demo_pdb_bytes, "demo_kras_g12d_7rpz.pdb")
    assert result.ok
    assert all(check.passed for check in result.checks)
    assert {c.key for c in result.checks} == {
        "readable", "atoms", "coordinates", "chains", "residues",
    }


def test_metadata_is_extracted_from_the_file(demo_pdb_bytes):
    """Values must come from the file's own records, not from constants."""
    result = S.parse_structure(demo_pdb_bytes, "demo.pdb")

    assert result.structure_format == "pdb"
    assert result.pdb_id_in_file == "7RPZ"
    assert "KRAS G12D" in result.title
    assert result.experiment_method == "X-RAY DIFFRACTION"
    assert result.resolution_a == pytest.approx(1.30)

    assert [c.chain_id for c in result.chains] == ["A"]
    chain = result.chains[0]
    assert chain.residue_count == 168
    assert chain.first_residue == 1
    assert chain.last_residue == 169

    assert result.residue_count == 168
    assert result.atom_count == 1690
    assert result.hetatm_count == 348
    assert result.ligands == ["6IC", "GDP", "MG"]
    assert "HOH" in result.solvent_residues
    assert result.warnings == []


def test_checksum_is_stable_and_content_dependent(demo_pdb_bytes):
    first = S.parse_structure(demo_pdb_bytes, "a.pdb")
    second = S.parse_structure(demo_pdb_bytes, "b.pdb")
    altered = S.parse_structure(demo_pdb_bytes + b"REMARK extra\n", "c.pdb")

    assert first.checksum == second.checksum
    assert first.checksum != altered.checksum


def test_mmcif_atom_site_loop_is_parsed():
    cif = """data_TEST
_entry.id TEST
_struct.title 'Minimal test structure'
_exptl.method 'X-RAY DIFFRACTION'
_refine.ls_d_res_high 2.10
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.label_atom_id
_atom_site.label_comp_id
_atom_site.auth_asym_id
_atom_site.auth_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
ATOM 1 N ALA A 1 1.000 2.000 3.000
ATOM 2 CA ALA A 1 2.000 3.000 4.000
ATOM 3 N GLY B 2 5.000 6.000 7.000
HETATM 4 MG MG B 3 8.000 9.000 1.000
"""
    result = S.parse_structure(cif, "test.cif")
    assert result.ok
    assert result.structure_format == "mmcif"
    assert result.title == "Minimal test structure"
    assert result.resolution_a == pytest.approx(2.10)
    assert [c.chain_id for c in result.chains] == ["A", "B"]
    assert result.atom_count == 4
    assert result.ligands == ["MG"]


def test_only_first_model_is_counted():
    """Multi-model (NMR-style) files describe one structure, not a sum."""
    pdb = (
        "MODEL        1\n"
        "ATOM      1  N   ALA A   1       1.000   2.000   3.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       2.000   3.000   4.000  1.00  0.00           C\n"
        "ENDMDL\n"
        "MODEL        2\n"
        "ATOM      3  N   ALA A   1       1.100   2.100   3.100  1.00  0.00           N\n"
        "ATOM      4  CA  ALA A   1       2.100   3.100   4.100  1.00  0.00           C\n"
        "ENDMDL\n"
        "END\n"
    )
    result = S.parse_structure(pdb, "nmr.pdb")
    assert result.ok
    assert result.atom_count == 2


def test_alternate_conformers_are_not_double_counted():
    pdb = (
        "ATOM      1  N   ALA A   1       1.000   2.000   3.000  1.00  0.00           N\n"
        "ATOM      2  CA AALA A   1       2.000   3.000   4.000  0.60  0.00           C\n"
        "ATOM      3  CA BALA A   1       2.100   3.100   4.100  0.40  0.00           C\n"
    )
    result = S.parse_structure(pdb, "altloc.pdb")
    assert result.ok
    assert result.atom_count == 2  # altLoc B excluded


# ---------------------------------------------------------------------------
# Rejected input — each failure must name the gate that failed
# ---------------------------------------------------------------------------
def _failed_check(result):
    return next(check for check in result.checks if not check.passed)


def test_empty_file_is_rejected():
    result = S.parse_structure(b"", "empty.pdb")
    assert not result.ok
    assert _failed_check(result).key == "readable"
    assert "empty" in result.error_detail.lower()


def test_unsupported_extension_is_rejected():
    result = S.parse_structure(b"ATOM", "structure.txt")
    assert not result.ok
    assert _failed_check(result).key == "readable"
    assert ".pdb" in result.error_detail


def test_file_without_atom_records_is_rejected():
    result = S.parse_structure(
        b"HEADER    TEST\nREMARK  no coordinates here\nEND\n", "noatoms.pdb"
    )
    assert not result.ok
    assert _failed_check(result).key == "atoms"
    assert "no atom" in result.error_detail.lower()


def test_malformed_coordinates_are_rejected():
    bad = "ATOM      1  N   ALA A   1      XX.XXX  10.000  10.000  1.00  0.00           N\n"
    result = S.parse_structure(bad, "bad.pdb")
    assert not result.ok
    assert _failed_check(result).key == "atoms"
    assert "line 1" in result.error_detail


def test_truncated_atom_record_is_rejected():
    result = S.parse_structure("ATOM      1  N   ALA A   1\n", "short.pdb")
    assert not result.ok
    assert "54" in result.error_detail  # names the column requirement


def test_degenerate_coordinates_are_rejected():
    """All-identical coordinates mean there is no usable geometry."""
    same = "".join(
        f"ATOM  {i:>5}  CA  ALA A {i:>3}       0.000   0.000   0.000  1.00  0.00           C\n"
        for i in range(1, 6)
    )
    result = S.parse_structure(same, "flat.pdb")
    assert not result.ok
    assert _failed_check(result).key == "coordinates"


def test_detect_format():
    assert S.detect_format("x.pdb") == "pdb"
    assert S.detect_format("x.ent") == "pdb"
    assert S.detect_format("x.cif") == "mmcif"
    assert S.detect_format("x.mmcif") == "mmcif"
    assert S.detect_format("x.txt") is None
    assert S.detect_format(None) is None
