# Deployment runbook — company Linux server

Goal of the server visit: **verify, configure the licence, start, validate.** No core development.
Everything below has a local counterpart that was run on the development machine; steps that can
only be completed on the server are marked **[SERVER ONLY]**.

## 0. What is in the box

| Component | Where it comes from | In the Docker image? |
|---|---|---|
| App (FastAPI + UI + workflow engine) | this repo | yes |
| PostgreSQL 16 | `docker-compose.yml` | separate container |
| AutoDock Vina 1.2.7 (Apache-2.0) | downloaded in the Dockerfile, SHA-256 pinned | yes |
| RDKit, Meeko, PDBFixer, ADMET-AI (CPU torch) | `requirements.txt` | yes |
| BioSolveIT FlexX / HYDE / FastGrow | vendor download (login on biosolveit.de) | **no** (vendor licence) |
| BioSolveIT licence file | vendor | **never** (bind-mounted at runtime) |
| Koffee (affinity, kinetics) | deferred | stages are labelled placeholders |

## 1. Prerequisites (verify before changing anything)

```bash
uname -m && cat /etc/os-release | head -3        # expect x86_64 Linux
docker --version && docker compose version       # Docker 24+ with the Compose plugin
nproc && free -g && df -h /var/lib/docker /opt   # >= 8 cores, >= 16 GB RAM, >= 20 GB free advised
nvidia-smi || echo "no NVIDIA driver (fine: no tool here uses a GPU)"
curl -sI https://files.docking.org | head -1     # ZINC reachable? If not, use library_source=demo/upload
```

## 2. BioSolveIT tools  **[SERVER ONLY for the company licence]**

Put the vendor tarballs and the `.lic` in one folder and run the installer — it unpacks each tool, fixes the
vendor's `0700` folder modes, copies the licence to a protected folder (contents never displayed), downloads the
checksum-pinned Vina, and verifies everything with the vendors' own `--license-info`:

```bash
./scripts/install_tools.sh /path/to/folder-with-tarballs-and-lic
#  tools   -> ~/.local/share/molecular-tools/<tool>/     (TOOLS_DIR overrides)
#  licence -> ~/.config/biosolveit/*.lic                  (BIOSOLVE_LICENSE_DIR overrides)
# On the server use system paths: TOOLS_DIR=/opt/molecular-tools BIOSOLVE_LICENSE_DIR=/etc/biosolveit sudo -E ./scripts/install_tools.sh <src>
```
Expect `>> Valid key, expires on ...` for flexx, hydescorer and fastgrow. The installer refuses a tarball that does
not contain the expected executable (the first "FlexX" download was a copy of FastGrow). Native runs find these
locations automatically; Docker mounts them via `TOOLS_HOST_DIR` / `LICENSE_HOST_DIR` (same defaults).
`TOOLS_HOST_DIR` must point at the **unpacked** tools, never at the folder holding the tarballs (then every vendor
tool shows "not installed").

The development licence (`license_2w.lic`, `HOSTID=ANY`) expires **21-Oct-2026**. Ask BioSolveIT for one valid on the
submission date; never assume a machine-bound licence moves between machines. If a shared library is missing, send
the message to support@biosolveit.com as the vendor asks.

## 3. Configure and start

```bash
git clone <repo> && cd molecular-discovery-mvp
cp .env.prod.example .env.prod
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env.prod
# edit .env.prod: TOOLS_HOST_DIR, LICENSE_HOST_DIR, MOUNT_OPTS (ro,z on SELinux), BIND_ADDRESS, CPU_BUDGET
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod"
$C build && $C up -d
$C ps                                   # postgres + app: healthy
```
(Compose files: `docker-compose.yml` shared base + `docker-compose.prod.yml`; the VPS and local-dev variants are
`docker-compose.vps.yml` / `docker-compose.dev.yml` — see `docs/VPS.md`.) In the commands below, replace
`docker compose` with `$C` when a service name is used.
Migrations run automatically at start (`alembic upgrade head`). Artifacts live in the `mdp_data`
volume (`/var/lib/mdp`: `runs/<campaign>/…`, uploaded structures, ZINC cache).

Mount points (see `docker-compose.prod.yml`): `/opt/molecular-tools` and `/etc/biosolveit` are mounted
**read-only**; on SELinux hosts (Fedora/RHEL, `getenforce` = Enforcing) set `MOUNT_OPTS=ro,z` in `.env.prod`, otherwise the container
gets `Permission denied` (this happened in local testing); override the host paths with `TOOLS_HOST_DIR` / `LICENSE_HOST_DIR` in `.env.prod`.

Without Docker: `python -m venv .venv && .venv/bin/pip install --extra-index-url
https://download.pytorch.org/whl/cpu -c constraints.txt -r requirements.txt`, put the variables from
`.env.example` in the service environment, and run `python -m backend.entrypoint` under systemd with
`KillMode=control-group` (so tool subprocesses die with the service).

## 4. Validate (about 10 minutes)

```bash
curl -s localhost:8008/ready                       # {"status":"ready"...}
curl -s localhost:8008/api/tools | python3 -m json.tool   # vina/hyde/flexx: available:true, license_ok:true
$C exec app python scripts/verify_tools.py   # runs each REAL tool; FlexX shows SKIP only if not installed
python scripts/run_campaign.py --base http://127.0.0.1:8008 --engine flexx --count 20 --label "server acceptance (FlexX)"
python scripts/run_campaign.py --base http://127.0.0.1:8008 --engine vina  --count 20 --label "server acceptance (Vina)"
```
Pass criteria: both campaigns reach `COMPLETE`; `scripts/verify_tools.py` shows PASS for Vina, FlexX, HYDE and ADMET
(from a dev checkout, `pytest -m real tests/integration` is the same evidence); the SDF opens in RDKit/PyMOL with 3D poses.
FlexX 6.5.3 was validated locally; a different FlexX build
may name output poses differently (`science/engines/flexx.py::parse_flexx_output`).

Offline server: run `python scripts/prefetch_zinc.py` once somewhere with internet and copy `<DATA_DIR>/library_cache`
(or use `library_source=demo/upload`). Optional analogue generation uses `FASTGROW_DB=/path/to/FastGrowDB_BioSolveIT_120k.fastgrowdb` when you have downloaded
BioSolveIT's library (login-gated); otherwise a fragment DB is derived automatically. Resource-safety knobs: `CPU_BUDGET`, `MAX_PARALLEL_BATCHES`, `BATCH_TIMEOUT_SECONDS`. One campaign runs
at a time; others queue.

## 5. Operate

* Logs: `docker compose logs -f app` (JSON, with `job_id`/`stage`). Per-batch tool logs are in the UI
  (Batch → *log*) and on disk under `runs/<campaign>/<stage>/batch_NNN/`.
* Restart safe: `docker compose restart app` mid-campaign resumes it from saved batches; leftover tool
  processes from a hard kill are terminated at start.
* Backup: `docker compose exec postgres pg_dump -U mdp molecular_discovery > backup.sql` and
  `docker run --rm -v molecular-discovery_mdp_data:/d -v $PWD:/b alpine tar czf /b/mdp_data.tgz -C /d .`
* Rollback: `git checkout <previous tag> && docker compose up -d --build`. The schema only moves forward
  with `alembic upgrade`; to roll the schema back too: `docker compose run --rm app alembic downgrade 0001`
  (0002 only adds tables/columns). Destructive reset (deletes all data): `docker compose down -v`.

## 6. Security notes

Uploads are size-limited and parsed, never executed; tool arguments are argv lists (no shell); child
processes get a minimal environment (no `DATABASE_URL`); the licence path, never its content, is passed to
the vendor tool; `/api/tools` shows tool names/versions only; the container is read-only, non-root,
`cap_drop: ALL`. The API has **no authentication** — put it behind the company reverse proxy / SSO.
