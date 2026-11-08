# Architecture — as built (Nov 8, 2026)

One Flask app, one SQLite database, no framework for the agent loop. The
agent is a hand-rolled turn function (`app.run_turn`); every layer below is
a plain module it calls in order.

## The turn, in order

1. **L1 input scan** (`guardrails.scan_input`) — regex/blocklist over the
   normalized message (zero-width stripped, homoglyphs folded, base64 spans
   decoded). A hit ends the turn with a fixed refusal and a logged event.
2. **Cache** (`cache.lookup`) — FAQ route only, exact SHA256 match or
   semantic match at 0.93. Never for tool, live, personal, or multi-turn
   answers. Partitioned by agent + locale + tenant + doc version + prompt
   version.
3. **Retrieval** (`rag.retrieve`) — BM25 (FTS5) + dense cosine (BGE-M3,
   1024-dim, local via fastembed), fused with RRF, reranked by
   bge-reranker-v2-m3, top 5–8. Chunks carry title + section. Order-note and
   KB text that fails the untrusted scan is withheld before it enters
   context; everything else is wrapped in `<untrusted-data>` delimiters.
4. **Router** (`router.route`) — deterministic tiers (simple / standard /
   complex) and chains (simple → Gemini Flash-Lite → DeepSeek Flash;
   standard/complex → DeepSeek Flash → Gemini Flash-Lite). Two retries with
   backoff and jitter, 60-second cooldown after two failures, never retry
   401/400, $5 budget guard routes everything to the free leg. Without keys
   the chain is `["demo"]` and the rule-based `DemoClient` answers.
5. **Tool loop** (`tools/registry.py`) — at most 6 rounds. Tools are plain
   Python functions behind MCP-shaped schemas, validated with jsonschema
   before execution. The allow-list matrix (`registry.ALLOW_LIST`) decides
   per agent per role; a tool missing from a role's row does not exist for
   that role, and the model is never shown it. Tool results are `isError`
   dicts, never tracebacks.
6. **L5 judge** (`guardrails.judge_action`) — before risky tools only
   (`issue_refund`), a second model call sees the original request, the
   proposed action, and the policy summary, and answers YES/NO. Cross-model
   when both providers are live; skipped with no live model.
7. **L6 policy engine** (`guardrails.policy_check_refund`,
   `tools/parcelco.py`) — the real wall. Refund rules in code: amount ≤
   order total, status paid/shipped/delivered, under 30 days old, one refund
   per order, at most 3 refunds per customer per day, ownership scoped to
   the session user. Above $50 the tool returns a proposal, never an
   execution.
8. **L4 output validation** (`guardrails.validate_output`) — the final
   answer is scanned for the session canary, secret patterns, and competitor
   recommendations. A hit replaces the answer with a fixed fallback.
9. **Trace** (`store.log_trace` and friends) — every turn writes its prompt,
   retrieved doc ids, tool calls, tokens, latency, cost, model, router
   decision, and validation outcome. Logging is best-effort and never breaks
   a request. Any trace replays via `tools/replay.py`.

## The eight layers, mapped to files

| Layer | File(s) |
|---|---|
| 1. System prompt | `config/prompts/parcelco.v1.txt` (versioned, never edited live) |
| 2. RAG | `rag.py`, `data/<agent>/`, `store.py` chunks + FTS5 |
| 3. Tools (MCP-shaped) | `tools/registry.py`, `tools/parcelco.py` |
| 4. Access control | `auth.py` (PBKDF2), `tools/registry.py` ALLOW_LIST, `app.py` role decorators |
| 5. Guardrails | `guardrails.py` (L1/L3/L4/L5/L6; L2 Prompt Guard 2 not installed) |
| 6. Cache | `cache.py`, `store.py` cache_entries |
| 7. Router | `router.py`, `config/routing.json`, `config/pricing.json` |
| 8. Observability | `store.py` traces/llm_calls/tool_calls/retrieval_hits, `app.py` /traces |
| 9. Evals gate | `tools/eval_run.py`, `evals/`, `tests/test_*eval*.py` |

## Approval gates

Money and commitments go through the `approvals` table (`store.py`).
A gated call creates a pending row bound to the exact arguments (SHA-256
hash), with the requester, the original request text, the policy checks,
and the judge verdict. Staff see it in `/staff/approvals`, described in
plain English written in code (`parcelco.describe_approval`), never by the
model. On approval the dispatcher re-validates every policy check at
execution time and executes once (idempotency key on `order_id`). Expiry
means NO (24 hours for refunds). Approver must differ from requester.
Staff never call `issue_refund` directly — they rule on proposals.

## Auth

PBKDF2-HMAC-SHA256, 200,000 iterations (`auth.py`, house pattern). Roles:
`customer`, `staff`, `admin`. Sessions are Flask signed cookies
(`HttpOnly`, `SameSite=Lax`, `Secure` on the live site). CSRF on every
state-changing route. Demo accounts seed from env vars at first boot.
Google OAuth (Authlib) for real Scoopy Doog customers is a later step;
until then PBKDF2 accounts are the stopgap.

## Deployment shape

One Docker container (`Dockerfile`: python:3.12-slim, gunicorn,
`/data/agent.db`, models under `/data/models`). Shares the OVH VPS with
SLATE: own Compose project under `/srv/agent-platform/`, own Caddy route,
nightly off-box SQLite backups. Measured runtime memory with BGE-M3 +
reranker loaded is ~2.5 GB — the container needs real memory limits, not
the 512 MB in the first draft plan. Deploy and CI live in `deploy/` and
`.github/` (Builder C).

## What is NOT built (deliberately)

- No LangChain / LangGraph / LlamaIndex as the agent core; no separate MCP
  server in v1 (the export adapter comes later, only if a second client
  needs the tools).
- No hosted vector database, no hosted guardrail API, no paid service.
- No Prompt Guard 2 (L2): gated download + torch weight; add only if evals
  show L1 missing paraphrased attacks.
- No rate limiting yet (see `docs/evals.md` known gaps).
- No live-model verification yet: no API keys exist in this environment, so
  the model path is exercised with fake clients in tests and the demo
  stand-in live. Live keys are a deploy-time step.
