# GPU (NVIDIA RTX 4050) — status and setup

## Status on this machine (checked 2026-10-10)

| Check | Result |
|---|---|
| GPU | RTX 4050 Max-Q (AD107M), hybrid laptop with Intel graphics |
| Driver in use | **`nouveau`** (open-source, no CUDA). `nvidia-smi` is not installed |
| Secure Boot | **enabled** → a proprietary driver's kernel module must be signed and enrolled (MOK) |
| PyTorch | `2.14.1+cpu` (CPU build) |
| **GPU used by the platform** | **No** |

## What a GPU could and could not speed up

| Component | Uses GPU? |
|---|---|
| ADMET-AI (PyTorch) | Yes — automatically once `torch.cuda.is_available()`; **but** it already scores ~35 molecules/s on CPU, so the gain is small |
| AutoDock Vina 1.2.7, FlexX, HYDE, FastGrow, RDKit | No — CPU programs. Docking dominates run time, so a GPU does not shorten campaigns |

GPU re-implementations of Vina exist (Vina-GPU, QuickVina-GPU). They are separate programs with their own search
algorithms and scoring behaviour, so adopting one would need its own validation (re-docking benchmark) and is **not**
done here; the validated Vina 1.2.7 result stays the reference.

## Enable it (needs your sudo password and a reboot; I cannot do these steps for you)

Run in order (in Claude Code prefix each line with `!`, or use a normal terminal):

```bash
# 1. RPM Fusion (Fedora) repositories
sudo dnf install -y \
  https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm \
  https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm
# 2. Driver + CUDA support library (builds a kernel module; wait until it finishes, ~5 min)
sudo dnf install -y akmod-nvidia xorg-x11-drv-nvidia-cuda
# 3. Secure Boot: create/enroll a signing key (you will set a one-time password)
sudo kmodgenca -a
sudo mokutil --import /etc/pki/akmods/certs/public_key.der
# 4. Check the module was built before rebooting
modinfo -F version nvidia          # must print a version, not an error
# 5. Reboot; at the blue "MOK management" screen choose Enroll MOK -> Continue -> enter the password
reboot
```

After the reboot:

```bash
nvidia-smi                          # must show the RTX 4050
./scripts/enable_gpu.sh             # swaps in CUDA PyTorch, prints a CPU/GPU ADMET timing
.venv/bin/python scripts/gpu_check.py
```

Docker: install the NVIDIA Container Toolkit, then add `-f docker-compose.gpu.yml` (builds the image with
`TORCH_VARIANT=cu126`). Not tested here, because no driver is present.

If the driver install fails on this very new kernel (7.1), staying on `nouveau` + CPU loses nothing the platform
needs — keep the CPU setup; every result in the acceptance report was produced on CPU.
