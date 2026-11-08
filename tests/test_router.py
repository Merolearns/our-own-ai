import pytest

import router
from llm import LLMError


@pytest.fixture
def run(db, fake):
    FakeClient, _ = fake

    def go(clients, tier="standard", state=None, clock=lambda: 1000.0, routing=None):
        built = {name: FakeClient(name, script) for name, script in clients.items()}
        sleeps = []
        state = {} if state is None else state
        try:
            outcome = router.route(
                [{"role": "user", "content": "x"}], tier, built.get, db, routing=routing,
                state=state, sleep=sleeps.append, clock=clock,
            )
        except router.RouterError as error:
            outcome = error
        return outcome, built, sleeps, state

    return go


@pytest.mark.parametrize(
    "text,multi,tier",
    [
        ("What is your refund policy?", False, "simple"),
        ("What is your refund policy?", True, "standard"),
        ("Where is my order?", False, "standard"),
        ("Please refund order 1001", False, "complex"),
        ("x" * 4001, False, "complex"),
        ("y " * 300, False, "standard"),
    ],
)
def test_tiers(text, multi, tier):
    assert router.classify_tier(text, multi) == tier


def test_routes():
    assert router.classify_route("How long does shipping take?") == "faq"
    assert router.classify_route("Where is order 1001?") == "account"
    assert router.classify_route("Where is my parcel?") == "account"


def test_simple_prefers_gemini_flash_lite(run, fake):
    _, reply = fake
    (response, decision), built, _, _ = run({"gemini": [reply()], "deepseek": [reply()]}, "simple")
    assert built["gemini"].calls[0]["model"] == "gemini-3.5-flash-lite"
    assert built["deepseek"].calls == [] and decision["tier"] == "simple"


@pytest.mark.parametrize("tier", ["standard", "complex"])
def test_standard_prefers_deepseek(run, fake, tier):
    _, reply = fake
    _, built, _, _ = run({"gemini": [reply()], "deepseek": [reply()]}, tier)
    assert built["deepseek"].calls[0]["model"] == "deepseek-flash" and built["gemini"].calls == []


def test_timeouts_by_tier(run, fake):
    _, reply = fake
    _, built, _, _ = run({"deepseek": [reply()]}, "complex")
    assert built["deepseek"].calls[0]["timeout"] == 60


def test_429_retries_then_falls_back(run, fake):
    _, reply = fake
    limit = LLMError(429, "rate", True)
    (response, decision), built, sleeps, _ = run({"deepseek": [limit, limit], "gemini": [reply(provider="gemini")]})
    assert response["provider"] == "gemini" and decision["fallback_used"] is True
    assert len(built["deepseek"].calls) == 2 and len(sleeps) == 1
    assert 1.5 <= sleeps[0] <= 2.5
    assert len(decision["attempts"]) == 3


def test_retry_after_is_honoured_and_capped(run, fake):
    _, reply = fake
    _, _, sleeps, _ = run({"deepseek": [LLMError(429, "r", True, retry_after=7), reply()]})
    assert sleeps == [7]
    _, _, sleeps, _ = run({"deepseek": [LLMError(429, "r", True, retry_after=900), reply()]})
    assert sleeps == [30]


def test_total_attempts_never_exceed_three(run):
    error = LLMError(503, "down", True)
    outcome, built, _, _ = run({"deepseek": [error] * 5, "gemini": [error] * 5})
    assert isinstance(outcome, router.RouterError)
    assert len(built["deepseek"].calls) + len(built["gemini"].calls) == 3


@pytest.mark.parametrize("status", [400, 401])
def test_auth_and_bad_request_never_retry(run, fake, status):
    _, reply = fake
    outcome, built, sleeps, _ = run({"deepseek": [LLMError(status, "no", False)], "gemini": [reply()]})
    assert isinstance(outcome, router.RouterError)
    assert len(built["deepseek"].calls) == 1 and built["gemini"].calls == [] and sleeps == []


def test_missing_key_is_skipped(run, fake):
    _, reply = fake
    (_, decision), _, _, _ = run({"gemini": [reply(provider="gemini")]})
    assert decision["skipped"] == [{"model": "deepseek_flash", "why": "no api key"}]


def test_cooldown_after_two_failures(run, fake):
    _, reply = fake
    limit = LLMError(429, "rate", True)
    state = {}
    run({"deepseek": [limit, limit], "gemini": [reply()]}, state=state)
    (_, decision), built, _, _ = run({"deepseek": [reply()], "gemini": [reply()]}, state=state, clock=lambda: 1030.0)
    assert built["deepseek"].calls == [] and decision["skipped"][0]["why"] == "cooldown"
    _, built, _, _ = run({"deepseek": [reply()], "gemini": [reply()]}, state=state, clock=lambda: 1061.0)
    assert len(built["deepseek"].calls) == 1


def test_bad_request_does_not_cool_down_the_model(run):
    state = {}
    for _ in range(3):
        run({"deepseek": [LLMError(400, "bad", False)]}, state=state)
    assert state == {}


def test_budget_guard_uses_free_model_only(run, fake, db):
    import store

    _, reply = fake
    store.log_trace(db, {"id": "t", "session_id": "s", "agent_id": "a", "config_version": "c", "input_text": "i", "total_cost_usd": 5.5})
    (_, decision), built, _, _ = run({"deepseek": [reply()], "gemini": [reply(provider="gemini")]})
    assert decision["over_budget"] is True and built["deepseek"].calls == [] and len(built["gemini"].calls) == 1


def test_costs_from_price_table():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "cached_tokens": 0}
    assert router.cost_usd("deepseek-flash", usage) == pytest.approx(1.5)
    assert router.cost_usd("gemini-3.5-flash-lite", usage) == 0


def test_cached_tokens_billed_at_cached_rate():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0, "cached_tokens": 1_000_000}
    assert router.cost_usd("deepseek-flash", usage) == pytest.approx(0.006)


def test_judge_prefers_the_other_provider():
    clients = {"deepseek": "d", "gemini": "g"}
    assert router.judge_client("deepseek", clients.get) == ("g", "gemini-3.5-flash-lite")
    assert router.judge_client("gemini", clients.get) == ("d", "deepseek-flash")


def test_judge_falls_back_to_same_provider():
    assert router.judge_client("deepseek", {"deepseek": "d"}.get) == ("d", "deepseek-flash")


def test_config_holds_no_quota_numbers():
    spec = router.load_routing()["models"]["gemini_flash_lite"]
    assert spec["quota_verified"] is False
    assert not {"rpm", "rpd", "tpm"} & set(spec)
