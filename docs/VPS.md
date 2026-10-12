# Running on the VPS (Caddy in another stack)

**Short answer: yes, you can run it on the VPS now** — with AutoDock Vina, RDKit SA scoring and the ADMET-AI model.
FlexX and HYDE are only available if you put the BioSolveIT binaries and licence on the VPS (see "Licensing"
below — check first). Without them the UI shows FlexX/HYDE as "not installed" and the campaign runs
Vina → SA → ADMET, ranked by docking score.

Verified locally for this layout: the image builds; the stack starts on PostgreSQL 16 with migrations applied
(`/ready` reports `schema: current`); a real campaign completes in containers. **Not verified**: the Caddy
hookup on your VPS (I can't reach it) — follow the checks in step 5.

## Before you start (5 minutes)

```bash
uname -m            # must be x86_64 — the bundled Vina binary is x86-64 (an ARM VPS would need a different build)
nproc; free -g      # docking is CPU-bound; the app needs ~2 GB RAM for the ADMET model + room for Vina
df -h /var/lib/docker   # the image is ~3 GB (CPU-only PyTorch included); keep 8 GB free
docker network ls | grep -i default    # find the proxy stack's network, e.g. n8n-stack_default
```

## Deploy

1. Get the code on the VPS and create the env file:
   ```bash
   git clone <repo> molecular-discovery && cd molecular-discovery
   cp .env.vps.example .env.vps
   sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env.vps
   # set CADDY_NETWORK to the network name from `docker network ls` (default: n8n-stack_default)
   # set CPU_BUDGET to about half of nproc; APP_MEMORY_LIMIT to what you can spare (default 4g)
   ```
2. Start:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.vps.yml --env-file .env.vps up -d --build
   docker compose -f docker-compose.yml -f docker-compose.vps.yml --env-file .env.vps ps   # both "healthy"
   ```
   (Shorter: add `COMPOSE_FILE=docker-compose.yml:docker-compose.vps.yml` to `.env.vps` and drop the `-f` flags.)
3. **Protect it — the app has no login of its own.** In the Caddyfile of the proxy stack add a site block with
   basic auth (hash from `docker exec <caddy-container> caddy hash-password`):
   ```caddyfile
   discovery.example.com {
       basicauth {
           admin <paste-bcrypt-hash>
       }
       request_body {
           max_size 130MB          # matches MAX_UPLOAD_MB
       }
       reverse_proxy mdp-app:8008
   }
   ```
   then reload Caddy: `docker exec <caddy-container> caddy reload --config /etc/caddy/Caddyfile`.
   `mdp-app` is the alias this stack gives the app on the proxy network. Point the DNS A-record at the VPS first.
4. Open `https://discovery.example.com`, create a target (the demo target is the quickest), and start a small
   campaign (10–20 molecules, "Quick" effort) to see it work.
5. If the site doesn't load: `docker compose ... logs app`; `docker exec <caddy-container> wget -qO- http://mdp-app:8008/health`
   (tests the proxy-network path); `docker network inspect <CADDY_NETWORK>` should list the app container.

## What the networks look like

```
internet ─ Caddy ──(proxy network: n8n-stack_default)── app ──(molecular-discovery-network, private)── postgres
```
PostgreSQL is only on the private network, never on the proxy network and with no published port. The app publishes
no host port in this layout; Caddy is the only way in.

## Updating an existing deployment

* The private network keeps its original name (`molecular-discovery-network`) so Compose upgrades in place.
* Keep the same `POSTGRES_PASSWORD` as the existing database volume, otherwise the app cannot log in.
* Migration `0002` runs automatically and keeps old rows. Runs from the earlier mocked version stay listed but have no
  stage detail; if you don't need them, a clean start is simpler: `docker compose ... down -v` (**deletes the database
  and all artifacts**).
* Remove the stale `STAGE_DELAY_SECONDS` from the old `.env`; it is no longer used.

## Licensing — read before copying BioSolveIT files to the VPS

The development licence (`license_2w.lic`, `HOSTID=ANY`, expires 21 Oct 2026) was issued for the Molecular Solutions
evaluation. Technically it is not machine-bound, so it would work on any host, but whether you may run it on a personal
VPS is a contractual question I can't answer. Ask BioSolveIT/the company before doing so. If you do: run `./scripts/install_tools.sh <folder-with-tarballs>` on the VPS. The base compose file already mounts
`~/.local/share/molecular-tools` and `~/.config/biosolveit` read-only into the container, so nothing else changes
(`docker compose ... restart app`). Never copy the licence into the repo or the image.

## Limits to expect

One campaign runs at a time; a 60-molecule Vina campaign took 8.6 minutes on 16 threads, so on a small VPS use
10–30 molecules. Library download needs outbound internet (`files.docking.org`); otherwise use the built-in or
uploaded library. ZINC molecules are catalogue compounds, and Koffee affinity/kinetics are still labelled placeholders.
