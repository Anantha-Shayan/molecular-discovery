"""
2D structure depiction — real, not mocked.

Generates an actual SVG from the molecule's SMILES via RDKit's drawing
code, so the dashboard's candidate table shows the real structure of
whatever molecule the pipeline produced, rather than a static hand-drawn
icon.
"""
from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D


def sdf_for_candidate(smiles: str, display_id: str, properties: dict) -> str:
    """Build a real, valid SDF record (V2000 molblock + property tags) for
    one candidate. This is genuine RDKit I/O, not a mocked string template
    — only the *property values* inside it (ADMET/affinity/kinetics) are
    mocked, same as everywhere else in this pipeline.

    Record framing matters: in an SDF, every data item is

        > <TAG>
        value
        <blank line>

    and the blank line after the *last* value is required too, before the
    `$$$$` terminator. Omitting it makes multi-record files unreadable —
    RDKit's supplier loses sync and silently skips alternating records —
    so the structure is assembled one complete block at a time rather than
    by joining lines.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Could not parse SMILES: {smiles!r}")
    rdDepictor.Compute2DCoords(mol)
    mol.SetProp("_Name", display_id)

    parts = [Chem.MolToMolBlock(mol)]  # already ends with "M  END\n"
    for key, value in properties.items():
        if value is None:
            continue  # omit rather than writing the string "None"
        parts.append(f"> <{key}>\n{value}\n\n")
    parts.append("$$$$\n")
    return "".join(parts)


def svg_for_smiles(smiles: str, width: int = 140, height: int = 80) -> str:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return ""
    rdDepictor.Compute2DCoords(mol)
    drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
    opts = drawer.drawOptions()
    opts.clearBackground = False
    opts.bondLineWidth = 1
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    svg = drawer.GetDrawingText()
    # Strip the XML header so it can be inlined directly into HTML.
    return svg.replace("<?xml version='1.0' encoding='iso-8859-1'?>\n", "")
