#!/usr/bin/env python3
"""Report honestly whether the NVIDIA GPU is usable and which parts of the platform would use it.

    .venv/bin/python scripts/gpu_check.py          # status
    .venv/bin/python scripts/gpu_check.py --bench  # also time ADMET-AI inference on CPU vs GPU
"""
import shutil
import subprocess
import sys
import time

print("== NVIDIA driver")
smi = shutil.which("nvidia-smi")
if smi:
    out = subprocess.run([smi, "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                          "--format=csv,noheader"], capture_output=True, text=True)
    print("nvidia-smi:", out.stdout.strip() or out.stderr.strip())
else:
    print("nvidia-smi: NOT FOUND (proprietary driver not installed; see docs/GPU.md)")

print("\n== PyTorch (used by ADMET-AI)")
try:
    import torch
    print(f"torch {torch.__version__}, built for CUDA {torch.version.cuda}, cuda.is_available() = {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print("device:", torch.cuda.get_device_name(0))
except Exception as exc:  # noqa: BLE001
    print("torch import failed:", exc)
    torch = None

print("\n== What can use a GPU in this platform")
rows = [
    ("ADMET-AI (PyTorch/Chemprop)", "YES, automatically when torch.cuda.is_available()", "CPU already does ~35 molecules/s"),
    ("AutoDock Vina 1.2.7", "no", "CPU only; GPU forks exist but are separate, unvalidated programs"),
    ("BioSolveIT FlexX / HYDE / FastGrow", "no", "vendor CPU multithreading only"),
    ("RDKit (SA, library, 3D embedding)", "no", "CPU"),
]
for name, gpu, note in rows:
    print(f"  {name:<36} {gpu:<52} {note}")

if "--bench" in sys.argv:
    from backend.science import admet
    smiles = ["CC(=O)Oc1ccccc1C(=O)O", "CC(C)Cc1ccc(cc1)C(C)C(=O)O", "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"] * 100
    admet.predict(smiles[:3])  # warm-up / model load
    t = time.time(); admet.predict(smiles); dt = time.time() - t
    dev = "cuda" if torch is not None and torch.cuda.is_available() else "cpu"
    print(f"\nADMET-AI: {len(smiles)} molecules in {dt:.1f}s on {dev}  ({len(smiles)/dt:.0f} molecules/s)")
