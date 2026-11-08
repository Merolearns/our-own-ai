import json

import pytest
import requests

import llm
from tools import registry

MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "where is it"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "check_order_status", "arguments": {"order_id": "1"}, "thought_signature": "sig"}]},
    {"role": "tool", "tool_call_id": "c1", "name": "check_order_status", "content": "{}"},
]


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}

    def json(self):
        if self.body is None:
            raise ValueError("no json")
        return self.body


@pytest.fixture
def post(monkeypatch):
    calls = []

    def install(response):
        def fake(url, headers=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(llm.requests, "post", fake)
        return calls

    return install


DEEPSEEK_OK = {
    "model": "deepseek-flash",
    "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [{"id": "x", "function": {"name": "n", "arguments": "{\"a\": 1}"}}]}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "prompt_cache_hit_tokens": 2},
}
GEMINI_OK = {
    "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hello"}, {"functionCall": {"name": "n", "args": {"a": 1}}, "thoughtSignature": "s"}]}}],
    "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4},
}


def test_deepseek_shape(post):
    calls = post(Response(body=DEEPSEEK_OK))
    out = llm.DeepSeekClient("k").chat(MESSAGES, tools=registry.list_tools("parcelco"), model="m")
    assert set(out) == {"text", "tool_calls", "usage", "model", "provider", "finish_reason"}
    assert out["tool_calls"][0] == {"id": "x", "name": "n", "arguments": "{\"a\": 1}"}
    assert out["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "cached_tokens": 2}
    body = calls[0]["json"]
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"order_id": "1"}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "{}"}
    assert body["tools"][0]["type"] == "function"
    assert calls[0]["headers"]["Authorization"] == "Bearer k"


def test_gemini_shape_and_key_not_in_url(post):
    calls = post(Response(body=GEMINI_OK))
    out = llm.GeminiClient("secretkey").chat(MESSAGES, tools=registry.list_tools("parcelco"), model="m")
    assert out["text"] == "hello" and out["tool_calls"][0]["thought_signature"] == "s"
    assert out["usage"]["prompt_tokens"] == 9
    request = calls[0]
    assert "secretkey" not in request["url"] and request["headers"]["x-goog-api-key"] == "secretkey"
    contents = request["json"]["contents"]
    assert request["json"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert contents[1]["role"] == "model" and contents[1]["parts"][0]["thoughtSignature"] == "sig"
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "check_order_status"


@pytest.mark.parametrize("client", [llm.DeepSeekClient("k"), llm.GeminiClient("k")])
@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (503, True), (400, False), (401, False), (403, False)])
def test_error_typing(post, client, status, retryable):
    post(Response(status=status, headers={"Retry-After": "3"}))
    with pytest.raises(llm.LLMError) as caught:
        client.chat([{"role": "user", "content": "x"}], model="m")
    assert caught.value.status == status and caught.value.retryable is retryable and caught.value.retry_after == 3


def test_timeout_is_retryable(post):
    post(requests.Timeout())
    with pytest.raises(llm.LLMError) as caught:
        llm.DeepSeekClient("k").chat([{"role": "user", "content": "x"}], model="m")
    assert caught.value.status == 0 and caught.value.retryable


def test_network_error_hides_details(post):
    post(requests.ConnectionError("https://x?key=secret"))
    with pytest.raises(llm.LLMError) as caught:
        llm.GeminiClient("k").chat([{"role": "user", "content": "x"}], model="m")
    assert "secret" not in caught.value.message


def test_gemini_safety_block_never_retries(post):
    post(Response(body={"candidates": []}))
    with pytest.raises(llm.LLMError) as caught:
        llm.GeminiClient("k").chat([{"role": "user", "content": "x"}], model="m")
    assert caught.value.retryable is False


def test_clients_need_keys(monkeypatch):
    assert llm.live_providers() == [] and llm.make_client("deepseek") is None
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert llm.live_providers() == ["gemini"] and isinstance(llm.make_client("gemini"), llm.GeminiClient)


def demo_step(user, tools=True, system="", extra=()):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}, *extra]
    return llm.DemoClient().chat(messages, tools=registry.list_tools("parcelco") if tools else None)


def test_demo_status_lookup():
    assert demo_step("Where is order #1001?")["tool_calls"][0]["arguments"] == {"order_id": "1001"}


def test_demo_refund_looks_up_order_first():
    assert demo_step("Refund order #1004 for $999.")["tool_calls"][0]["name"] == "check_order_status"


def test_demo_uses_the_stated_amount():
    tool = {"role": "tool", "name": "check_order_status", "content": json.dumps({"order_id": "1004", "total": "$25.00"})}
    call = demo_step("Refund order #1004 for $999.", extra=[tool])["tool_calls"][0]
    assert call["name"] == "issue_refund" and call["arguments"]["amount"] == 999


def test_demo_refund_needs_an_order():
    assert "Which order" in demo_step("I want a refund")["text"]


def test_demo_policy_question_does_not_call_tools():
    out = demo_step("What is your refund policy?")
    assert out["tool_calls"] == [] and "I don't know" in out["text"]


def test_demo_quotes_sources_with_citation():
    system = "[1] Refunds: Limits\n<untrusted-data source=\"kb\" id=\"x\">Refunds: Limits\nEach order can be refunded one time only.</untrusted-data>"
    assert demo_step("how many refund times per order", tools=False, system=system)["text"] == "Each order can be refunded one time only. [1]"


def test_demo_grounded_answer_skips_headings():
    system = (
        "[1] Refunds: Overview\n<untrusted-data source=\"kb\" id=\"x\">Refunds: Overview\n"
        "## How much can be refunded\n\n"
        "The most any refund can be is the order total. "
        "Nova can process a refund of up to $50 once the customer presses Confirm. "
        "Refunds above $50 need staff approval, and the $50 limit is not $500.\n</untrusted-data>\n"
        "[2] Returns: Overview\n<untrusted-data source=\"kb\" id=\"y\">Returns: Overview\n"
        "A damaged parcel qualifies for a refund under the refund policy, "
        "and photos help the support team resolve it quickly.\n</untrusted-data>"
    )
    out = demo_step("What is the refund policy?", tools=False, system=system)
    assert "$50" in out["text"] and "## How much" not in out["text"]
