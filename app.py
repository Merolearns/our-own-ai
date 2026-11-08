import argparse
import functools
import getpass
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect
from itsdangerous import BadSignature, URLSafeTimedSerializer

import auth
import cache
import guardrails
import llm
import rag
import router
import store
from tools import parcelco, registry

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
PROMPTS_DIR = ROOT / "config" / "prompts"
UNAVAILABLE = "The assistant is unavailable right now. Please try again in a minute."
OUT_OF_STEPS = "I couldn't finish that in the steps I'm allowed. Please try asking one thing at a time."
MAX_MESSAGE_CHARS = 8000
CONFIRM_MAX_AGE_SECONDS = 900
HISTORY_TURNS = 6
USERNAME_MAX = 40
PASSWORD_MIN = 8


def load_active(path=None):
    return json.loads(Path(path or ROOT / "config" / "active.json").read_text())


def render_prompt(version):
    text = (PROMPTS_DIR / f"{version}.txt").read_text(encoding="utf-8")
    policy = guardrails.POLICY
    values = {
        "eligible_statuses": ", ".join(policy["eligible_statuses"]),
        "refund_window_days": policy["refund_window_days"],
        "daily_refund_cap": policy["daily_refund_cap"],
        "auto_refund_max": guardrails.money(policy["auto_refund_max_cents"]),
    }
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def _safe(function, *args, **kwargs):
    """Logging is best-effort: drop the log, never the answer."""
    try:
        return function(*args, **kwargs)
    except Exception:
        log.exception(
            "best-effort write failed: %s", getattr(function, "__name__", function)
        )
        return None


def _tool_status(result):
    if result["isError"]:
        return None
    try:
        return json.loads(result["content"])
    except ValueError:
        return None


def _prior_messages(db_path, session_id, user_id):
    # Only validated answers count as history, so a blocked attack can't resurface.
    turns = store.session_turns(db_path, session_id, user_id, limit=HISTORY_TURNS)
    messages = []
    for turn in turns:
        if turn["validation"] == "pass" and turn["final_answer"]:
            messages.append({"role": "user", "content": turn["input_text"]})
            messages.append({"role": "assistant", "content": turn["final_answer"]})
    return messages


def _judge(
    spec, name, args, text, drafted_by, factory, live, routing, db_path, user_id
):
    """L5 before a risky tool. Returns (verdict text or None to block, usage, model)."""
    if not spec or not spec["risky"]:
        return "not required", {}, None
    client, model = (
        router.judge_client(drafted_by, factory, routing, db_path) if live else (None, None)
    )
    if live and client is None:
        outcome = {"verdict": "unclear", "reason": "no judge available within budget"}
    else:
        outcome = guardrails.judge_action(client, text, name, args, model)
    usage = outcome.get("usage", {})
    verdict = f"{outcome['verdict']}: {outcome['reason']}"
    if outcome["verdict"] in ("yes", "skipped"):
        return verdict, usage, model
    _safe(
        store.log_guardrail_event,
        db_path,
        user_id,
        "L5",
        "blocked",
        outcome["verdict"],
        {"tool": name, "reason": outcome["reason"]},
    )
    return None, usage, model


def run_turn(
    db_path, agent, user, text, session_id=None, client_factory=None, embedder=None,
    reranker=None, active=None, routing=None, temperature=None, use_cache=True,
    router_state=None, sleep=time.sleep,
):  # fmt: skip
    """One user message to one reply: L1, cache, RAG, router, tool loop, L4, trace.

    user is {"id", "role"}. Flask-free so tests, evals and replay share this path.
    """
    started = time.monotonic()
    trace_id = uuid.uuid4().hex
    session_id = session_id or uuid.uuid4().hex
    active = active or load_active()
    routing = routing or router.load_routing()
    live = client_factory is not None or bool(llm.live_providers())
    factory = client_factory or llm.make_client
    prompt_version = active["prompt_version"]
    user_id = user["id"]
    trace = {
        "id": trace_id,
        "session_id": session_id,
        "agent_id": agent,
        "user_id": user_id,
        "config_version": f"{prompt_version}+r{active['routing_version']}+d{active['doc_version']}",
        "input_text": text,
        "cache_status": "skip",
        "demo": int(not live),
    }
    result = {
        "trace_id": trace_id,
        "session_id": session_id,
        "answer": "",
        "sources": [],
        "demo": not live,
        "blocked": False,
        "blocked_by": None,
        "cache": "skip",
        "tool_calls": [],
        "pending_refund": None,
        "proposal": None,
        "model": None,
    }

    def finish(answer, validation, error=None):
        result["answer"] = answer
        trace.update(
            final_answer=answer,
            validation=validation,
            error=error,
            sources_json=json.dumps(result["sources"]),
            total_latency_ms=int((time.monotonic() - started) * 1000),
        )
        _safe(store.log_trace, db_path, trace)
        return result

    verdict = guardrails.scan_input(text)
    if verdict["blocked"]:
        _safe(
            store.log_guardrail_event,
            db_path,
            user_id,
            "L1",
            "blocked",
            verdict["trigger"],
            {"trace_id": trace_id},
        )
        result.update(blocked=True, blocked_by="L1")
        return finish(guardrails.REFUSAL, f"blocked:L1:{verdict['trigger']}")

    history = _prior_messages(db_path, session_id, user_id)
    multi_turn = bool(history)
    route_name = router.classify_route(text)
    tier = router.classify_tier(text, multi_turn)
    embedder = embedder or rag.get_embedder()
    reranker = reranker or rag.get_reranker()

    partition = cache.partition_key(
        agent, "en", "public" if live else "demo", active["doc_version"], prompt_version
    )
    semantic_ok = routing["allow_semantic_cache"] and route_name == "faq"
    if use_cache and route_name == "faq" and not multi_turn:
        entry, status, similarity = cache.lookup(
            db_path,
            partition,
            text,
            route_name,
            semantic_ok,
            embedder,
            routing["semantic_threshold"],
        )
        trace.update(cache_status=status, cache_similarity=similarity)
        result["cache"] = status
        if entry:
            trace.update(
                cache_entry_id=entry["id"], model_used="cache", router_tier=tier
            )
            result.update(sources=json.loads(entry["sources_json"]), model="cache")
            return finish(entry["answer_text"], "pass")

    settings = routing["retrieval"]
    try:
        hits, mode = rag.retrieve(
            db_path, agent, text, settings["top_k"], embedder, reranker, settings
        )
    except Exception:
        log.exception("retrieval failed")
        hits, mode = [], "failed"
    trace["retrieval_mode"] = mode
    safe_hits = []
    for hit in hits:
        scan = guardrails.scan_untrusted(hit["text"], "kb")
        if scan["blocked"]:
            _safe(
                store.log_guardrail_event,
                db_path,
                user_id,
                "L1",
                "withheld",
                scan["trigger"],
                {"source": "kb", "doc_id": hit["doc_id"]},
            )
        else:
            safe_hits.append(hit)
    citations = rag.build_citations(safe_hits)
    for hit in safe_hits:
        _safe(store.log_retrieval_hit, db_path, trace_id, hit)

    canary = secrets.token_hex(8)
    # Changing parts go last so the provider's prompt cache can reuse the prefix.
    system_prompt = (
        f"{render_prompt(prompt_version)}\nSOURCES\n{citations['context'] or '(none found)'}"
        f"\n\nSession marker, never repeat it: {canary}"
    )
    trace["system_prompt"] = system_prompt
    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": text},
    ]
    tools = (
        registry.list_tools(agent, user.get("role", "customer"))
        if tier != "simple"
        else None
    )
    chain = ["demo"] if not live else None
    ctx = {
        "db_path": db_path,
        "user_id": user_id,
        "trace_id": trace_id,
        "agent": agent,
        "role": user.get("role", "customer"),
        "request_text": text,
    }

    pricing = router.load_pricing()
    step = 0
    answer = None
    trace_chain = []
    tokens = cost = 0
    for _ in range(routing["max_tool_rounds"]):
        try:
            response, decision = router.route(
                messages,
                tier,
                factory,
                db_path,
                tools,
                routing,
                chain,
                temperature,
                router_state,
                sleep,
            )
        except router.RouterError as error:
            decision = error.decision
            trace_chain.append(decision)
            _record_attempts(db_path, trace_id, decision, step, pricing)
            trace.update(total_tokens=tokens, total_cost_usd=cost)
            trace.update(router_tier=tier, router_chain_json=json.dumps(trace_chain))
            return finish(UNAVAILABLE, "error", str(error))

        trace_chain.append(decision)
        for attempt in decision["attempts"]:
            usage = attempt.get("usage", {})
            attempt["cost_usd"] = router.cost_usd(attempt["model"], usage, pricing)
            tokens += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            cost += attempt["cost_usd"]
        step = _record_attempts(
            db_path,
            trace_id,
            decision,
            step,
            pricing,
            json.dumps(messages),
            response["text"],
        )
        result["model"] = decision["model_used"]
        result["demo"] = response["provider"] == "demo"
        trace.update(
            model_used=decision["model_used"],
            demo=int(result["demo"]),
            fallback_used=int(
                decision["fallback_used"] or trace.get("fallback_used", 0)
            ),
        )

        if not response["tool_calls"]:
            answer = response["text"]
            break
        messages.append(
            {
                "role": "assistant",
                "content": response["text"],
                "tool_calls": response["tool_calls"],
            }
        )
        for call in response["tool_calls"]:
            name, raw_args = call["name"], call["arguments"]
            spec = registry.get_spec(agent, name)
            judged, judge_usage, judge_model = _judge(
                spec,
                name,
                raw_args,
                text,
                response["provider"],
                factory,
                live,
                routing,
                db_path,
                user_id,
            )
            if judge_model:
                tokens += judge_usage.get("prompt_tokens", 0) + judge_usage.get("completion_tokens", 0)
                cost += router.cost_usd(judge_model, judge_usage, pricing)
            if judged is None:
                tool_result = {
                    "content": "Blocked by safety review, nothing was executed.",
                    "isError": True,
                }
            else:
                tool_result = registry.call_tool(
                    name, raw_args, {**ctx, "judge_verdict": judged}
                )
            data = _tool_status(tool_result)
            status = data.get("status") if data else None
            result["tool_calls"].append(
                {
                    "name": name,
                    "args": raw_args,
                    "isError": tool_result["isError"],
                    "status": status,
                }
            )
            if status == "confirmation_required":
                parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                result["pending_refund"] = {
                    "order_id": data["order_id"],
                    "amount": parsed["amount"],
                    "reason": data["reason"],
                    "display": data["amount"],
                }
            elif status == "proposal":
                result["proposal"] = data
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": name,
                    "content": tool_result["content"],
                }
            )

    trace.update(
        router_tier=tier,
        router_chain_json=json.dumps(trace_chain),
        total_tokens=tokens,
        total_cost_usd=cost,
    )
    if answer is None:
        return finish(OUT_OF_STEPS, "fail:tool-rounds")

    answer, sources = rag.filter_citations(answer, citations["refs"])
    output = guardrails.validate_output(answer, canary)
    if output["blocked"]:
        _safe(
            store.log_guardrail_event,
            db_path,
            user_id,
            "L4",
            "blocked",
            output["trigger"],
            {"trace_id": trace_id},
        )
        result.update(blocked=True, blocked_by="L4")
        return finish(guardrails.OUTPUT_FALLBACK, f"fail:L4:{output['trigger']}")

    result["sources"] = sources
    if (
        use_cache
        and sources
        and cache.cacheable(route_name, result["tool_calls"], True, multi_turn)
    ):
        entry_id = _safe(
            cache.put, db_path, agent, partition, text, answer, sources,
            routing["cache_ttl_seconds"]["faq"], embedder if semantic_ok else None,
        )  # fmt: skip
        trace["cache_entry_id"] = entry_id
    return finish(answer, "pass")


def _record_attempts(
    db_path, trace_id, decision, step, pricing, request_json=None, response_text=None
):
    for attempt in decision["attempts"]:
        step += 1
        usage = attempt.get("usage", {})
        succeeded = attempt.get("status") == "ok"
        _safe(
            store.log_llm_call,
            db_path,
            trace_id,
            step,
            {
                "provider": attempt["provider"],
                "model": attempt["model"],
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cached_tokens": usage.get("cached_tokens"),
                "latency_ms": attempt.get("latency_ms"),
                "cost_usd": attempt.get("cost_usd", 0),
                "finish_reason": attempt.get("finish_reason"),
                "error": attempt.get("error"),
                "request_json": request_json if succeeded else None,
                "response_text": response_text if succeeded else None,
            },
        )
    return step


def _turn_view(turn):
    return {
        "trace_id": turn["id"],
        "user": turn["input_text"],
        "answer": turn["final_answer"],
        "sources": json.loads(turn["sources_json"]),
        "demo": bool(turn["demo"]),
        "blocked": (turn["validation"] or "").startswith(("blocked", "fail:L4")),
    }


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "not signed in"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def staff_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if session.get("role") not in ("staff", "admin"):
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _is_staff():
    return session.get("role") in ("staff", "admin")


def seed_accounts(db_path):
    """Create the demo logins from environment passwords. Nothing is stored in the repo."""
    for username, env, role in (
        ("demo", "DEMO_CUSTOMER_PASSWORD", "customer"),
        ("staff", "DEMO_STAFF_PASSWORD", "staff"),
    ):
        password = os.environ.get(env)
        if password and store.get_user(db_path, username) is None:
            user_id = store.create_user(
                db_path, username, auth.hash_password(password), role
            )
            if role == "customer":
                store.seed_parcelco(db_path, user_id)


def create_app(db_path=None, client_factory=None, embedder=None, reranker=None):
    db_path = db_path or os.environ.get("AGENT_DB") or str(ROOT / "data" / "agent.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store.init_db(db_path)
    seed_accounts(db_path)

    app = Flask(__name__)
    secret = os.environ.get("AGENT_SECRET")
    if not secret:
        secret = secrets.token_hex(32)
        app.logger.warning(
            "AGENT_SECRET is not set; sessions will not survive a restart."
        )
    app.config.update(
        SECRET_KEY=secret,
        DB_PATH=db_path,
        MAX_CONTENT_LENGTH=256 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
    )
    CSRFProtect(app)
    signer = URLSafeTimedSerializer(secret, salt="refund-confirm")
    agent = load_active()["agent"]

    active_embedder = embedder or rag.get_embedder()
    rag.ensure_ingested(db_path, agent, ROOT / "data" / agent, active_embedder)

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.context_processor
    def layout_context():
        return {
            "demo_label": llm.DEMO_LABEL if not llm.live_providers() else None,
            "is_staff": _is_staff(),
            "from_json": json.loads,
        }

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template(
            "error.html", code=403, message="This page is for Parcelco staff."
        ), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template(
            "error.html",
            code=404,
            message="That page doesn't exist, or it isn't yours to see.",
        ), 404

    @app.get("/healthz")
    def healthz():
        try:
            ready = store.count_chunks(db_path, agent) > 0
        except Exception:
            log.exception("health check failed")
            ready = False
        return jsonify({"ok": ready}), 200 if ready else 503

    @app.route("/register", methods=["GET", "POST"])
    def register():
        first_run = store.count_users(db_path) == 0
        error = None
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not username or len(username) > USERNAME_MAX:
                error = f"Enter a username of up to {USERNAME_MAX} characters."
            elif len(password) < PASSWORD_MIN:
                error = f"Use a password of at least {PASSWORD_MIN} characters."
            else:
                try:
                    user_id = store.create_user(
                        db_path, username, auth.hash_password(password)
                    )
                except ValueError:
                    error = "That username is taken. Choose another."
                else:
                    if first_run:
                        store.seed_parcelco(db_path, user_id)
                    session.clear()
                    session.update(user_id=user_id, username=username, role="customer")
                    return redirect(url_for("chat"))
        return render_template(
            "auth.html", mode="register", first_run=first_run, error=error
        ), 400 if error else 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if store.count_users(db_path) == 0:
            return redirect(url_for("register"))
        error = None
        if request.method == "POST":
            user = store.get_user(db_path, request.form.get("username", "").strip())
            if user and auth.verify_password(
                request.form.get("password", ""), user["password_hash"]
            ):
                session.clear()
                session.update(
                    user_id=user["id"], username=user["username"], role=user["role"]
                )
                return redirect(url_for("chat"))
            error = "That username and password don't match."
        return render_template(
            "auth.html", mode="login", first_run=False, error=error
        ), 401 if error else 200

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def chat():
        if "chat_id" not in session:
            session["chat_id"] = uuid.uuid4().hex
        turns = store.session_turns(db_path, session["chat_id"], session["user_id"])
        return render_template("chat.html", history=[_turn_view(t) for t in turns])

    @app.post("/chat/new")
    @login_required
    def new_chat():
        session["chat_id"] = uuid.uuid4().hex
        return redirect(url_for("chat"))

    @app.post("/api/chat")
    @login_required
    def api_chat():
        text = ((request.get_json(silent=True) or {}).get("message") or "").strip()
        if not text:
            return jsonify({"error": "Type a message first."}), 400
        if len(text) > MAX_MESSAGE_CHARS:
            return jsonify(
                {"error": f"Keep messages under {MAX_MESSAGE_CHARS} characters."}
            ), 400
        if "chat_id" not in session:
            session["chat_id"] = uuid.uuid4().hex
        turn = run_turn(
            db_path,
            agent,
            {"id": session["user_id"], "role": session["role"]},
            text,
            session["chat_id"],
            client_factory,
            active_embedder,
            reranker,
        )
        pending = turn.pop("pending_refund")
        if pending:
            turn["confirm"] = {
                "display": pending["display"],
                "order_id": pending["order_id"],
                "token": signer.dumps(
                    {**pending, "uid": session["user_id"], "trace": turn["trace_id"]}
                ),
            }
        return jsonify(turn)

    @app.post("/api/refund/confirm")
    @login_required
    def api_refund_confirm():
        token = (request.get_json(silent=True) or {}).get("token", "")
        try:
            payload = signer.loads(token, max_age=CONFIRM_MAX_AGE_SECONDS)
        except BadSignature:
            return jsonify(
                {"error": "This confirmation has expired. Ask Nova again."}
            ), 400
        if payload["uid"] != session["user_id"]:
            return jsonify(
                {"error": "This confirmation belongs to another account."}
            ), 403
        outcome = registry.call_tool(
            "issue_refund",
            {
                "order_id": payload["order_id"],
                "amount": payload["amount"],
                "reason": payload["reason"],
                "confirmed": True,
            },
            {
                "db_path": db_path,
                "user_id": session["user_id"],
                "trace_id": payload["trace"],
                "agent": agent,
                "role": session["role"],
                "human_confirmed": True,
            },
        )
        if outcome["isError"]:
            return jsonify({"ok": False, "message": outcome["content"]}), 409
        return jsonify(
            {"ok": True, "message": json.loads(outcome["content"])["message"]}
        )

    @app.post("/api/feedback")
    @login_required
    def api_feedback():
        body = request.get_json(silent=True) or {}
        rating = body.get("rating")
        if rating not in ("up", "down"):
            return jsonify({"error": "rating must be up or down"}), 400
        trace = store.get_trace(db_path, str(body.get("trace_id")), session["user_id"])
        if trace is None:
            return jsonify({"error": "trace not found"}), 404
        store.log_feedback(db_path, trace["id"], rating)
        if rating == "down" and trace["cache_entry_id"]:
            store.cache_delete(db_path, trace["cache_entry_id"])
        return jsonify({"ok": True})

    @app.get("/traces")
    @login_required
    def traces():
        rows = store.list_traces(db_path, session["user_id"], include_all=_is_staff())
        return render_template(
            "traces.html", traces=rows, spend=store.total_spend(db_path)
        )

    @app.get("/traces/<trace_id>")
    @login_required
    def trace_detail(trace_id):
        trace = store.get_trace(
            db_path, trace_id, session["user_id"], include_all=_is_staff()
        )
        if trace is None:
            abort(404)
        return render_template(
            "trace.html", trace=trace, steps=store.trace_steps(db_path, trace_id)
        )

    @app.get("/security")
    @login_required
    def security():
        events = store.list_guardrail_events(
            db_path, session["user_id"], include_all=_is_staff()
        )
        return render_template(
            "security.html",
            events=events,
            l2_status=guardrails.L2_STATUS,
            policy=guardrails.POLICY,
            money=guardrails.money,
        )

    @app.get("/evals")
    @staff_required
    def evals():
        runs = store.list_eval_runs(db_path)
        results = store.list_eval_results(db_path, runs[0]["id"]) if runs else []
        return render_template("evals.html", runs=runs, results=results)

    @app.get("/prompts")
    @staff_required
    def prompts():
        active = load_active()
        files = [
            {
                "version": path.stem,
                "text": path.read_text(encoding="utf-8"),
                "live": path.stem == active["prompt_version"],
            }
            for path in sorted(PROMPTS_DIR.glob("*.txt"))
        ]
        return render_template("prompts.html", prompts=files, active=active)

    @app.get("/staff/approvals")
    @staff_required
    def approvals():
        store.expire_approvals(db_path)
        rows = store.list_approvals(db_path)
        for row in rows:
            row["description"] = parcelco.describe_approval(db_path, row)
            row["checks"] = json.loads(row["policy_checks_json"])
        return render_template(
            "approvals.html",
            pending=[r for r in rows if r["status"] == "pending"],
            decided=[r for r in rows if r["status"] != "pending"],
        )

    @app.post("/staff/approvals/<int:approval_id>/<decision>")
    @staff_required
    def decide_approval(approval_id, decision):
        if decision == "approve":
            outcome = parcelco.execute_approved(
                db_path,
                approval_id,
                session["user_id"],
                request.form.get("args_hash", ""),
            )
        elif decision == "reject":
            outcome = parcelco.reject(db_path, approval_id, session["user_id"])
        else:
            abort(404)
        flash(outcome["message"], "ok" if outcome["ok"] else "error")
        return redirect(url_for("approvals"))

    return app


def main():
    parser = argparse.ArgumentParser(description="Our Own AI agent platform")
    parser.add_argument(
        "--create-staff", metavar="USERNAME", help="create a staff login"
    )
    parser.add_argument("--port", type=int, default=5000)
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if options.create_staff:
        db_path = os.environ.get("AGENT_DB") or str(ROOT / "data" / "agent.db")
        store.init_db(db_path)
        store.create_user(
            db_path,
            options.create_staff,
            auth.hash_password(getpass.getpass()),
            "staff",
        )
        return
    create_app().run(host="127.0.0.1", port=options.port)


if __name__ == "__main__":
    main()
