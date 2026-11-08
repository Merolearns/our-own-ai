# BUILD REPORT — Scoopy Doog agent (agent 2), Nov 8, 2026

Builder: subagent (Claude Code), per `BUILD_BRIEF_SCOOPY.md`. Parent verifies,
pushes, and deploys — nothing here was pushed and the deploy workflow was
not run. Parcelco is live in production (deploy run 6); its suite, eval
gate, and live smoke are unchanged by this build (verified below).

## What was built

One process serves both agents; the Host header picks the agent
(`app.resolve_agent`): `agents.40.160.149.104.nip.io` -> parcelco,
`scoopy.40.160.149.104.nip.io` -> scoopy, anything else -> parcelco.
No second container — the 8 GB box cannot hold two app processes at peak
(one peaks at ~2.5 GB with BGE-M3), and the RAM gate was not lowered.

| File | What it does |
|---|---|
| `config/prompts/scoopy.v1.txt` | System prompt, grounded in `data/scoopy/` only. States the [VERIFY] deflection rule, the per-frequency-price ban, the no-guarantee / no-promotion bans, the "residential sanitation service" ban, scoopydoog.com-only links, and the exact-city deflection. |
| `config/scoopy.json` | Agent config for eval runs (`--config config/scoopy.json`). |
| `tools/scoopy.py` | The two tools (see justifications below). |
| `tools/registry.py` | `REGISTRY["scoopy"]` + allow-list (both tools for customer/staff/admin). |
| `data/scoopy/*.md` | Re-read, re-wrapped (see § Data), plus a "What is Scoopy Doog?" FAQ sentence. README excluded from ingest. |
| `app.py` | Host routing per request, dual-corpus ingest at startup, healthz requires chunks for every agent, per-agent page branding, Google OAuth routes, ProxyFix for the OAuth redirect behind Caddy. |
| `oauth.py` | Google OAuth (Authlib). Guarded by env vars; PBKDF2 fallback when unconfigured. |
| `store.py` | `users.google_sub` column (migration in `init_db`, existing DBs upgrade in place), `get_user_by_google_sub`, `set_google_sub`, `create_user(..., google_sub=)`. |
| `templates/*`, `static/chat.js` | Branding follows the host (Scoopy pages never say Nova/Parcelco; Parcelco pages byte-identical). |
| `evals/scoopy/cases.jsonl` | 25 cases: 10 FAQ, 6 tool, 4 refusal, 3 edge, 2 tone. |
| `evals/baselines/scoopy.json` | First green baseline (25/25, demo mode, Nov 8, 2026). |
| `tests/test_scoopy_tools.py`, `tests/test_oauth.py` | Tools, allow-list, host routing, demo turns, HTTP branding, OAuth paths, eval subset. Plus S1/S2/S3 tests in the existing files. |
| `deploy/caddy-snippet`, `deploy/CHECKLIST.md`, `deploy/README.md` | Second Caddy block (same container, same port), §4b + §5b parent steps. |
| `deploy/smoke-test.py` | Scoopy section (Host header via http.client): branding, $60 pricing, per-frequency refusal, callback ticket. |
| `requirements.txt`, `.env.example` | `Authlib==1.6.2`, `GOOGLE_OAUTH_*` names. |
| `docs/architecture.md`, `docs/evals.md`, `README.md` | Agent 2, host routing, OAuth, Scoopy eval set. |

## Tool justifications

Scoopy Doog has no customer API and no order system, so the tool set is
deliberately two tools — every other candidate was rejected for lack of a
backend:

- **`open_ticket` (main tool, side effect, not risky).** A callback /
  quote-follow-up request: name, phone, zip (optional), subject,
  description. Staff follow up by phone. This is how a chat conversation
  turns into a real-world action. A ticket with only subject + description
  is still filed — a partial request beats a dropped one. No order_id
  exists for Scoopy; the field is absent from the schema, not nullable.
- **`check_service_area` (read-only).** Returns the general service area
  (Orange County, California) and deflects to the office for any specific
  city or zip — the exact city list is not published, so the tool never
  confirms a city (`city_confirmed: false` always). Guessing coverage
  for a real address would send a technician to the wrong place or turn
  a real customer away.
- **NOT built:** order lookup, refund, scheduling, payment. No backend
  exists for any of them. Booking stays on the website's Free Quote flow
  and the customer portal; the agent links and explains, never books.

## Engine fixes (from reviews/REVIEW.md)

- **S1 — L4 card pattern (`guardrails.py`).** `find_card_number` (same
  CARD_RUN + Luhn shape as `store.py`'s redactor) and `find_secret`
  (secret patterns + card) now gate `validate_output` with trigger
  `card-number`. A pasted test card (4111111111111111) is blocked at L4
  instead of being echoed as a fake order number. Phone numbers (10
  digits), order ids (4 digits), and money amounts never match the run.
- **S2 — Gemini temperature (`llm.py`).** `GeminiClient` drops
  `generationConfig.temperature` for Gemini 3.5+ models (deprecated,
  ignored today, an error later) and keeps it for older models.
- **S3 — ingest screening (`rag.py`).** `_store_chunks` screens every
  chunk with `guardrails.find_secret` and raises `ValueError` (fail
  loudly) instead of storing a secret-bearing document. Lands before any
  Scoopy doc goes through ingest in production. The Parcelco corpus
  passes the screen unchanged.
- **S5 — ruff.** Touched files are clean under the CI gate (`--select F`).
  The gate was not widened.

## Verification (all firsthand, Nov 8, 2026)

- **Full suite: 344 passed, 1 skipped** (was 298 passed + 1 skipped;
  +46 new Scoopy/OAuth/S1–S3 tests). The 1 skip is A10 rate limiting,
  still unbuilt by design (sales agent).
- **Scoopy eval gate: 25/25, pass rate 1.0, zero hard blockers, 3 runs at
  temp 0** (demo mode, BM25-only retrieval). First run was 17/25; the
  8 fixes were all quoter-ranking or demo-client bugs, fixed in code and
  data, never by weakening an expectation (see `docs/evals.md`).
- **Parcelco eval gate re-run: 25/25, pass rate 1.0, delta 0.0** — no
  regression from host routing, the DemoClient changes, or the S1–S3
  fixes.
- **Ruff `--select F` on all touched files: clean.**
- **Local smoke (real gunicorn + real BGE-M3/reranker, scratch DB,
  throwaway passwords): all checks passed, exit 0** — /healthz 200,
  login as demo, $50 refund answer with sources, demo banner, no ShipFast
  on #1003, plus the new Scoopy section: Scoopy branding on the scoopy
  host (Parcelco host unchanged), pricing answer states $60, per-frequency
  price deflects to Free Quote with no quoted price, callback opens a
  ticket. Server stopped afterwards; scratch files deleted.
- **OAuth paths tested with a mocked Authlib client** (callback signs in,
  failure lands on login, unconfigured redirects to password login).
  The live Google flow cannot be tested here — no OAuth client exists
  yet (parent step below).

## Data — what changed on re-read

Re-read scoopydoog.com during the build (homepage crawled Nov 8, 2026 +
service-page search record). Confirmed: frequencies, 2x/week
recommendation, Orange County only, hours Mon–Sat 7am–6pm, Free Quote
flow (zip + signup), billing on the 1st before services, auto-pay
(bank/card), invoice + email receipt, removal from schedule when unpaid,
contact details. The detailed process/FAQ text is carried over from the
Nov 8 morning read — the crawler returns homepage content for sub-pages,
so it could not be re-confirmed line by line. **No fact changed.**

Two flags for the next browser pass (do not change the figures on a
hunch — recorded in `data/scoopy/README.md`):
- **"$100–$135 typical"**: sibling sites on the same template (Swoop
  Scoop, Poopy Pup LA) publish "$95–$135". Scoopy's own figure needs one
  direct look at its FAQ.
- **One-time visits** are in the FAQ/service data but not on the
  homepage's frequency line — confirm they are still offered.
- **dooscooping.com still does not resolve** (fetch failed again Nov 8)
  — the redirect stays [VERIFY]; scoopydoog.com links only.

Mechanical changes to the KB files (no fact changed): the "Source:"
provenance lines moved to the README, and every sentence was put on a
single line — the demo stand-in quotes sentences and splits on line
breaks, so a wrapped sentence quotes broken. `data/scoopy/README.md`
itself is now excluded from ingest (`rag.SKIP_DOCS`): builder notes, not
customer knowledge. The rejected-label ban is carried by the prompt and
by a new customer-facing FAQ sentence ("Scoopy Doog is a professional
dog waste removal service...").

## Google OAuth — parent step (after the deploy)

The code path is built (`oauth.py`, Authlib 1.6.2, routes
`/login/google` and `/auth/google/callback`); the Google OAuth client
does not exist yet. Exact steps in `deploy/CHECKLIST.md` §5b:
1. Google Cloud Console -> Credentials -> OAuth client ID (Web
   application), name "Scoopy Doog support".
2. Authorized redirect URI (exact):
   `https://scoopy.40.160.149.104.nip.io/auth/google/callback`
3. Put the client ID + secret in the VPS `.env` as
   `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`, restart the
   container. **Caveat:** the deploy workflow writes the VPS .env from a
   fixed secret list that does NOT include the Google pair — until the
   workflow gains the two lines (web UI edit), re-add the pair to the
   VPS .env after any deploy re-run. Until the client exists, password
   logins work and the "Continue with Google" link stays hidden; nothing
   blocks on this.

## What the parent does next (in order)

1. Review + push this commit (local only, author Merolearns).
2. Re-run the deploy workflow (same as Parcelco — no new repo secrets
   needed for the Scoopy deploy itself).
3. Hand the Scoopy Caddy block to the SLATE side chat (`deploy/caddy-snippet`
   §4b: append, validate, reload only on exit 0).
4. Live-verify through https://scoopy.40.160.149.104.nip.io/ (healthz +
   login + pricing question) and screenshot `images/14-scoopy-live.png`.
5. Google OAuth client (§5b above) + LLM keys into repo secrets, then
   re-run the deploy and re-run both eval gates live.

## Assumptions (stated up front)

- Host routing is exact-match on `scoopy.40.160.149.104.nip.io`
  (port stripped, case-insensitive); everything else is Parcelco.
- The DemoClient (no-live-model stand-in) detects the agent from the
  system prompt text ("Scoopy Doog" present) and answers with that
  agent's tools. Parcelco demo behaviour is unchanged (its gate re-run
  proves it); the live-model path needs no such detection.
- Eval questions share vocabulary with the source sentences they need —
  the demo quoter ranks by 5-char-prefix overlap. Wording choices are
  documented per case in `evals/scoopy/cases.jsonl`.
- OAuth users are keyed by Google subject id; a verified email links to
  an existing password account. OAuth-only accounts have an empty
  password hash and can never password-login.
