#!/usr/bin/env bash
# Agent platform — nightly off-box backup: SQLite -> R2.
#
# Runs ON THE VPS (installed as a cron job, /etc/cron.d/agent-platform-backup,
# 03:30 UTC daily — deliberately offset from SLATE's 03:00 backup so the two
# never contend). NOT run from this repo or this VM.
#
# What it backs up:
#   The SQLite DB, via Python's sqlite3 backup API run INSIDE the running
#   container (an online, consistent snapshot — the container is never
#   stopped, and a raw file copy of a live SQLite DB can be torn).
#   The DB is the WHOLE durable state: per PLAN.md the vector store is a
#   plain SQLite `chunks` table inside the same file, and /data/models
#   (fastembed cache) is re-downloadable, so there is no second artifact.
#   (An earlier draft of this script copied a /data/vector_store directory
#   that does not exist — the app has no such path. Do not re-add it.)
# It is uploaded to Cloudflare R2 (S3-compatible, awscli v2 — same tool
# SLATE's backup uses) and the upload is verified by size before the local
# staging copy is deleted. Any failure exits non-zero so the cron log shows
# it; a failed backup is never silently called a backup.
#
# Credentials: NEVER in this file or the repo. Read from the environment —
# in production, /srv/agent-platform/.env (written at deploy time from
# GitHub Actions secrets). Required:
#   BACKUP_R2_ENDPOINT   e.g. https://<account-id>.r2.cloudflarestorage.com
#   BACKUP_R2_BUCKET      e.g. agent-platform-backups
#   BACKUP_R2_ACCESS_KEY_ID
#   BACKUP_R2_SECRET_ACCESS_KEY
#
# Layout default (override via env if the app's layout differs — verified
# against the app Nov 8, 2026: Dockerfile sets AGENT_DB=/data/agent.db;
# this script must match the app, not the other way round):
#   AGENT_DB_PATH     path inside the container, default /data/agent.db
#
# MANDATORY before real users (research/05 §5 step 7): nightly backups plus
# ONE tested restore. See deploy/README.md for the restore procedure.
# SLATE's own backup cron has no verified successful run — do not assume
# this one works until its first upload is verified AND a restore is tested.

set -euo pipefail

APP_DIR="/srv/agent-platform"
COMPOSE_FILE="${COMPOSE_FILE:-$APP_DIR/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
DB_PATH="${AGENT_DB_PATH:-/data/agent.db}"

log() { echo "[agent-backup $(date -u +%FT%TZ)] $*"; }

# --- Config: R2 credentials from env (never hardcoded) ---------------------
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi
: "${BACKUP_R2_ENDPOINT:?BACKUP_R2_ENDPOINT not set}"
: "${BACKUP_R2_BUCKET:?BACKUP_R2_BUCKET not set}"
: "${BACKUP_R2_ACCESS_KEY_ID:?BACKUP_R2_ACCESS_KEY_ID not set}"
: "${BACKUP_R2_SECRET_ACCESS_KEY:?BACKUP_R2_SECRET_ACCESS_KEY not set}"
export AWS_ACCESS_KEY_ID="$BACKUP_R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$BACKUP_R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="${BACKUP_R2_REGION:-auto}"

for cmd in docker aws python3; do
  command -v "$cmd" >/dev/null || { log "ERROR: $cmd not found"; exit 1; }
done
[ -f "$COMPOSE_FILE" ] || { log "ERROR: $COMPOSE_FILE not found"; exit 1; }

CONTAINER="$(docker compose -f "$COMPOSE_FILE" ps -q agent-platform)"
[ -n "$CONTAINER" ] || { log "ERROR: agent-platform container is not running"; exit 1; }

TS="$(date -u +%Y%m%dT%H%M%SZ)"
STAGING="$(mktemp -d /tmp/agent-backup-$TS.XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT
DB_FILE="$STAGING/app-$TS.db"

# --- 1. Consistent SQLite snapshot (inside the running container) -----------
log "snapshotting SQLite $DB_PATH -> container /tmp/agent-backup-$TS.db"
docker compose -f "$COMPOSE_FILE" exec -T agent-platform python -c "
import sqlite3, sys
src = sqlite3.connect('file:$DB_PATH?mode=ro', uri=True)
dst = sqlite3.connect('/tmp/agent-backup-$TS.db')
try:
    src.backup(dst)
finally:
    dst.close(); src.close()
"
docker cp "$CONTAINER:/tmp/agent-backup-$TS.db" "$DB_FILE"
docker compose -f "$COMPOSE_FILE" exec -T agent-platform rm -f "/tmp/agent-backup-$TS.db"

# Fail closed on a corrupt or empty snapshot — never upload garbage.
[ -s "$DB_FILE" ] || { log "ERROR: DB snapshot is empty, refusing to upload"; exit 1; }
python3 -c "
import sqlite3, sys
c = sqlite3.connect('file:$DB_FILE?mode=ro', uri=True)
row = c.execute('PRAGMA integrity_check').fetchone()
sys.exit(0 if row and row[0] == 'ok' else 1)
" || { log "ERROR: DB snapshot failed integrity_check, refusing to upload"; exit 1; }
DB_SIZE="$(stat -c%s "$DB_FILE")"
log "DB snapshot ok (${DB_SIZE} bytes, integrity_check passed)"

# --- 2. Upload to R2 + verify ----------------------------------------------
DB_KEY="agent-platform/backups/$TS/app.db"

log "uploading to R2: s3://$BACKUP_R2_BUCKET/$DB_KEY"
aws --endpoint-url "$BACKUP_R2_ENDPOINT" s3 cp "$DB_FILE" "s3://$BACKUP_R2_BUCKET/$DB_KEY"

verify() {
  local key="$1" expected="$2"
  local remote
  remote="$(aws --endpoint-url "$BACKUP_R2_ENDPOINT" s3api head-object \
    --bucket "$BACKUP_R2_BUCKET" --key "$key" --query ContentLength --output text)"
  if [ "$remote" != "$expected" ]; then
    log "ERROR: upload size mismatch for $key (local=$expected remote=$remote) — backup NOT trusted"
    exit 1
  fi
}
verify "$DB_KEY" "$DB_SIZE"

log "backup ok: $TS (DB ${DB_SIZE} bytes, upload verified)"
