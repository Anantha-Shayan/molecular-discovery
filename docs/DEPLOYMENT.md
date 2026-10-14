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
                                                            # (models[].chemairs_sascore: available:true if configured — see §6)
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

## 6. ChemAIRS SAScore API (commercial, optional)

Replaces the local RDKit SAscore for the main pipeline's SA stage with the ChemAIRS commercial API
(`~/Desktop/APIInterfaceDocumentationEN.pdf`, section "1. SAScore API"), when configured. Leaving it
unconfigured is always safe — the app falls back to RDKit automatically, with no other behaviour change.

**Before using it on a real campaign, resolve this open question — the vendor documentation never
states it:** the numeric scale and direction of ChemAIRS's `SAScore` field (RDKit's is 1=easy..10=hard;
every worked example in the vendor PDF uses a placeholder value of `"0"`). Until confirmed, the SA
stage reports ChemAIRS scores with `"scale": "unknown (confirm with ChemAIRS before trusting sa_limit
filtering)"` rather than assuming it matches RDKit — meaning `sa_limit` filtering will likely do the
wrong thing (too strict, too lenient, or backwards) until this is confirmed.

**Steps:**
1. `cp .env.prod.example .env.prod` (if not already done), then set:
   ```
   CHEMAIRS_BASE_URL=https://your-chemairs-domain.example.com   # no trailing slash, no /open/v2 suffix
   CHEMAIRS_API_KEY=sk-...
   ```
2. Restart the app so it picks up the new environment: `$C up -d app` (no rebuild needed — these are
   runtime env vars, not baked into the image).
3. Confirm it's seen: `curl -s localhost:8008/api/tools | python3 -m json.tool` — look for
   `models[].key == "chemairs_sascore"` with `"available": true`.
4. **Run the real verification and read the printed numbers — don't just check PASS/FAIL:**
   ```bash
   $C exec app python scripts/verify_tools.py
   ```
   This prints each test molecule's SMILES alongside its raw `sa_score`, `feasibility`, and `remark`
   from ChemAIRS. Compare a known-easy molecule (e.g. aspirin) against a known-hard one (a heavily
   decorated, multi-ring, stereo-rich molecule) — whichever direction gives the expected easy/hard
   ordering **is** the scale. Also run the same check locally first if possible (same command, no
   Docker): `pytest -m real tests/integration/test_chemairs.py -v -s` — it does the same thing before
   touching the server.
5. Once the scale is confirmed, update `sa_scoring.score_batch_for_stage`'s `"scale"` string in
   `backend/stages/sa_scoring.py` to state it plainly, and double-check the `sa_limit` value used in
   campaign setup still means what you intend on the new scale (it may need inverting or rescaling).
6. Everyday operation after that: nothing — the "sa" pipeline stage uses ChemAIRS automatically whenever
   it's configured, one API task per batch (not one call per molecule), and each stage's recorded `tool`
   name shows which engine actually ran (`ChemAIRS SAScore API` vs `RDKit SAscore (...)`), visible per
   campaign in the UI and via `/api/jobs/{id}`.

If ChemAIRS becomes unreachable mid-deployment (network issue, revoked key, rate limit), the SA stage's
batch fails with the real error message attached (see the batch log in the UI) — it does **not** silently
fall back to RDKit mid-campaign, so a run that was using ChemAIRS stays comparable throughout. Unset the
two variables and restart to switch back to RDKit deliberately.

## 7. Caddy (bundled reverse proxy, optional)

The API has no login of its own, so something must sit in front of it before anyone but you can reach
it. `docker-compose.prod.yml` includes a `caddy` service for this — `docker compose ... up -d` starts it
alongside `app` and `postgres` automatically; only the `Caddyfile` needs editing.

**Pre-flight check — run this before `up -d` if the server might already run a web server (nginx, Apache,
another proxy):**
```bash
sudo ss -ltnp '( sport = :80 or sport = :443 )'
```
- **Empty output** → ports are free, Caddy will start cleanly. Proceed.
- **Something listed** → that port is taken. Docker will then simply fail to start the `caddy` container
  (port already in use) — this does **not** disturb whatever already holds the port, it just means Caddy
  won't come up. Either stop here and use the existing nginx/proxy instead (point it at `127.0.0.1:8008`,
  the app's own binding), or edit the `ports:` lines under `caddy:` in `docker-compose.prod.yml` to use
  different host ports (e.g. `8080:80`) before starting.

**Steps (ports free):**
1. Generate a password hash before starting the stack, then store it in `.env.prod` as
   `CADDY_PASSWORD_HASH`. Keep the value single-quoted so Compose preserves the `$` characters:
   ```bash
   C="docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod"
   $C run --rm --no-deps caddy caddy hash-password
   ```
   The one-off container prompts for the password and prints its hash; it works before Caddy is running.
   Add `CADDY_PASSWORD_HASH='<hash printed above>'` to `.env.prod`.
2. Edit `Caddyfile`: change `:80` to the server's real DNS name if it has one reachable from where
   reviewers will connect (Caddy then provisions HTTPS automatically via Let's Encrypt); otherwise leave
   it as `:80` for plain HTTP on an internal-only hostname/IP.
3. Start the stack: `$C up -d --build` — this brings up `postgres`, `app` and `caddy` together.
4. Visit `http://<server-dns-or-ip>/` (or `https://` if you set a real domain) — log in with the
   `admin` user and the password you hashed in step 1. You should see the app live.

No other file changes are needed; `app` keeps its own `127.0.0.1:8008` binding for direct SSH-tunnelled
debugging, which doesn't conflict with Caddy on 80/443.

## 8. Security notes

Uploads are size-limited and parsed, never executed; tool arguments are argv lists (no shell); child
processes get a minimal environment (no `DATABASE_URL`); the licence path, never its content, is passed to
the vendor tool; `/api/tools` shows tool names/versions only; the container is read-only, non-root,
`cap_drop: ALL`. The API has **no authentication of its own** — §7's Caddy (or the company's existing
nginx/SSO) is what actually protects it; don't skip that step on a reachable network.
