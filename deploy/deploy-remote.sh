#!/usr/bin/env bash
# Agent platform — VPS-side deploy, run ON the VPS via SSH by the deploy
# workflow (.github/workflows/deploy.yml). Never run from the dev VM.
#
# Usage: deploy-remote.sh <expected-sha>
#
# Steps, each failing loudly (set -euo pipefail; a failure aborts the run
# and the workflow reports it — never silently serve a broken deploy):
#   0. RAM gate: refuse unless >= 3.5 GB is free (app needs ~2.5 GB RSS).
#   1. Ensure AGENT_SECRET exists in /srv/agent-platform/.env (generated
#      on first install, persisted in the .env, never printed).
#   2. docker compose build (from /srv/agent-platform/repo) && up -d.
#   3. Poll /healthz from inside the VPS for up to 10 minutes (first boot
#      downloads ~2.3 GB of models into the /data volume).
#   4. End-to-end smoke: deploy/smoke-test.py (login + refund-policy
#      question, $50 answer with sources, demo banner).
#   5. Install the nightly backup cron if absent.
#   6. Disk guard: refuse to finish if / drops below 5 GB free.
#
# Never touches SLATE's files, routes, or env. Never prints a secret.
# The Caddy route is NOT touched here — one-time manual append, see
# deploy/caddy-snippet and deploy/CHECKLIST.md.

set -euo pipefail

APP_DIR="/srv/agent-platform"
REPO_DIR="$APP_DIR/repo"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"
ENV_FILE="$APP_DIR/.env"
EXPECTED_SHA="${1:-}"

log() { echo "[agent-deploy $(date -u +%FT%TZ)] $*"; }

[ -n "$EXPECTED_SHA" ] || { log "ERROR: expected SHA not given"; exit 1; }
[ -f "$COMPOSE_FILE" ] || { log "ERROR: $COMPOSE_FILE not found (did the workflow copy it?)"; exit 1; }
[ -f "$ENV_FILE" ] || { log "ERROR: $ENV_FILE not found (did the workflow write it from secrets?)"; exit 1; }
[ -d "$REPO_DIR/.git" ] || { log "ERROR: $REPO_DIR is not a git checkout"; exit 1; }
for cmd in docker git python3 openssl; do
  command -v "$cmd" >/dev/null || { log "ERROR: $cmd not found on the VPS"; exit 1; }
done

# --- 0. RAM gate ------------------------------------------------------------
FREE_MB="$(free -m | awk '/^Mem:/ {print $7}')"
log "free RAM: ${FREE_MB} MB (need >= 3584 MB for the app + SLATE headroom)"
if [ "$FREE_MB" -lt 3584 ]; then
  log "ERROR: not enough free RAM to start the app (free=${FREE_MB} MB, need 3584 MB)."
  log "Do not lower the limit to force it — report the numbers instead."
  docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null || true
  exit 1
fi
log "disk free on /: $(df -h / --output=avail | tail -1)"

# --- 0b. The checkout must be exactly the commit being deployed -------------
ACTUAL_SHA="$(git -C "$REPO_DIR" rev-parse HEAD)"
[ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] || {
  log "ERROR: checkout is $ACTUAL_SHA, expected $EXPECTED_SHA (did the pull race?)"
  exit 1
}
log "deploying $ACTUAL_SHA"

# --- 1. AGENT_SECRET (sessions) --------------------------------------------
if ! grep -q '^AGENT_SECRET=.\+' "$ENV_FILE"; then
  log "generating AGENT_SECRET (first install; persisted in the .env)"
  umask 077
  echo "AGENT_SECRET=$(openssl rand -hex 32)" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# --- 2. Build + start --------------------------------------------------------
log "building image on the VPS (pip install; models download at first boot, not here)"
docker compose -f "$COMPOSE_FILE" build
log "starting container"
# --- 2b. Host port must be free (SLATE's API owns 127.0.0.1:8000) -----------
if command -v ss >/dev/null; then
  if ss -ltn 2>/dev/null | grep -q '127.0.0.1:8001'; then
    log "ERROR: 127.0.0.1:8001 is already in use:"
    ss -ltnp 2>/dev/null | grep '8001' || true
    exit 1
  fi
  log "host port 127.0.0.1:8001 is free"
else
  log "WARNING: ss not found, skipping host-port check"
fi
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

# --- 3. Health poll (up to 10 min; first boot downloads the models) ---------
log "polling /healthz (up to 10 min)"
HEALTH_OK=0
for i in $(seq 1 120); do
  if curl -fsS -m 5 http://127.0.0.1:8001/healthz >/dev/null 2>&1; then
    HEALTH_OK=1
    log "healthz ok after ~$((i * 5))s"
    break
  fi
  sleep 5
done
if [ "$HEALTH_OK" -ne 1 ]; then
  log "ERROR: /healthz never came up in 10 min — container logs:"
  docker compose -f "$COMPOSE_FILE" logs --tail 50 agent-platform || true
  exit 1
fi

# --- 4. End-to-end smoke ------------------------------------------------------
log "running smoke test (login + refund-policy question)"
python3 "$REPO_DIR/deploy/smoke-test.py"

# --- 5. Nightly backup cron (idempotent) --------------------------------------
CRON_FILE="/etc/cron.d/agent-platform-backup"
CRON_LINE="30 3 * * * root /bin/bash $REPO_DIR/deploy/backup.sh >> /var/log/agent-platform-backup.log 2>&1"
if [ -f "$CRON_FILE" ] && grep -q "agent-platform-backup" "$CRON_FILE"; then
  log "backup cron already installed"
else
  log "installing nightly backup cron (03:30 UTC, offset from SLATE's 03:00)"
  printf '%s\n' "$CRON_LINE" > "$CRON_FILE"
  chmod 644 "$CRON_FILE"
fi

# --- 6. Disk guard -------------------------------------------------------------
FREE_GB="$(df -BG / --output=avail | tail -1 | tr -dc '0-9')"
if [ "$FREE_GB" -lt 5 ]; then
  log "ERROR: disk free on / is ${FREE_GB} GB (< 5 GB) after deploy — investigate before going live"
  exit 1
fi

log "deploy ok: $ACTUAL_SHA live on localhost:8001 (public URL needs the Caddy route — see deploy/CHECKLIST.md)"
