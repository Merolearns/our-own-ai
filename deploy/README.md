# deploy/ — CI, deploy, and backup files for the agent platform

Owner: Builder C (Codex, ops). Nothing outside `.github/`, `deploy/`, and
`reviews/` is touched from here.

## Files

| File | What it does |
|---|---|
| `.github/workflows/ci.yml` | CI on pull requests and pushes to `main`: Python 3.12, `pip install -r requirements.txt`, `ruff check .` (pinned 0.16.10 — the version the code was verified clean against), `pytest -q`. No secrets — tests use fake LLM clients. Red fails the build and blocks deploy. |
| `.github/workflows/measure-vps.yml` | One-off read-only host measurement via the `slate-vps` self-hosted runner (free/df/docker stats). Diagnostic: proves whether that runner is visible to this repo. Nothing is written. |
| `.github/workflows/deploy.yml` | Manual deploy (workflow_dispatch only). SSH setup, repo sync at the exact SHA, .env written from secrets, compose copied, then `deploy-remote.sh` on the VPS. Fails loudly on any missing required secret, failed health check, failed smoke test, or low disk. |
| `deploy/deploy-remote.sh` | The VPS-side deploy script: RAM gate (>= 3.5 GB free, counting a running agent-platform container's RAM as reclaimable on a redeploy, or abort with numbers), AGENT_SECRET generation on first install, `docker compose build` + `up -d`, /healthz poll up to 10 min, smoke test, backup cron install, disk guard. Never touches SLATE's files. |
| `deploy/smoke-test.py` | End-to-end smoke run on the VPS: /healthz, login as demo, demo banner, refund-policy question ($50 with sources, never $500), #1003 injection note (no ShipFast), then the Scoopy host (same container, Host header): Scoopy branding, $60 pricing answer, per-frequency-price refusal (not published, never a quoted price), callback ticket. Stdlib only; never prints a secret. |
| `deploy/docker-compose.yml` | The VPS Compose project (`/srv/agent-platform/docker-compose.yml`). One service, built ON THE VPS from the repo checkout (deliberately not in Actions — the Actions budget is nearly exhausted), gunicorn on `:8000` inside the container, host port 8001 bound to loopback only, named volume `agent-data:/data`, `mem_limit: 3g`, `cpus: 1.0`, `restart: unless-stopped`, env file `/srv/agent-platform/.env`, `/healthz` healthcheck. One container serves BOTH agents (host routing in `app.py`); there is deliberately no second service — the 8 GB box cannot hold two app processes at peak. |
| `deploy/caddy-snippet` | Caddy routes for `agents.40.160.149.104.nip.io` AND `scoopy.40.160.149.104.nip.io`, both reverse-proxying to the same container (`agent-platform:8000`), kept separate from SLATE's routes. One-time manual append to the VPS Caddyfile + `caddy validate` before reload — a bad Caddyfile takes down SLATE too. |
| `deploy/backup.sh` | Nightly off-box backup (SQLite snapshot -> Cloudflare R2 via awscli v2, upload verified by size, fail-closed). Runs on the VPS via cron (03:30 UTC, offset from SLATE's 03:00). R2 credentials come from env only — never in this file or the repo. DB-only: the chunks table lives in the same SQLite file and /data/models is re-downloadable. |
| `deploy/CHECKLIST.md` | The first-deploy checklist: the two parent-side browser actions (workflow files + repo secrets), what the deploy does, the Caddy step, going live-model, backup status. |

## Deploy flow (manual trigger)

1. Parent adds the three workflow files in the repo web UI (the PATs lack
   the Workflows permission — see CHECKLIST.md) and sets the three required
   repo secrets (`VPS_SSH_KEY`, `DEMO_CUSTOMER_PASSWORD`,
   `DEMO_STAFF_PASSWORD`).
2. Parent runs the `deploy` workflow (Actions tab -> Run workflow).
3. The workflow SSHes in (same mechanism as SLATE's deploy-workers.yml),
   syncs the repo to `/srv/agent-platform/repo` at the exact SHA, writes
   `/srv/agent-platform/.env` from secrets (mode 600, never printed),
   copies `docker-compose.yml`, and runs `deploy-remote.sh`.
4. The remote script gates on RAM, builds the image on the VPS, starts the
   container, polls /healthz (up to 10 min for the first model download),
   runs the smoke test, installs the backup cron, and checks disk.
5. Caddy route is a one-time manual append (see `caddy-snippet`); until it
   exists the app is verified over SSH only, not via a public URL.

Direct SSH from the dev VM to the VPS is blocked by the egress proxy;
deploys go through GitHub Actions only. Nothing in this directory deploys
anything by itself.

## Secrets

Never in the repo, never in any file here. At deploy time, GitHub Actions
secrets are written to `/srv/agent-platform/.env` on the VPS:
`DEMO_CUSTOMER_PASSWORD`, `DEMO_STAFF_PASSWORD` (required),
`DEEPSEEK_API_KEY`, `GEMINI_API_KEY` (optional — demo mode without them),
plus the backup credentials `BACKUP_R2_ENDPOINT`, `BACKUP_R2_BUCKET`,
`BACKUP_R2_ACCESS_KEY_ID`, `BACKUP_R2_SECRET_ACCESS_KEY`. `AGENT_SECRET`
is generated on first install by the deploy script and persisted in the
VPS .env. The `.env` file is not committed anywhere. Google OAuth for
Scoopy (`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`) is NOT
written by the deploy workflow — its .env list is fixed (see
CHECKLIST.md §5b) — set the pair directly in the VPS .env.

## Backups — MANDATORY before real users

Nightly off-box backups are mandatory before any real user touches the app
(research/05 §5 step 7). A disk failure without them loses everything, and
SLATE's own backup cron has no verified successful run — do not assume a
backup works until it is verified AND a restore has been tested once.

Restore procedure (tested locally Nov 8, 2026 — snapshot, integrity_check,
restore to a scratch DB, row counts match; the R2 upload leg still needs the
`BACKUP_R2_*` secrets before it can be proven end to end):
1. Download a backup set from R2 (`app.db` for one timestamp).
2. `python3 -c "import sqlite3; sqlite3.connect('file:app.db?mode=ro', uri=True).execute('PRAGMA integrity_check').fetchall()"` — must return `ok`.
3. Stop the app container, copy the DB into a scratch copy of the
   `agent-data` volume, start a scratch container from the same image
   against that volume, and confirm `/healthz` and a login work. Only then
   is the backup trusted.
