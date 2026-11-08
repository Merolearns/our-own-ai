# Codex review — agent platform (Builder C), Nov 8, 2026

Reviewer: Builder C (Codex, ops + review). Scope: the engine + Parcelco
agent as built by Builder A (Claude) and Builder B (Kimi), plus the deploy
path. I ran the suite, the eval gate, an A11 leak probe over every table,
a backup snapshot/restore probe, and a local end-to-end smoke run myself;
I read the refund/approval/ownership code paths; I verified the Gemini
model ID from Google's own docs. Nothing below is taken on trust.

## Verdict
- Engine + Parcelco agent: **ship-ready in demo mode**, with two parent-side
  unblockers before it can go live (B1, B2 below). No code blocker found.
- Evals green 25/25, full suite 298 passed / 1 skipped.
- A11 (trace redaction) fixed and proven.
- **Live deploy NOT done**: blocked on (1) workflow files cannot be pushed
  (the PATs lack the Workflows permission), (2) repo secrets cannot be set
  from here (403). Both need the parent in the browser. Exact steps in
  `deploy/CHECKLIST.md`. The deploy itself is fully written and ready.

## What I verified firsthand
1. Full suite: **298 passed, 1 skipped** (A10 rate limiting, not built in
   v1), 12.61s. Was 297 passed + 1 xfailed; the xfail is now a pass.
2. Eval gate: **25/25, pass rate 1.0, blocked False**, demo mode.
3. A11 probe: a pasted API key (`sk-` + 32 chars) and a Luhn-valid test
   card (4111111111111111) appear **nowhere in the SQLite file** — every
   table scanned after a turn plus direct writes through every other
   store function. Stored `input_text`: "My key is [REDACTED] and my
   card is [REDACTED], can you repeat them back?"
4. Backup probe: snapshot 200,704 bytes, `integrity_check` ok, all 22
   tables' row counts match after restore to a scratch DB.
5. Remote main `ccf468be` verified by fresh clone: the redactor is in
   `store.py`, routing flags are `model_id_verified: true`, and (as
   expected) no `.github/workflows/` exists on remote yet.
6. Gemini Flash-Lite model ID `gemini-3.5-flash-lite` verified from
   Google's own docs: docs.cloud.google.com model page ("Model ID |
   `gemini-3.5-flash-lite`", GA Jul 21, 2026, retirement Jul 21, 2027 or
   later) and aistudio.google.com docs. `config/routing.json` already
   carried the correct ID with `model_id_verified: true`; no edit needed.
7. Local end-to-end smoke test (`deploy/smoke-test.py` against a local
   gunicorn server): see "Local smoke test" below.
8. Ruff: the files I changed are clean; the whole repo is not — a plain
   `ruff check .` reports **33 pre-existing style errors** in engine/test
   files (see S5). CI's lint step is therefore scoped to `--select F`
   (pyflakes), which is green; widening it before the cleanup lands
   would turn CI red on code this workflow did not touch. YAML valid on
   the compose file and all three workflows; `bash -n` clean on both
   shell scripts; `py_compile` clean.

## What I changed (my file set + the two allowed exceptions)
- `store.py`: redaction at every free-text write site (see N5); the A11
  strict-xfail decorator removed, test body unchanged (see N4).
- `.github/workflows/ci.yml`: rewritten (PRs + pushes to main, pytest +
  ruff 0.16.10). `.github/workflows/measure-vps.yml` and `deploy.yml`: new.
- `deploy/`: compose rewritten (VPS-side build, 3g / 1.0 CPU);
  `backup.sh` fixed (DB-only — the app has no `/data/vector_store`);
  `deploy-remote.sh` + `smoke-test.py` new; `README.md` rewritten;
  `CHECKLIST.md` new.
- `reviews/REVIEW.md`: this file.
- Push tooling: persistent copy at
  `~/workspace/github-portfolio/push-our-own-ai.py` (message arg + workflow
  exclusion, see B1). The old `/tmp` copy dies on VM restart.

## Findings

### Blockers (need the parent; no code fix exists)
- **B1. Workflow files cannot be pushed.** Both PATs (vault "Muse GitHub
  Access" and the gh CLI token) lack the **Workflows** permission.
  Evidence: a tree containing `.github/workflows/probe.yml` -> 403,
  the same tree with `probe.txt` -> 201; `git push` of a commit touching
  workflow files -> 403 "Permission denied". Affects `ci.yml`,
  `measure-vps.yml`, `deploy.yml`. Fix: add the three files in the repo
  web UI, or grant the Workflows permission to the PAT. Until then CI
  cannot run and no deploy can trigger.
- **B2. Repo secrets cannot be set from here.** Both tokens return 403 on
  the secrets API (read public key and set). Needed for deploy:
  `VPS_SSH_KEY`, `DEMO_CUSTOMER_PASSWORD`, `DEMO_STAFF_PASSWORD`.
  The deploy workflow refuses to start without them (by design) — it
  never deploys without logins.
- **B3. No VPS measurement yet.** Direct SSH is blocked by the egress
  proxy (tested: connection reset by 198.19.0.1:3128); the measure
  workflow cannot run until B1 is fixed. The box is 8 GB (research/05);
  the app needs ~2.5 GB RSS. The deploy enforces a RAM gate (>= 3.5 GB
  free or abort with numbers) so no guess ships — but the actual free-RAM
  number is still unknown. Do not deploy blind to the gate; the gate
  prints the numbers on the first run.

### Should-fix (not blocking a demo-mode deploy)
- **S1. L4 output validation has no card-number pattern.**
  `guardrails.SECRET_PATTERNS` covers `sk-` / `AIza` / PEM blocks but not
  Luhn-valid cards. Proven: a pasted test card (4111111111111111) was
  echoed in the demo answer as a fake order number ("Order
  4111111111111111 not found"). Storage is safe (redacted at the write
  site since this review); the answer echo is the gap. Owner:
  `guardrails.py` (Builder A). Research §9's Presidio note is the fuller
  fix for PII in output.
- **S2. Gemini client sends `temperature` to Gemini 3.5 Flash-Lite**
  (`llm.py:163`, `routing.json` temperature 0.2). Google's docs:
  temperature/top_p/top_k are deprecated for 3.5+ models — ignored
  today, an error in future generations. Stop sending sampling params
  to Gemini 3.5+ (or drop temperature from the Gemini client). Owner:
  `llm.py` (Builder A).
- **S3. No secret screening at ingest.** Research §9 requires docs to be
  screened for secret patterns at ingest (fail loudly). `rag.py` has no
  such check (grep: no secret/redact matches). Parcelco docs are authored
  so this is not a live risk today; it becomes one when ingesting
  untrusted docs (Scoopy/sales web content). Owner: `rag.py` (Builder A).
- **S4. A10 rate limiting is still unbuilt** (skipped test, by design) —
  add with the sales agent; anonymous booking requests queue for staff
  and never auto-confirm until then.
- **S5. 33 pre-existing ruff style errors in engine/test files.**
  A plain `ruff check .` (0.16.10, no config file in the repo) reports
  33 errors: 14× FURB167 (`re.I` → `re.IGNORECASE`), 9× PLE2515, 2×
  RUF059, 2× ISC004, 1× UP012, 1× SIM905, 1× RUF100, 1× PLE2502, 1×
  I001, 1× BLE001 — concentrated in `guardrails.py` (11) and
  `tests/test_guardrails.py` (8), plus `rag.py`, `llm.py`, `router.py`,
  `cache.py`, `tools/eval_run.py`, `tests/conftest.py`, `tests/test_*.py`.
  27 are auto-fixable (`ruff check --fix`). None is an F-rule (no unused
  imports, no undefined names), so the `--select F` CI gate is green.
  Owner: Builder A (cleanup + decide whether to add a repo `ruff.toml`).
  Note: `--select E,F` would add 282 E501 line-length hits — the codebase
  does not follow the 88-char limit, so do not widen the gate to E
  without a line-length decision first.

### Notes (deliberate deviations, not defects)
- **N1. Image built on the VPS, not in CI/GHCR** (the SLATE pattern).
  The Actions budget is nearly exhausted ($20.04 of $25 used Nov 8); a
  VPS build costs $0 marginal. Revisit if the budget is raised.
- **N2. Compose limits 3g / 1.0 CPU** (was 512m / 0.5 in the pre-staged
  draft, which cannot hold BGE-M3 + reranker). Enforced by the deploy RAM
  gate; see B3.
- **N3. CI pins ruff 0.16.10** (the version the code was verified clean
  against). No ruff config file exists in the repo; the binary's defaults
  are the rule set, so the version pin is what keeps CI and local
  identical.
- **N4. The A11 strict-xfail decorator was removed** (test body
  unchanged). The marker's own docstring says it flags when the fix
  lands; leaving it would keep the suite red. This is the promotion the
  test anticipated, not a weakening.
- **N5. Redaction scope: every free-text write site in `store.py`**, not
  only `log_trace` — the eval case's expectation is "nowhere in SQLite",
  and `llm_calls.request_json` (the full message list) and
  `cache_entries.query_text` held the raw key in the probe before the
  fix. `args_hash` is still computed on the ORIGINAL args (redaction
  never changes a hash), so approval binding is unaffected.
- **N6. `reviews/README.md` says one file per review
  (`codex-review-YYYYMMDD.md`) and that the reviewer never edits engine
  files; the brief (`BUILD_BRIEF_C.md`) overrides both (`REVIEW.md` +
  the A11 `store.py` exception). The brief wins.
- **N7. Demo seed passwords are required repo secrets for deploy;
  `AGENT_SECRET` is generated on first install and persisted in the VPS
  .env** (never printed, never a repo secret).

## Code read (refund / approval / ownership paths)
- `tools/parcelco.py` `issue_refund`: L6 `policy_check_refund` gates
  every refund; > $50 becomes a proposal; execution requires BOTH the
  model's `confirmed` flag AND the UI's `human_confirmed` flag; the
  write is atomic (`record_refund` returns duplicate/cap).
- `execute_approved`: expires first, staff-only, not expired, pending,
  not self-approval, hash match + recompute, atomic claim, policy
  RE-VALIDATED at execution time, then write + mark executed. This is
  the designed gate, correctly implemented.
- `check_order_status`: ownership scoping (`_load_order`: staff sees all,
  customer only their own); order notes are scanned with
  `scan_untrusted`, withheld on block, wrapped in `<untrusted-data>`
  otherwise. The #1003 injection note can never reach the model raw.
- `describe_approval`: the approval sentence is built in code, never by
  the model. Correct.

## Deploy readiness
- Code: ready (suite + evals green, A11 proven, backup restore proven).
- CI: ready, pending B1 (file must land via browser).
- Deploy workflow: ready, pending B1 + B2.
- VPS measurement: pending (measure-vps.yml, after B1).
- Caddy route: one-time manual append, after the first successful deploy.
- Live-model keys: pending parent supply (after first demo-mode deploy).
- Backup R2 upload: pending `BACKUP_R2_*` secrets (cron fails loudly
  until set).

## Local smoke test
`deploy/smoke-test.py` run against a local gunicorn server (scratch DB,
throwaway demo passwords, demo mode, real BGE-M3 + reranker): **all checks
passed, exit 0** — /healthz 200, login as demo, refund-policy answer
states $50 with sources (demo mode), demo banner present, #1003
injection note produces no ShipFast recommendation.

Two real bugs in the script itself were found and fixed by running it
(not by reading it):
1. `/api/chat` requires the `X-CSRFToken` header from the chat page's
   `<meta name="csrf-token">` (the same header `static/chat.js` sends);
   without it the API answers 400 "The CSRF token is missing." The
   script now reads the meta tag after login and sends the header.
2. The first `$500` check was too strict: the correct demo answer says
   "the $50 limit is not $500" — a negation, not the invented limit. The
   check is now negation-aware (a `$500` fails only when the 25 chars
   before it contain no not/n't/no/never).

First run also exposed an environment fact, not an app bug: on a cold
cache the server needs ~4 minutes to download the reranker
(`onnx-community/bge-reranker-v2-m3-ONNX`, 5 files) before it answers
`/healthz` — the first attempt's 5-minute poll expired mid-download
and killed the server. The deploy script's 10-minute healthz poll
covers this; the smoke script's poll window is 15 minutes.

## Open items for the next builders
- Scoopy Doog agent + sales-machine agent (same bar).
- A10 rate limiting with the sales agent.
- S1 / S2 / S3 / S5 above (Builder A).
- Gemini free-tier quotas: still unread (needs AI Studio sign-in); none
  hard-coded anywhere.
- Live LLM path: untested end to end (no keys in this environment); the
  first live-model deploy must re-run the eval gate + smoke test.
