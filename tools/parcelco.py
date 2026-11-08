"""Parcelco tools. Handlers take (args, ctx) and return JSON-able dicts; a
ToolError becomes an isError result the model can read and act on."""

import json
from datetime import datetime, timedelta, timezone

import guardrails
import store
from guardrails import money


class ToolError(Exception):
    pass


def _order_id(raw):
    return raw.strip().lstrip("#")


def _can_see_all(ctx):
    return ctx.get("role") in ("staff", "admin")


def _load_order(ctx, order_id):
    if _can_see_all(ctx):
        return store.get_order_for_staff(ctx["db_path"], order_id)
    return store.get_order(ctx["db_path"], order_id, ctx["user_id"])


def _deny(ctx, verdict, order_id, amount):
    store.log_guardrail_event(
        ctx["db_path"],
        ctx["user_id"],
        "L6",
        "blocked",
        next((c["name"] for c in verdict["checks"] if not c["passed"]), None),
        {"order_id": order_id, "amount": amount, "reason": verdict["reason"]},
    )
    raise ToolError(f"Refund not allowed, nothing was executed. {verdict['reason']}")


def check_order_status(args, ctx):
    db_path = ctx["db_path"]
    order_id = _order_id(args["order_id"])
    order = _load_order(ctx, order_id)
    if order is None:
        raise ToolError(
            f"Order {order_id} not found. Ask the customer to check the order number."
        )

    refund = store.get_refund(db_path, order["id"])
    result = {
        "order_id": order["id"],
        "status": order["status"],
        "total": money(order["total_cents"]),
        "carrier": order["carrier"],
        "latest_event": order["latest_event"],
        "placed_at": order["created_at"][:10],
        "refund": money(refund["amount_cents"]) if refund else None,
        "refund_pending_staff_approval": store.has_open_approval(
            db_path, "issue_refund", order["id"]
        ),
    }
    if order["note"]:
        scan = guardrails.scan_untrusted(order["note"], "order-note")
        if scan["blocked"]:
            store.log_guardrail_event(
                db_path,
                ctx["user_id"],
                "L1",
                "withheld",
                scan["trigger"],
                {"source": "order-note", "order_id": order["id"]},
            )
            result["note"] = "[withheld: failed a safety scan]"
        else:
            result["note"] = guardrails.wrap_untrusted(
                order["note"], "order-note", order["id"]
            )
    return result


def issue_refund(args, ctx):
    db_path = ctx["db_path"]
    user_id = ctx["user_id"]
    order_id = _order_id(args["order_id"])
    amount = args["amount"]
    order = store.get_order(db_path, order_id, user_id)
    verdict = guardrails.policy_check_refund(order, amount, user_id, db_path)
    if not verdict["allowed"]:
        _deny(ctx, verdict, order_id, amount)

    cents = verdict["cents"]
    reason = args["reason"]
    summary = {"order_id": order_id, "amount": money(cents), "reason": reason}

    if verdict["action"] == "proposal":
        expires = datetime.now(timezone.utc) + timedelta(
            hours=guardrails.POLICY["approval_expiry_hours"]
        )
        approval_id = store.create_approval(
            db_path,
            ctx.get("agent", "parcelco"),
            "issue_refund",
            {"order_id": order_id, "amount_cents": cents, "reason": reason},
            order_id,
            user_id,
            ctx.get("request_text", ""),
            verdict["checks"],
            ctx.get("judge_verdict", "skipped"),
            ctx.get("trace_id"),
            expires.isoformat(timespec="microseconds"),
        )
        return {
            **summary,
            "status": "proposal",
            "approval_id": approval_id,
            "message": "Above the automatic limit. Sent to staff for approval; nothing was refunded yet.",
        }

    # The model's word is never enough: only the UI confirm path sets human_confirmed.
    if not (args.get("confirmed") is True and ctx.get("human_confirmed") is True):
        return {
            **summary,
            "status": "confirmation_required",
            "message": "Nothing was refunded. The customer must press Confirm first.",
        }

    outcome = store.record_refund(
        db_path, order_id, user_id, cents, reason, guardrails.POLICY["daily_refund_cap"]
    )
    if outcome != "ok":
        message = (
            "This order has already been refunded."
            if outcome == "duplicate"
            else "The daily refund limit has been reached."
        )
        store.log_guardrail_event(db_path, user_id, "L6", "blocked", outcome, summary)
        raise ToolError(f"Refund not allowed, nothing was executed. {message}")
    return {**summary, "status": "refunded", "message": f"Refunded {money(cents)}."}


def open_ticket(args, ctx):
    db_path = ctx["db_path"]
    order_id = args.get("order_id")
    if order_id is not None:
        order_id = _order_id(order_id)
        if _load_order(ctx, order_id) is None:
            raise ToolError(
                f"Order {order_id} not found. Ask the customer to check the order number."
            )
    priority = args.get("priority", "normal")
    ticket_id = store.create_ticket(
        db_path,
        ctx["user_id"],
        order_id,
        args["subject"],
        args["description"],
        priority,
    )
    return {
        "status": "opened",
        "ticket_id": ticket_id,
        "order_id": order_id,
        "priority": priority,
    }


def describe_approval(db_path, approval):
    """The sentence a human relies on to approve money. Built here, never by the model."""
    args = json.loads(approval["args_json"])
    requester = store.get_user_by_id(db_path, approval["requester"])
    name = requester["username"] if requester else "unknown customer"
    order = store.get_order_for_staff(db_path, args["order_id"])
    if order is None:
        return f"Refund {money(args['amount_cents'])} for order #{args['order_id']}, which no longer exists."
    return (
        f"Refund {money(args['amount_cents'])} for order #{order['id']} "
        f"({order['status']}, total {money(order['total_cents'])}) to customer {name}. "
        f"Reason given: {args['reason']}. This sends money out and cannot be undone."
    )


def execute_approved(db_path, approval_id, approver_id, expected_hash):
    """Run a staff-approved refund exactly once. Returns {"ok": bool, "message": str}."""
    store.expire_approvals(db_path)
    approval = store.get_approval(db_path, approval_id)
    if approval is None:
        return {"ok": False, "message": "Approval not found."}

    def refuse(trigger, message):
        store.log_guardrail_event(
            db_path, approver_id, "L6", "blocked", trigger, {"approval_id": approval_id}
        )
        return {"ok": False, "message": message}

    approver = store.get_user_by_id(db_path, approver_id)
    if approver is None or approver["role"] not in ("staff", "admin"):
        return refuse("approver-not-staff", "Only staff can approve refunds.")
    if approval["status"] == "expired":
        return refuse("expired", "This approval expired, so the refund will not run.")
    if approval["status"] != "pending":
        return refuse("not-pending", f"This approval is already {approval['status']}.")
    if approval["requester"] == approver_id:
        return refuse("self-approval", "You cannot approve your own request.")
    if (
        approval["args_hash"] != expected_hash
        or store.args_hash(json.loads(approval["args_json"])) != approval["args_hash"]
    ):
        return refuse(
            "args-mismatch",
            "The request changed after you opened it. Nothing was refunded.",
        )
    if not store.claim_approval(db_path, approval_id, approver_id):
        return refuse("claim-failed", "This approval is no longer open.")

    args = json.loads(approval["args_json"])
    requester = approval["requester"]
    order = store.get_order(db_path, args["order_id"], requester)
    verdict = guardrails.policy_check_refund(
        order, args["amount_cents"] / 100, requester, db_path
    )
    if not verdict["allowed"]:
        store.reject_approval(
            db_path, approval_id, approver_id, f"Failed re-check: {verdict['reason']}"
        )
        return refuse(
            "revalidation-failed", f"Refused at execution: {verdict['reason']}"
        )

    outcome = store.record_refund(
        db_path,
        args["order_id"],
        requester,
        args["amount_cents"],
        args["reason"],
        guardrails.POLICY["daily_refund_cap"],
        approval_id,
    )
    if outcome != "ok":
        store.reject_approval(
            db_path, approval_id, approver_id, f"Failed at write: {outcome}"
        )
        return refuse(outcome, "Refused at execution: a refund rule no longer holds.")
    store.mark_approval_executed(db_path, approval_id)
    store.log_guardrail_event(
        db_path,
        approver_id,
        "L6",
        "allowed",
        "approval-executed",
        {"approval_id": approval_id},
    )
    return {
        "ok": True,
        "message": f"Refunded {money(args['amount_cents'])} for order #{args['order_id']}.",
    }


def reject(db_path, approval_id, approver_id, note="Rejected by staff"):
    approver = store.get_user_by_id(db_path, approver_id)
    if approver is None or approver["role"] not in ("staff", "admin"):
        return {"ok": False, "message": "Only staff can reject refunds."}
    if not store.reject_approval(db_path, approval_id, approver_id, note):
        return {"ok": False, "message": "This approval is no longer open."}
    return {"ok": True, "message": "Rejected. Nothing was refunded."}


TOOLS = {
    "check_order_status": {
        "description": (
            "Look up a Parcelco order by its number. Returns status, carrier, latest "
            "tracking event and any refund. Use when the customer asks about an order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order number, e.g. '1001'.",
                }
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "handler": check_order_status,
        "side_effect": False,
        "risky": False,
    },
    "issue_refund": {
        "description": (
            "Request a refund for one order. Check the order first and refund at most its "
            "total. Small refunds ask the customer to press Confirm; large ones go to staff. "
            "The result's status says which happened."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The order number."},
                "amount": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Refund amount in US dollars.",
                },
                "reason": {
                    "type": "string",
                    "enum": ["damaged", "lost", "late", "other"],
                    "description": "Why the customer wants a refund.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Leave false. Only the customer's Confirm button can confirm.",
                },
                # Accepted for retries, but the server keys refunds on order id alone.
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional retry key.",
                },
            },
            "required": ["order_id", "amount", "reason"],
            "additionalProperties": False,
        },
        "handler": issue_refund,
        "side_effect": True,
        "risky": True,
    },
    "open_ticket": {
        "description": "Open a support ticket for a problem you cannot resolve in this chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Related order number, if any.",
                },
                "subject": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Short summary.",
                },
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "description": "What happened.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "Defaults to normal.",
                },
            },
            "required": ["subject", "description"],
            "additionalProperties": False,
        },
        "handler": open_ticket,
        "side_effect": True,
        "risky": False,
    },
}
