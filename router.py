"""Deterministic model routing: tiers, fallback chains, cooldown and budget guard."""

import json
import random
import re
import time
from pathlib import Path

import store
from llm import LLMError

CONFIG_DIR = Path(__file__).parent / "config"

ORDER_OR_NUMBER = re.compile(r"\d{3,}")
PERSONAL = re.compile(
    r"\b(my|mine|me|i'm|i've|track|cancel|ticket|complain\w*|status|refund me)\b", re.I
)
SIDE_EFFECT = re.compile(r"\b(refund|cancel|book|charge|ticket)\b", re.I)
SIMPLE_MAX_CHARS = 400
COMPLEX_MIN_CHARS = 4000

COOLDOWNS = {}


class RouterError(Exception):
    def __init__(self, message, decision):
        super().__init__(message)
        self.decision = decision


def load_routing(path=None):
    return json.loads(Path(path or CONFIG_DIR / "routing.json").read_text())


def load_pricing(path=None):
    return json.loads(Path(path or CONFIG_DIR / "pricing.json").read_text())


def classify_route(text) -> str:
    """'faq' for public questions, 'account' for anything tied to a person or order."""
    return "account" if ORDER_OR_NUMBER.search(text) or PERSONAL.search(text) else "faq"


def classify_tier(text, multi_turn=False) -> str:
    if len(text) > COMPLEX_MIN_CHARS:
        return "complex"
    route = classify_route(text)
    if route == "account" and SIDE_EFFECT.search(text):
        return "complex"
    if route == "faq" and len(text) < SIMPLE_MAX_CHARS and not multi_turn:
        return "simple"
    return "standard"


def cost_usd(model, usage, pricing=None) -> float:
    rates = (pricing or load_pricing())["usd_per_million"].get(model)
    if rates is None:
        return 0.0
    cached = usage.get("cached_tokens", 0)
    fresh = max(usage.get("prompt_tokens", 0) - cached, 0)
    return (
        fresh * rates["input"]
        + cached * rates["cached_input"]
        + usage.get("completion_tokens", 0) * rates["output"]
    ) / 1_000_000


def _cooling(state, name, now):
    return state.get(name, {}).get("until", 0) > now


def _record_failure(state, name, now, cooldown):
    entry = state.setdefault(name, {"failures": [], "until": 0})
    entry["failures"] = [
        t for t in entry["failures"] if now - t < cooldown["window_seconds"]
    ]
    entry["failures"].append(now)
    if len(entry["failures"]) >= cooldown["failures"]:
        entry["until"] = now + cooldown["seconds"]
        entry["failures"] = []


def _backoff(routing, attempt, retry_after):
    if retry_after is not None:
        return min(retry_after, routing["max_retry_after_seconds"])
    jitter = routing["backoff_jitter"]
    return (
        routing["backoff_seconds"] * 2**attempt * random.uniform(1 - jitter, 1 + jitter)
    )


def route(
    messages, tier, client_factory, db_path, tools=None, routing=None, chain=None,
    temperature=None, state=None, sleep=time.sleep, clock=time.time,
):  # fmt: skip
    """Call the first healthy model in the tier's chain.

    Returns (response, decision); raises RouterError when nothing answered.
    decision records every skip and attempt for the trace.
    """
    routing = routing or load_routing()
    state = COOLDOWNS if state is None else state
    names = list(chain or routing["chains"][tier])
    spend = store.total_spend(db_path)
    over_budget = spend >= routing["budget_cap_usd"]
    if over_budget:
        names = [name for name in names if routing["models"][name]["free"]]
    decision = {
        "tier": tier,
        "chain": names,
        "over_budget": over_budget,
        "spend_usd": spend,
        "skipped": [],
        "attempts": [],
        "fallback_used": False,
        "model_used": None,
    }
    temperature = routing["temperature"] if temperature is None else temperature
    timeout = routing["timeouts"][tier]
    limits = routing["attempts"]
    total = 0

    for position, name in enumerate(names):
        spec = routing["models"][name]
        client = client_factory(spec["provider"])
        if client is None:
            decision["skipped"].append({"model": name, "why": "no api key"})
            continue
        if _cooling(state, name, clock()):
            decision["skipped"].append({"model": name, "why": "cooldown"})
            continue
        allowed = limits["primary"] if position == 0 else limits["fallback"]
        for attempt in range(allowed):
            if total >= limits["total"]:
                break
            total += 1
            started = time.monotonic()
            record = {"provider": spec["provider"], "model": spec["model"]}
            decision["attempts"].append(record)
            try:
                response = client.chat(
                    messages,
                    tools=tools,
                    model=spec["model"],
                    timeout=timeout,
                    temperature=temperature,
                )
            except LLMError as error:
                record.update(
                    status=error.status,
                    error=error.message,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                # A bad request says nothing about provider health, and any
                # user could otherwise trigger a cooldown for everyone.
                if not error.retryable:
                    raise RouterError(error.message, decision)
                _record_failure(state, name, clock(), routing["cooldown"])
                if attempt + 1 < allowed and total < limits["total"]:
                    sleep(_backoff(routing, attempt, error.retry_after))
                continue
            record.update(
                status="ok",
                usage=response["usage"],
                latency_ms=int((time.monotonic() - started) * 1000),
                finish_reason=response.get("finish_reason"),
            )
            decision["model_used"] = spec["model"]
            decision["fallback_used"] = position > 0 or bool(decision["skipped"])
            return response, decision
    raise RouterError("no model could answer", decision)


def judge_client(drafted_by, client_factory, routing=None, db_path=None):
    """Prefer a different provider from the one that drafted the action, so a
    model never grades itself. Returns (client, model) or (None, None)."""
    routing = routing or load_routing()
    specs = [spec for spec in routing["models"].values() if spec["provider"] != "demo"]
    if db_path and store.total_spend(db_path) >= routing["budget_cap_usd"]:
        specs = [spec for spec in specs if spec["free"]]
    for spec in sorted(specs, key=lambda s: s["provider"] == drafted_by):
        client = client_factory(spec["provider"])
        if client is not None:
            return client, spec["model"]
    return None, None
