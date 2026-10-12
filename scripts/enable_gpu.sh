#!/usr/bin/env bash
# Switch this checkout's PyTorch from the CPU build to the CUDA 12.6 build so ADMET-AI uses the NVIDIA GPU.
# Prerequisite: the proprietary NVIDIA driver works (`nvidia-smi` prints your GPU) — see docs/GPU.md.
# Reversible:  .venv/bin/pip install --force-reinstall torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu
set -euo pipefail
cd "$(dirname "$0")/.."
command -v nvidia-smi >/dev/null || { echo "nvidia-smi not found: install the NVIDIA driver first (docs/GPU.md)." >&2; exit 1; }
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
.venv/bin/pip install --force-reinstall "torch==2.14.1" \
  --index-url https://download.pytorch.org/whl/cu126 --extra-index-url https://pypi.org/simple
.venv/bin/python scripts/gpu_check.py --bench
