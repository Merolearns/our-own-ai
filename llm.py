"""Provider clients behind one chat() shape. Keys come from the environment only."""

import json
import os
import re
import uuid

import requests

from rag import STOPWORDS, WORD
from tools.registry import to_gemini_tools, to_openai_tools

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

DEMO_LABEL = "demo mode — no live model"


class LLMError(Exception):
    def __init__(self, status, message, retryable, retry_after=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


def _post(url, headers, body, timeout):
    """POST and return JSON, turning every failure into a typed LLMError."""
    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout:
        raise LLMError(0, "request timed out", True)
    except requests.RequestException as exc:
        # The exception text can hold a full URL, so report the class only.
        raise LLMError(0, f"network error: {type(exc).__name__}", True)
    if response.status_code != 200:
        retry_after = response.headers.get("Retry-After", "")
        raise LLMError(
            response.status_code,
            f"provider returned HTTP {response.status_code}",
            response.status_code in RETRYABLE_STATUS,
            float(retry_after) if retry_after.replace(".", "", 1).isdigit() else None,
        )
    try:
        return response.json()
    except ValueError:
        raise LLMError(response.status_code, "provider returned invalid JSON", True)


def _result(text, tool_calls, prompt, completion, cached, model, provider, finish):
    return {
        "text": text or "",
        "tool_calls": tool_calls,
        "usage": {
            "prompt_tokens": prompt or 0,
            "completion_tokens": completion or 0,
            "cached_tokens": cached or 0,
        },
        "model": model,
        "provider": provider,
        "finish_reason": finish,
    }


class DeepSeekClient:
    provider = "deepseek"

    def __init__(self, api_key=None):
        self._key = api_key or os.environ.get("DEEPSEEK_API_KEY")

    def chat(self, messages, tools=None, model=None, timeout=30, temperature=0.2):
        body = {
            "model": model,
            "messages": self._wire(messages),
            "temperature": temperature,
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        data = _post(
            DEEPSEEK_URL, {"Authorization": f"Bearer {self._key}"}, body, timeout
        )
        return self._parse(data, model)

    @staticmethod
    def _wire(messages):
        wire = []
        for message in messages:
            if message["role"] == "assistant" and message.get("tool_calls"):
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": (
                                        call["arguments"]
                                        if isinstance(call["arguments"], str)
                                        else json.dumps(call["arguments"])
                                    ),
                                },
                            }
                            for call in message["tool_calls"]
                        ],
                    }
                )
            elif message["role"] == "tool":
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": message["tool_call_id"],
                        "content": message["content"],
                    }
                )
            else:
                wire.append({"role": message["role"], "content": message["content"]})
        return wire

    def _parse(self, data, model):
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(200, "unexpected response shape", True)
        calls = [
            {
                "id": call.get("id") or uuid.uuid4().hex,
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
            }
            for call in message.get("tool_calls") or []
        ]
        usage = data.get("usage") or {}
        return _result(
            message.get("content"),
            calls,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("prompt_cache_hit_tokens"),
            data.get("model") or model,
            self.provider,
            choice.get("finish_reason"),
        )


class GeminiClient:
    provider = "gemini"

    def __init__(self, api_key=None):
        self._key = api_key or os.environ.get("GEMINI_API_KEY")

    @staticmethod
    def _supports_temperature(model):
        """Sampling params are deprecated for Gemini 3.5+ (ignored today, an
        error in future generations), so they are only sent to older models."""
        match = re.search(r"gemini-(\d+)", model or "")
        return not (match and int(match.group(1)) >= 3)

    def chat(self, messages, tools=None, model=None, timeout=30, temperature=0.2):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        body = {"contents": self._wire([m for m in messages if m["role"] != "system"])}
        if self._supports_temperature(model):
            body["generationConfig"] = {"temperature": temperature}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = to_gemini_tools(tools)
        # The key goes in a header because URLs end up in logs.
        data = _post(
            GEMINI_URL.format(model=model), {"x-goog-api-key": self._key}, body, timeout
        )
        return self._parse(data, model)

    @staticmethod
    def _wire(messages):
        contents = []
        for message in messages:
            if message["role"] == "tool":
                part = {
                    "functionResponse": {
                        "name": message["name"],
                        "response": {"result": message["content"]},
                    }
                }
                if (
                    contents
                    and contents[-1]["role"] == "user"
                    and "functionResponse" in contents[-1]["parts"][0]
                ):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
            elif message["role"] == "assistant":
                parts = [{"text": message["content"]}] if message.get("content") else []
                for call in message.get("tool_calls") or []:
                    arguments = call["arguments"]
                    part = {
                        "functionCall": {
                            "name": call["name"],
                            "args": json.loads(arguments)
                            if isinstance(arguments, str)
                            else arguments,
                        }
                    }
                    if call.get("thought_signature"):
                        part["thoughtSignature"] = call["thought_signature"]
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
            else:
                contents.append(
                    {"role": "user", "parts": [{"text": message["content"]}]}
                )
        return contents

    def _parse(self, data, model):
        candidates = data.get("candidates") or []
        if not candidates:
            # Blocked by the provider's safety filter. Asking another model would dodge it.
            raise LLMError(400, "provider returned no answer (content blocked)", False)
        candidate = candidates[0]
        if candidate.get("finishReason") in (
            "SAFETY",
            "PROHIBITED_CONTENT",
            "RECITATION",
        ):
            raise LLMError(400, "provider blocked the content", False)
        text, calls = [], []
        for part in (candidate.get("content") or {}).get("parts") or []:
            if "text" in part and not part.get("thought"):
                text.append(part["text"])
            if "functionCall" in part:
                call = {
                    "id": part["functionCall"].get("id") or uuid.uuid4().hex,
                    "name": part["functionCall"]["name"],
                    "arguments": part["functionCall"].get("args") or {},
                }
                if part.get("thoughtSignature"):
                    call["thought_signature"] = part["thoughtSignature"]
                calls.append(call)
        usage = data.get("usageMetadata") or {}
        return _result(
            "".join(text),
            calls,
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
            usage.get("cachedContentTokenCount"),
            model,
            self.provider,
            candidate.get("finishReason"),
        )


ORDER_REF = re.compile(r"(?<![$\d.])#?\b(\d{4,})\b")
AMOUNT = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
SOURCE = re.compile(
    r"^\[(\d+)\] [^\n]*\n<untrusted-data[^>]*>(.*?)</untrusted-data>", re.S | re.M
)
GREETING = re.compile(
    r"^\s*(hi|hello|hey|good (morning|afternoon|evening))\b[\s!.,]*$", re.I
)


class DemoClient:
    """Rule-based stand-in used when no API key is set. It is not a model: it
    reads intent with regexes and answers from templates and retrieved text.

    Agent-aware: the system prompt names the agent (Parcelco Nova or Scoopy
    Doog), and the stand-in answers for that agent only, with that agent's
    tools. Parcelco behaviour is unchanged."""

    provider = "demo"

    @staticmethod
    def _agent(messages):
        system = next(
            (m["content"] for m in messages if m["role"] == "system"), ""
        )
        return "scoopy" if "Scoopy Doog" in system else "parcelco"

    @staticmethod
    def _greeting(agent):
        if agent == "scoopy":
            return (
                "Hi, I'm the Scoopy Doog assistant. I can help with dog waste "
                "removal, pricing, visits, and booking questions."
            )
        return (
            "Hi, I'm Nova from Parcelco. I can help with orders, refunds and "
            "delivery questions."
        )

    TICKET_INTENT = re.compile(
        r"\b(call\s+me\s+back|callback|call\s+back|open\s+a\s+ticket|support\s+ticket)\b",
        re.I,
    )
    SERVICE_AREA_INTENT = re.compile(
        r"\b(cover|service area|do you (come to|serve|work in)|coverage)\b|\bserve\b",
        re.I,
    )
    CITY_AFTER = re.compile(r"(?:cover|serve|in|at|to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")
    ZIP = re.compile(r"\b(\d{5})\b")
    ORDER_MENTION = re.compile(r"order\s*#?\s*(\d+)", re.I)

    def chat(self, messages, tools=None, model=None, timeout=None, temperature=0):
        user_text = next(
            m["content"] for m in reversed(messages) if m["role"] == "user"
        )
        tool_names = {tool["name"] for tool in tools or []}
        last = messages[-1]
        if last["role"] == "tool":
            return self._after_tool(last, user_text, tool_names, model)
        return self._first_step(messages, user_text, tool_names, model)

    def _reply(self, text, model):
        return _result(
            text, [], 0, 0, 0, model or "demo-template", self.provider, "stop"
        )

    def _call(self, name, arguments, model):
        call = {"id": uuid.uuid4().hex, "name": name, "arguments": arguments}
        return _result(
            "", [call], 0, 0, 0, model or "demo-template", self.provider, "tool_calls"
        )

    @staticmethod
    def _wants_refund(text):
        lowered = text.lower()
        return "refund" in lowered and "policy" not in lowered

    def _first_step(self, messages, text, tool_names, model):
        if GREETING.match(text):
            return self._reply(self._greeting(self._agent(messages)), model)
        order = ORDER_REF.search(text)
        lowered = text.lower()
        if self._wants_refund(text) and "check_order_status" in tool_names:
            if not order:
                return self._reply(
                    "I can look at one order at a time. Which order number should I check?",
                    model,
                )
            return self._call("check_order_status", {"order_id": order.group(1)}, model)
        if "open_ticket" in tool_names and self.TICKET_INTENT.search(lowered):
            args = {"subject": text[:80], "description": text}
            # An order number only counts when the customer names an order
            # ("order #1002"). A bare digit run — a phone number, a zip —
            # is not an order, and attaching it would fail the call.
            # Scoopy has no order tool at all, so it never gets an order_id.
            mentioned = self.ORDER_MENTION.search(text)
            if mentioned and "check_order_status" in tool_names:
                args["order_id"] = mentioned.group(1)
                args["subject"] = f"Support for order {mentioned.group(1)}"
            return self._call("open_ticket", args, model)
        if "check_service_area" in tool_names and self.SERVICE_AREA_INTENT.search(lowered):
            args = {}
            city = self.CITY_AFTER.search(text)
            if city:
                args["city"] = city.group(1)
            zip_match = self.ZIP.search(text)
            if zip_match:
                args["zip_code"] = zip_match.group(1)
            return self._call("check_service_area", args, model)
        if order and "check_order_status" in tool_names:
            return self._call("check_order_status", {"order_id": order.group(1)}, model)
        return self._reply(self._grounded(messages[0]["content"], text), model)

    def _after_tool(self, last, user_text, tool_names, model):
        try:
            data = json.loads(last["content"])
        except ValueError:
            return self._reply(f"I couldn't do that. {last['content']}", model)
        if (
            last["name"] == "check_order_status"
            and self._wants_refund(user_text)
            and "issue_refund" in tool_names
        ):
            explicit = AMOUNT.search(user_text)
            amount = (
                float(explicit.group(1))
                if explicit
                else float(data["total"].lstrip("$").replace(",", ""))
            )
            return self._call(
                "issue_refund",
                {
                    "order_id": data["order_id"],
                    "amount": amount,
                    "reason": self._reason(user_text),
                },
                model,
            )
        return self._reply(self._describe(last["name"], data), model)

    @staticmethod
    def _reason(text):
        lowered = text.lower()
        for word, reason in (
            ("damag", "damaged"),
            ("lost", "lost"),
            ("missing", "lost"),
            ("late", "late"),
        ):
            if word in lowered:
                return reason
        return "other"

    @staticmethod
    def _describe(name, data):
        if name == "open_ticket":
            return f"I opened support ticket #{data['ticket_id']} for you."
        if name == "check_service_area":
            reply = f"We serve {data['service_area']}."
            if data.get("note"):
                reply += f" {data['note']}"
            return reply
        if name == "issue_refund":
            order, amount = data["order_id"], data["amount"]
            if data["status"] == "proposal":
                return f"A refund of {amount} on order #{order} is above the automatic limit, so I sent it to staff for approval. Nothing has been refunded yet."
            if data["status"] == "confirmation_required":
                return f"I can refund {amount} on order #{order}. Press Confirm to go ahead."
            return f"Done. Refunded {amount} on order #{order}."
        reply = f"Order #{data['order_id']} is {data['status']} with {data['carrier']}. Latest update: {data['latest_event']}. Order total: {data['total']}."
        if data.get("refund"):
            reply += f" Refunded: {data['refund']}."
        if data.get("refund_pending_staff_approval"):
            reply += " A refund is waiting for staff approval."
        return reply

    @staticmethod
    def _grounded(system_prompt, question):
        """Quote the sentences from the retrieved sources that best match the question."""
        terms = {w[:5] for w in WORD.findall(question.lower()) if w not in STOPWORDS}
        scored = []
        for number, body in SOURCE.findall(system_prompt)[:3]:
            lines = body.split("\n", 1)
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", lines[-1]):
                cleaned = sentence.strip(" -*\t").replace("**", "")
                if cleaned.startswith("#"):
                    continue  # headings are not answers; quoting one hides the fact
                overlap = len(terms & {w[:5] for w in WORD.findall(cleaned.lower())})
                if overlap and len(cleaned) > 15:
                    scored.append((overlap, -len(scored), cleaned, number))
        if not scored:
            return "I don't know. That isn't in Parcelco's documents. I can open a support ticket if you'd like."
        best = sorted(scored, reverse=True)[:3]
        return " ".join(f"{cleaned} [{number}]" for _, _, cleaned, number in best)


def live_providers():
    return [
        provider
        for provider, env in (
            ("deepseek", "DEEPSEEK_API_KEY"),
            ("gemini", "GEMINI_API_KEY"),
        )
        if os.environ.get(env)
    ]


def make_client(provider):
    """A client for provider, or None when its key is not set."""
    if provider == "demo":
        return DemoClient()
    if provider not in live_providers():
        return None
    return DeepSeekClient() if provider == "deepseek" else GeminiClient()
