import json

import pytest

import store
from tools import parcelco, registry


def call(name, args, ctx):
    return registry.call_tool(name, args, ctx)


def data(result):
    assert result["isError"] is False, result
    return json.loads(result["content"])


def refund_args(order="1001", amount=18.5, **extra):
    return {"order_id": order, "amount": amount, "reason": "damaged", **extra}


def test_lists_mcp_shape():
    tools = registry.list_tools("parcelco")
    assert {t["name"] for t in tools} == {"check_order_status", "issue_refund", "open_ticket"}
    assert set(tools[0]) == {"name", "description", "inputSchema"}


def test_staff_are_never_shown_the_refund_tool():
    assert "issue_refund" not in {t["name"] for t in registry.list_tools("parcelco", "staff")}


def test_openai_translation():
    tool = registry.list_tools("parcelco")[0]
    out = registry.to_openai_tools([tool])[0]
    assert out["type"] == "function"
    assert out["function"] == {"name": tool["name"], "description": tool["description"], "parameters": tool["inputSchema"]}


def test_gemini_translation_strips_unsupported_keys():
    declarations = registry.to_gemini_tools(registry.list_tools("parcelco"))[0]["functionDeclarations"]
    text = json.dumps(declarations)
    assert "additionalProperties" not in text and "exclusiveMinimum" not in text
    assert {d["name"] for d in declarations} == {"check_order_status", "issue_refund", "open_ticket"}


def test_check_order_happy_path(ctx):
    out = data(call("check_order_status", {"order_id": "#1001"}, ctx))
    assert out["status"] == "shipped" and out["total"] == "$18.50"


def test_order_note_injection_is_withheld(ctx, db):
    out = data(call("check_order_status", {"order_id": "1003"}, ctx))
    assert "ShipFast" not in json.dumps(out) and "withheld" in out["note"]
    assert store.list_guardrail_events(db, include_all=True)[0]["decision"] == "withheld"


def test_clean_order_note_is_wrapped(ctx):
    assert data(call("check_order_status", {"order_id": "1006"}, ctx))["note"].startswith("<untrusted-data")


def test_missing_and_foreign_orders_look_identical(ctx, db):
    other = store.create_user(db, "bob", "x")
    missing = call("check_order_status", {"order_id": "9999"}, ctx)
    foreign = call("check_order_status", {"order_id": "1001"}, {**ctx, "user_id": other})
    assert missing["isError"] and foreign["isError"]
    assert missing["content"].replace("9999", "N") == foreign["content"].replace("1001", "N")


def test_staff_can_see_any_order(ctx, staff):
    assert data(call("check_order_status", {"order_id": "1001"}, {**ctx, "user_id": staff, "role": "staff"}))


@pytest.mark.parametrize(
    "name,args",
    [
        ("issue_refund", {"order_id": "1001", "amount": "18.5", "reason": "damaged"}),
        ("issue_refund", {"order_id": "1001", "reason": "damaged"}),
        ("issue_refund", {"order_id": "1001", "amount": 1, "reason": "because"}),
        ("issue_refund", {"order_id": "1001", "amount": 1, "reason": "damaged", "extra": 1}),
        ("issue_refund", {"order_id": "1001", "amount": True, "reason": "damaged"}),
        ("issue_refund", {"order_id": "1001", "amount": -5, "reason": "damaged"}),
        ("check_order_status", {"order_id": 1001}),
        ("open_ticket", {"subject": "s", "description": "d", "priority": "urgent"}),
    ],
)
def test_bad_arguments_never_run(ctx, db, name, args):
    result = call(name, args, ctx)
    assert result["isError"] and result["content"].startswith("Invalid arguments")
    assert store.get_refund(db, "1001") is None


def test_malformed_json(ctx):
    result = call("check_order_status", "{not json", ctx)
    assert result["isError"] and "valid JSON" in result["content"]


def test_json_string_arguments_are_accepted(ctx):
    assert data(call("check_order_status", '{"order_id": "1001"}', ctx))["order_id"] == "1001"


def test_unknown_tool(ctx):
    result = call("refund_everything", {}, ctx)
    assert result["isError"] and "Unknown tool" in result["content"]


def test_allow_list_blocks_staff_refund(ctx, db, staff):
    result = call("issue_refund", refund_args(confirmed=True), {**ctx, "user_id": staff, "role": "staff", "human_confirmed": True})
    assert result["isError"] and store.get_refund(db, "1001") is None


def test_unknown_role_gets_nothing(ctx):
    assert call("check_order_status", {"order_id": "1001"}, {**ctx, "role": "guest"})["isError"]


def test_handler_exception_stays_generic(ctx, monkeypatch):
    def boom(args, ctx):
        raise RuntimeError("secret internal detail")

    monkeypatch.setitem(registry.REGISTRY["parcelco"]["check_order_status"], "handler", boom)
    result = call("check_order_status", {"order_id": "1001"}, ctx)
    assert result["isError"] and "secret" not in result["content"]


def test_every_call_is_logged(ctx, db):
    call("check_order_status", {"order_id": "1001"}, ctx)
    assert store.trace_steps(db, "t1")["tool_calls"][0]["tool_name"] == "check_order_status"


def test_unconfirmed_refund_does_not_execute(ctx, db):
    assert data(call("issue_refund", refund_args(), ctx))["status"] == "confirmation_required"
    assert store.get_refund(db, "1001") is None


def test_model_cannot_confirm_by_itself(ctx, db):
    assert data(call("issue_refund", refund_args(confirmed=True), ctx))["status"] == "confirmation_required"
    assert store.get_refund(db, "1001") is None


def test_ui_confirmed_refund_executes(ctx, db):
    out = data(call("issue_refund", refund_args(confirmed=True), {**ctx, "human_confirmed": True}))
    assert out["status"] == "refunded" and store.get_refund(db, "1001")["amount_cents"] == 1850


def test_refund_above_total_blocked(ctx, db):
    result = call("issue_refund", refund_args("1004", 999), ctx)
    assert result["isError"] and "$25.00" in result["content"]
    assert store.list_guardrail_events(db, include_all=True)[0]["layer"] == "L6"


def test_sub_cent_amount_blocked(ctx):
    assert call("issue_refund", refund_args(amount=1.005), ctx)["isError"]


def test_unknown_order_refund_blocked(ctx):
    assert "not found" in call("issue_refund", refund_args("9999", 1), ctx)["content"]


def test_foreign_order_refund_blocked(ctx, db):
    other = store.create_user(db, "bob", "x")
    assert call("issue_refund", refund_args(confirmed=True), {**ctx, "user_id": other, "human_confirmed": True})["isError"]
    assert store.get_refund(db, "1001") is None


def test_double_refund_blocked(ctx):
    confirmed = {**ctx, "human_confirmed": True}
    data(call("issue_refund", refund_args(confirmed=True), confirmed))
    second = call("issue_refund", refund_args(confirmed=True), confirmed)
    assert second["isError"] and "already been refunded" in second["content"]


def test_daily_cap_blocks_fourth(ctx):
    confirmed = {**ctx, "human_confirmed": True}
    for order, amount in (("1001", 18.5), ("1002", 40), ("1003", 32)):
        data(call("issue_refund", refund_args(order, amount, confirmed=True), confirmed))
    result = call("issue_refund", refund_args("1004", 25, confirmed=True), confirmed)
    assert result["isError"] and "3 of 3" in result["content"]


def test_over_limit_becomes_a_proposal_without_confirmation(ctx, db):
    out = data(call("issue_refund", refund_args("1005", 60), {**ctx, "request_text": "refund it", "judge_verdict": "yes: ok"}))
    assert out["status"] == "proposal" and store.get_refund(db, "1005") is None
    approval = store.get_approval(db, out["approval_id"])
    assert approval["status"] == "pending" and approval["request_text"] == "refund it"
    assert json.loads(approval["args_json"]) == {"order_id": "1005", "amount_cents": 6000, "reason": "damaged"}
    assert approval["trace_id"] == "t1" and len(json.loads(approval["policy_checks_json"])) == 7


def test_repeat_proposal_reuses_the_approval(ctx):
    first = data(call("issue_refund", refund_args("1005", 60), ctx))
    assert data(call("issue_refund", refund_args("1005", 60), ctx))["approval_id"] == first["approval_id"]


def test_threshold_is_config(ctx, monkeypatch):
    import guardrails

    monkeypatch.setitem(guardrails.POLICY, "auto_refund_max_cents", 1000)
    assert data(call("issue_refund", refund_args(), ctx))["status"] == "proposal"


def test_ticket_scoped_to_owner(ctx, db):
    assert data(call("open_ticket", {"order_id": "1001", "subject": "s", "description": "d"}, ctx))["priority"] == "normal"
    assert len(store.list_tickets(db, ctx["user_id"])) == 1
    other = store.create_user(db, "bob", "x")
    assert call("open_ticket", {"order_id": "1001", "subject": "s", "description": "d"}, {**ctx, "user_id": other})["isError"]


def _proposal(ctx, db):
    out = data(call("issue_refund", refund_args("1005", 60), ctx))
    return store.get_approval(db, out["approval_id"])


def test_staff_approval_executes_once(ctx, db, staff):
    approval = _proposal(ctx, db)
    assert parcelco.execute_approved(db, approval["id"], staff, approval["args_hash"])["ok"]
    assert store.get_approval(db, approval["id"])["status"] == "executed"
    assert store.get_refund(db, "1005")["amount_cents"] == 6000
    assert not parcelco.execute_approved(db, approval["id"], staff, approval["args_hash"])["ok"]


def test_customer_cannot_approve(ctx, db, customer):
    approval = _proposal(ctx, db)
    assert not parcelco.execute_approved(db, approval["id"], customer, approval["args_hash"])["ok"]
    assert store.get_refund(db, "1005") is None


def test_staff_cannot_approve_own_request(ctx, db, staff):
    store.get_order(db, "1005", ctx["user_id"])
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE orders SET user_id = ? WHERE id = '1005'", (staff,))
    approval = _proposal({**ctx, "user_id": staff}, db)
    assert "own request" in parcelco.execute_approved(db, approval["id"], staff, approval["args_hash"])["message"]


def test_args_changed_after_review_refused(ctx, db, staff):
    approval = _proposal(ctx, db)
    assert "changed" in parcelco.execute_approved(db, approval["id"], staff, "0" * 64)["message"]
    assert store.get_refund(db, "1005") is None


def test_tampered_row_refused(ctx, db, staff):
    import sqlite3

    approval = _proposal(ctx, db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE approvals SET args_json = ? WHERE id = ?", ('{"order_id":"1005","amount_cents":1,"reason":"damaged"}', approval["id"]))
    assert not parcelco.execute_approved(db, approval["id"], staff, approval["args_hash"])["ok"]


def test_expired_approval_refused(ctx, db, staff):
    import sqlite3

    approval = _proposal(ctx, db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE approvals SET expires_at = '2020-01-01T00:00:00.000000+00:00'")
    assert "expired" in parcelco.execute_approved(db, approval["id"], staff, approval["args_hash"])["message"]
    assert store.get_refund(db, "1005") is None


def test_policy_rechecked_at_execution(ctx, db, staff):
    import sqlite3

    approval = _proposal(ctx, db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = '1005'")
    assert "Refused at execution" in parcelco.execute_approved(db, approval["id"], staff, approval["args_hash"])["message"]
    assert store.get_approval(db, approval["id"])["status"] == "rejected"


def test_rejected_approval_never_runs(ctx, db, staff):
    approval = _proposal(ctx, db)
    assert parcelco.reject(db, approval["id"], staff)["ok"]
    assert not parcelco.execute_approved(db, approval["id"], staff, approval["args_hash"])["ok"]


def test_description_comes_from_code(ctx, db):
    text = parcelco.describe_approval(db, _proposal(ctx, db))
    assert "Refund $60.00 for order #1005" in text and "alice" in text
