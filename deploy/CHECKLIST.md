# deploy/CHECKLIST.md — first live deploy (parent steps + what happens when)

Status: DEPLOYED (Nov 8, 2026, workflow run 6, commit 0b435aa1) — the app
is live on the VPS at localhost:8001, smoke test green. The public URL
still needs the one-time Caddy route (§4, handed to the SLATE side chat
Nov 8 — pending their reply). Everything below is written; nothing is guessed.

Agent 2 — Scoopy Doog (Nov 8, 2026): built locally, NOT yet pushed or
deployed. The deploy is the same workflow re-run (§3) — no new secrets
are required for it — plus the second Caddy block (§4b) and the Google
OAuth parent step (§5b). See images/BUILD_REPORT_SCOOPY.md.

## 1. Parent action A — land the workflow files (browser, GitHub web UI)
DONE Nov 8, 2026 (commit 1be8003, via the repo web UI). The rest of this
section is the record of how it was done.
The vault PAT and the gh CLI PAT both lack the **Workflows** permission, so
no workflow file can be pushed by API or git (verified Nov 8, 2026: a tree
containing any `.github/workflows/` path is refused with 403, while a tree
without one succeeds with 201). Add these three files in the repo web UI
("Add file" -> "Create new file", one commit on main):
- `.github/workflows/ci.yml` — pytest + ruff 0.16.10 on PRs and pushes to main.
- `.github/workflows/measure-vps.yml` — read-only host measurement via the
  `slate-vps` runner (proves whether that runner is visible to this repo).
- `.github/workflows/deploy.yml` — the deploy (manual trigger only).

Alternative (TRIED Nov 8, 2026 — did NOT fix the API route): the
"Muse GitHub Access" PAT was opened in the UI and already showed Actions
Read and write; it was saved ("Your personal access token has been
updated") — but the contents API still returns 403
("Resource not accessible by personal access token") on all three
workflow paths. The web-UI upload above is the working path; do not
re-fight the API route. (Also note: the token edit needed a sudo-mode
email code to meroatschool@gmail.com — the protected-lookup flow.)

## 2. Parent action B — set three repo secrets (browser, repo Settings -> Secrets)
DONE Nov 8, 2026 (all three set; confirmed in the secrets table 05:02–05:03 PST).
- `VPS_SSH_KEY` — the SSH private key for root@40.160.149.104 (the SLATE
  deploy key, `~/.ssh/id_slate_deploy` on the dev VM; the value never leaves
  the browser form / GitHub secrets).
- `DEMO_CUSTOMER_PASSWORD` — password for the `demo` login (pick any
  12+ character password; this is the login the LinkedIn video will show).
- `DEMO_STAFF_PASSWORD` — password for the `staff` login (different value).

The deploy workflow fails loudly if any of these is missing — it never
deploys without logins. `AGENT_SECRET` (sessions) is generated on first
install by the deploy script and persisted in the VPS .env; it is not a
repo secret and is never printed.

## 3. Trigger the deploy (browser, Actions tab -> deploy -> Run workflow)
The workflow runs, in order: SSH setup -> repo sync at the exact SHA ->
.env written from secrets (mode 600, never printed) -> compose copied ->
`deploy-remote.sh` on the VPS: RAM gate (>= 3.5 GB free, counting the running container's RAM as reclaimable on a redeploy, or abort with
numbers), build on the VPS, up -d, /healthz poll up to 10 min (first boot
downloads ~2.3 GB of models), end-to-end smoke (login + refund-policy
question: $50 with sources, demo banner, no ShipFast), backup cron
installed, disk guard (>= 5 GB free or abort). Any failure aborts the run
with the numbers in the log.

Deploy runs (Nov 8, 2026, all by API dispatch):
- Run 1 (42483353062): FAILED — bind for 127.0.0.1:8000 failed, port already
  allocated (SLATE's API). Fix: host port 8000 -> 8001 (commit 17abd147).
- Run 2 (42483897087): FAILED — smoke test login as demo: HTTP 400. Cause:
  SESSION_COOKIE_SECURE=1 + cookiejar dropping the Secure cookie over plain
  HTTP. Fix: smoke test carries cookies manually + captures the 302's
  Set-Cookie (commit ab299226; verified locally first).
- Run 3 (42484637062): FAILED — RAM gate: 3340 MB free vs 3584 MB needed,
  while the run-2 container held 1.664 GiB. Fix: gate counts the running
  container's RAM as reclaimable (commit 01aff05c).
- Run 4 (42485001507): FAILED — port check: 127.0.0.1:8001 "already in use"
  by docker-proxy (the run-2 container legitimately holds it). Fix: port
  check skips when the agent-platform container itself is up (commit 0b435aa1).
- Run 5 (42485296313): FAILED — SSH step hung 120s (LoginGraceTime) then
  "Connection closed by 40.160.149.104 port 22". Transient: same key/config
  worked in runs 1–4 and 6. No code change; retried.
- Run 6 (42485635901): SUCCESS — RAM gate 3464 + 1655 reclaimable = 5119 MB
  effective, healthz ok after ~10s, smoke test all checks passed, backup
  cron installed, disk free 36G. AGENT_SECRET generated on first install.
  Deploy ok: 0b435aa1 live on localhost:8001.

## 4. One-time Caddy route (parent / SLATE side chat, vps-ops)
Append `deploy/caddy-snippet` (host `agents.40.160.149.104.nip.io`,
reverse-proxy to localhost:8001) to the VPS Caddyfile BELOW SLATE's blocks,
then `caddy validate` and only reload if validation exits 0. A bad Caddyfile
takes SLATE down too — never reload a file that failed validation. Until
the route exists the app is verified over SSH only, not via a public URL.
After the route is live: screenshot the chat page as `images/13-live-deploy.png`
(headless Chromium, same as Builder A's 01-12).

Handed to the SLATE side chat Nov 8 (after run 6) — LIVE the same day:
route appended, validated, reloaded; healthz 200 at
https://agents.40.160.149.104.nip.io/healthz. Adaptations (Caddy is a
container, not a host service): Caddyfile at /srv/slate/infra/caddy/Caddyfile,
validate/reload via `docker exec slate-caddy caddy ...`; reverse_proxy
agent-platform:8000 (container name + internal port) after connecting
slate-caddy to the agent-platform_default network — localhost:8001 does not
work from inside the Caddy container. See deploy/caddy-snippet for the
applied block. SLATE healthy after the reload.

## 4b. Agent 2 — Scoopy Caddy route (parent / SLATE side chat, vps-ops)
Same pattern as §4, second block in `deploy/caddy-snippet`:
`scoopy.40.160.149.104.nip.io` -> `reverse_proxy agent-platform:8000`
(the SAME container, SAME port — one process serves both agents, picked
by the Host header). The Caddy container is already on the
agent-platform_default network from the Parcelco route, so no new
`docker network connect` is needed. Append below the Parcelco block,
`caddy validate`, reload only on exit 0. After the route is live, verify
firsthand: healthz + login + a pricing question through
https://scoopy.40.160.149.104.nip.io/ and screenshot the chat page as
`images/14-scoopy-live.png`.

## 5. After the first deploy — live-model keys (parent, when ready)
The first deploy runs in DEMO MODE (no LLM keys): the app answers from its
rule-based demo client and shows the "demo mode — no live model" banner.
To go live-model, set the repo secrets `DEEPSEEK_API_KEY` and
`GEMINI_API_KEY` (both already in the vault; the values never enter chat or
the repo), then re-run the deploy workflow — it rewrites the .env and
restarts the container. Verify afterwards: the banner disappears and the
eval gate still passes (`python -m tools.eval_run --agent parcelco`).

## 5b. Agent 2 — Google OAuth client (parent, after the Scoopy deploy)
Real Scoopy customers sign in with Google. The code path is built
(`oauth.py`, Authlib) but the Google OAuth client does not exist yet —
creating it is a ~10-minute parent step in Google Cloud Console (his
Google login), then the two values go in the VPS `.env` (never the repo):
1. Google Cloud Console -> APIs & Services -> Credentials -> Create
   Credentials -> OAuth client ID -> Web application. Name it
   "Scoopy Doog support".
2. Authorized redirect URI (exact):
   `https://scoopy.40.160.149.104.nip.io/auth/google/callback`
   (Authorized JavaScript origins: `https://scoopy.40.160.149.104.nip.io`).
   No localhost redirect is needed: sign-in happens on the live host.
3. Copy the client ID and client secret into the VPS `.env` as
   `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`, then restart
   the container (`docker compose restart` in /srv/agent-platform, or
   re-run the deploy workflow).
4. NOTE: the deploy workflow writes the VPS .env from a FIXED list of
   repo secrets, and the Google pair is NOT in that list (workflow files
   can only be edited in the repo web UI). Until the workflow gains the
   two lines, set the pair directly in the VPS .env — a deploy re-run
   overwrites the .env, so re-add them after any re-deploy, or add the
   two secrets + two lines to deploy.yml in the web UI once and be done.
Until the client exists, logins fall back to PBKDF2 password accounts
(the same pattern as the Parcelco demo accounts) and the "Continue with
Google" link stays hidden. Nothing blocks on this: the Scoopy deploy
ships first, OAuth lands after.

## 6. Backups — status
`deploy/backup.sh` (nightly 03:30 UTC cron, installed by the deploy) backs
up the SQLite DB only — the chunks table lives in the same DB file, and the
model cache under /data/models is re-downloadable, so there is no separate
vector-store step. The snapshot + integrity-check + restore procedure was
verified locally (see reviews/REVIEW.md). The R2 upload leg needs the repo
secrets `BACKUP_R2_ENDPOINT`, `BACKUP_R2_BUCKET`, `BACKUP_R2_ACCESS_KEY_ID`,
`BACKUP_R2_SECRET_ACCESS_KEY` (SLATE already uses R2; a second bucket on the
same account works). Until those secrets exist, the cron fails loudly in
its log (exit non-zero, never a silent fake backup) — set them before real
users touch the app.

## 7. What is already verified (firsthand, Nov 8, 2026)
- Full suite: 298 passed, 1 skipped (A10 rate limiting, not built in v1).
- Eval gate: 25/25, pass rate 1.0, demo mode.
- A11: a pasted API key and a Luhn-valid test card appear nowhere in the
  SQLite file (probe over every table); stored trace shows `[REDACTED]`.
- Gemini Flash-Lite model ID `gemini-3.5-flash-lite` verified from Google's
  own docs (GA Jul 21, 2026; retirement Jul 21, 2027 or later).
- Remote main: `0b435aa1` (deploy fixes: port 8001, smoke-test Secure
  cookies, redeploy-aware RAM gate + port check).
