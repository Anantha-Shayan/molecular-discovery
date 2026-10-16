#!/usr/bin/env python3
"""Run each integrated tool for real on the bundled 7RPZ fixture and report PASS/FAIL/SKIP.

Works inside the production image (no pytest needed):
    docker compose ... exec app python scripts/verify_tools.py
Exit code 1 if any tool that IS installed fails; missing tools are reported as SKIP.
"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import APP_ROOT, settings  # noqa: E402
from backend.science import receptor as recep  # noqa: E402
from backend.science import site as sitelib  # noqa: E402
from backend.science.engines.common import DockInput, DockSettings  # noqa: E402
from backend.tools import registry  # noqa: E402

PDB = APP_ROOT / "data" / "fixtures" / "demo_kras_g12d_7rpz.pdb"
SMILES = ["CC(=O)Oc1ccccc1C(=O)O", "CC(C)Cc1ccc(cc1)C(C)C(=O)O"]
results = []


def step(name, fn, needs=None):
    if needs and not registry.get(needs).available:
        results.append((name, "SKIP", registry.get(needs).reason or "unavailable"))
        return None
    t = time.time()
    try:
        out = fn()
        results.append((name, "PASS", f"{time.time() - t:.0f}s {out or ''}"))
        return out
    except Exception as exc:  # noqa: BLE001
        results.append((name, "FAIL", f"{exc.__class__.__name__}: {exc}"[:300]))
        return None


def main():
    tmp = Path(tempfile.mkdtemp(prefix="verify-"))
    ref = sitelib.find_site(PDB, "6IC:A:202")
    box = sitelib.box_from_ligand(ref)
    recep.write_chain_pdb(PDB, tmp / "protein.pdb", "A", ref)
    recep.write_reference_ligand(PDB, tmp / "ref_ligand.sdf", ref)
    items = [DockInput(str(i), f"V{i}", s) for i, s in enumerate(SMILES)]
    cfg = DockSettings(cpu=4, exhaustiveness=4, num_modes=3)
    poses = []

    for key, st in registry.tool_statuses().items():
        if key == "flexx":
            continue   # local FlexX is superseded by HPSee (legacy campaigns only)
        results.append((f"{key} detected", "PASS" if st.available else "SKIP",
                        (st.version or "") + " " + (st.license_detail or st.reason or "")))

    def vina():
        from backend.science.engines.vina import VinaEngine
        rec = recep.prepare_vina_receptor(tmp / "protein.pdb", tmp, box["center"], box["size"])
        res = VinaEngine().dock_batch(items, receptor_pdbqt=rec, center=box["center"], size=box["size"],
                                      workdir=tmp / "v", log_path=tmp / "v.log", cfg=cfg)
        assert all(r.ok for r in res), [r.error for r in res]
        poses.extend(r.pose_sdf for r in res)
        return f"scores {[r.score for r in res]}"
    step("Vina docking", vina, "vina")

    # BioSolveIT HPSee replaces local FlexX for new campaigns: a real 2-molecule docking + HYDE workflow.
    from backend.science.engines import hpsee as H
    hst = H.status(refresh=True)
    results.append(("HPSee connection", "PASS" if hst.authenticated else ("SKIP" if not hst.configured else "FAIL"),
                    f"{hst.version or ''} " + (hst.reason or "licensed: " + ", ".join(k for k, v in hst.licenses.items() if v["licensed"]))))
    if not (hst.docking_ready and hst.scoring_ready):
        results.append(("HPSee docking + HYDE", "SKIP", hst.reason or "not configured"))
    else:
        def hpsee():
            from rdkit import Chem
            from backend.science.engines.common import write_3d_library
            c = H.HPSeeClient()
            write_3d_library(items, tmp / "hp_lib.sdf", 42)

            def run(info):
                wid = info["workflowId"]
                for _ in range(360):
                    info = c.workflow(wid)
                    if info["status"] in H.TERMINAL:
                        break
                    time.sleep(5)
                assert info["status"] == H.SUCCESSFUL, f"workflow {wid}: {info.get('statusDescription')}"
                return [H.parse_entry(e["entryId"], e["entry"]) for e in c.library_entries(info["result"]["libraryId"], limit=200)]
            docked = run(c.start_docking(tmp / "protein.pdb", tmp / "ref_ligand.sdf", tmp / "hp_lib.sdf",
                                         description="MDP verify_tools (safe to delete)"))
            assert docked and all(e.docking_score is not None for e in docked)
            (tmp / "hp_poses.sdf").write_text("".join(e.record for e in docked))
            scored = run(c.start_scoring(tmp / "protein.pdb", tmp / "ref_ligand.sdf", tmp / "hp_poses.sdf",
                                         description="MDP verify_tools (safe to delete)"))
            assert scored and all(e.hyde and e.hyde.affinity_lower_nm for e in scored)
            return f"{len(docked)} poses, best FlexX {min(e.docking_score for e in docked)}; {len(scored)} HYDE-scored"
        step("HPSee docking + HYDE", hpsee)

    def hyde():
        from backend.science.engines.hyde import HydeEngine
        assert poses, "needs the Vina step to have produced poses"
        (tmp / "poses.sdf").write_text("".join(poses))
        out = HydeEngine().score(tmp / "poses.sdf", protein_pdb=tmp / "protein.pdb", ref_ligand_sdf=tmp / "ref_ligand.sdf",
                                 workdir=tmp, log_path=tmp / "h.log", threads=2, timeout=300)
        assert out, "HYDE scored nothing"
        return f"{len(out)} scored"
    step("HYDE re-scoring", hyde, "hydescorer")

    def fastgrow():
        from rdkit import Chem
        from backend.science.engines import fastgrow as fg
        assert poses, "needs the Vina step to have produced poses"
        pose = Chem.AddHs(Chem.MolFromMolBlock(poses[0].split("$$$$")[0].split("M  END")[0] + "M  END\n", removeHs=False), addCoords=True)
        pool = ["CC(=O)Nc1ccc(O)cc1", "CC(C)Cc1ccc(cc1)C(C)C(=O)O", "COc1ccc(CCN)cc1", "OCCN1CCOCC1", "CN1CCN(CC1)c1ccccc1",
                "NC(=O)c1ccccn1", "CCOC(=O)c1ccc(N)cc1", "Cc1ccc(S(N)(=O)=O)cc1", "OC(=O)CCc1ccccc1", "c1ccc2[nH]ccc2c1",
                "O=C(O)c1ccccc1O", "CCN(CC)CC", "CC1=CC=C(C=C1)C(=O)N", "FC(F)(F)c1ccccc1", "ClC1=CC=CC=C1",
                "COC(=O)c1ccccc1", "CC(N)C(=O)O", "c1ccncc1C(=O)N", "CC(C)(C)c1ccccc1", "OC1CCCCC1", "NC1CCCC1"]
        db, n = fg.build_fragment_db(pool, tmp / "fgdb", tmp / "fgdb.log", threads=2)
        evs = fg.choose_exit_vectors(pose, tmp / "protein.pdb", n=3)
        total = 0
        for k, ev in enumerate(evs):
            cp = tmp / f"core{k}.sdf"
            w = Chem.SDWriter(str(cp)); w.write(fg.core_with_linker(pose, ev)); w.close()
            total += len(fg.grow(cp, tmp / "protein.pdb", db, tmp / f"g{k}.sdf", tmp / f"g{k}.log", 5, 2, 300))
        return f"{n} fragments, {len(evs)} exit vectors, {total} grown molecules"
    step("FastGrow growing", fastgrow, "fastgrow")

    def admet():
        from backend.science import admet as m
        r = m.predict(SMILES, threads=2)
        assert r[0]["values"]["HIA_Hou"] > 0.5
        return r[0]["profile"]
    step("ADMET-AI", admet)

    if not settings.chemairs_configured:
        results.append(("ChemAIRS SAScore", "SKIP", "CHEMAIRS_BASE_URL / CHEMAIRS_API_KEY not set (using RDKit SAscore)"))
    else:
        def chemairs():
            from backend.stages import sa_scoring
            out = sa_scoring.score_batch_for_stage(SMILES)
            assert all("error" not in r for r in out), out
            # PRINTED, not just asserted: read these numbers by eye. The documentation never states
            # the SAScore scale/direction, so this is the first live evidence of what it actually means.
            for smi, r in zip(SMILES, out):
                print(f"    {smi}  ->  sa_score={r['sa_score']}  feasibility={r.get('chemairs_feasibility_score')}  remark={r.get('chemairs_remark')!r}")
            return f"{len(out)} scored via {sa_scoring.CHEMAIRS_METHOD} -- READ THE SCORES ABOVE BY EYE"
        step("ChemAIRS SAScore", chemairs)

    width = max(len(n) for n, _, _ in results)
    for name, status, detail in results:
        print(f"{status:5} {name:<{width}}  {detail}")
    sys.exit(1 if any(s == "FAIL" for _, s, _ in results) else 0)


if __name__ == "__main__":
    main()
