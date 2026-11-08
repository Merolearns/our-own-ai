"""One registry for every tool, in the shape MCP's tools/list returns."""

import json
import logging
import time

from jsonschema import Draft202012Validator

import store
from tools import parcelco

log = logging.getLogger(__name__)

REGISTRY = {"parcelco": parcelco.TOOLS}

# Who may call what. A tool missing here does not exist for that role. Staff rule
# on refund proposals in the approval queue; they never call issue_refund.
ALLOW_LIST = {
    "parcelco": {
        "customer": {"check_order_status", "issue_refund", "open_ticket"},
        "staff": {"check_order_status", "open_ticket"},
        "admin": {"check_order_status", "open_ticket"},
    }
}


def _allowed(agent, role):
    return ALLOW_LIST.get(agent, {}).get(role, set())


def get_spec(agent, name):
    return REGISTRY.get(agent, {}).get(name)


def list_tools(agent, role="customer"):
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        }
        for name, spec in REGISTRY.get(agent, {}).items()
        if name in _allowed(agent, role)
    ]


def to_openai_tools(tools):
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["inputSchema"],
            },
        }
        for tool in tools
    ]


def _gemini_schema(schema):
    """Gemini's functionDeclarations accept only part of JSON Schema."""
    drop = {"additionalProperties", "exclusiveMinimum", "minLength"}
    if isinstance(schema, dict):
        return {
            key: _gemini_schema(value)
            for key, value in schema.items()
            if key not in drop
        }
    return schema


def to_gemini_tools(tools):
    return [
        {
            "functionDeclarations": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": _gemini_schema(tool["inputSchema"]),
                }
                for tool in tools
            ]
        }
    ]


def _validate(schema, args):
    return [
        f"#/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in Draft202012Validator(schema).iter_errors(args)
    ]


def call_tool(name, args, ctx):
    """MCP tools/call equivalent. Always returns {"content": str, "isError": bool}."""
    started = time.monotonic()
    agent = ctx.get("agent", "parcelco")
    role = ctx.get("role", "customer")
    spec = get_spec(agent, name)
    parsed = args

    def finish(content, is_error):
        _log_call(ctx, name, parsed, content, is_error, spec, started)
        return {"content": content, "isError": is_error}

    if spec is None:
        return finish(
            f"Unknown tool '{name}'. Available: {sorted(_allowed(agent, role))}", True
        )
    if name not in _allowed(agent, role):
        _audit(ctx, "tool-not-allowed", {"tool": name, "role": role})
        return finish(f"Tool '{name}' is not available to this user.", True)
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return finish("Arguments are not valid JSON, nothing was executed.", True)
    if not isinstance(parsed, dict):
        return finish("Arguments must be a JSON object, nothing was executed.", True)
    errors = _validate(spec["inputSchema"], parsed)
    if errors:
        return finish(
            "Invalid arguments, nothing was executed:\n- " + "\n- ".join(errors), True
        )
    try:
        result = spec["handler"](parsed, ctx)
    except parcelco.ToolError as exc:
        return finish(str(exc), True)
    except Exception:
        log.exception("tool %s failed", name)
        return finish("The tool failed unexpectedly. Nothing further was done.", True)
    return finish(json.dumps(result), False)


def _audit(ctx, trigger, detail):
    try:
        store.log_guardrail_event(
            ctx["db_path"], ctx.get("user_id"), "L6", "blocked", trigger, detail
        )
    except Exception:
        log.exception("could not write guardrail event")


def _log_call(ctx, name, args, content, is_error, spec, started):
    # Logging must never break a turn.
    try:
        store.log_tool_call(
            ctx["db_path"],
            ctx.get("trace_id") or "",
            name,
            args if isinstance(args, (dict, list)) else {"raw": str(args)},
            content,
            not is_error,
            bool(spec and spec["side_effect"]),
            int((time.monotonic() - started) * 1000),
        )
    except Exception:
        log.exception("could not log tool call")
