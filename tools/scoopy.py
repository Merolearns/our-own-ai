"""Scoopy Doog tools. Handlers take (args, ctx) and return JSON-able dicts; a
ToolError becomes an isError result the model can read and act on.

Scoopy Doog has NO customer API and NO order system, so there is deliberately
no order lookup, refund, scheduling, or payment tool here. Booking stays on
the website's Free Quote flow and the customer portal; the agent links and
explains, never books. The two tools below are the whole set. Handlers that
need to fail raise tools.parcelco.ToolError, the class the registry catches:

- open_ticket: a callback / quote-follow-up request. Staff follow up by
  phone. This is the main tool — it is how a chat conversation turns into
  a real-world action.
- check_service_area: confirms the general service area (Orange County, CA)
  from the knowledge base. The exact list of cities covered is NOT
  published, so the tool never confirms a specific city — it deflects to
  the office for that.
"""

import store

OFFICE_PHONE = "(714) 933-8654"
OFFICE_EMAIL = "service@scoopydoog.com"
FREE_QUOTE_URL = "https://www.scoopydoog.com/"
SERVICE_AREA = "Orange County, California"


def open_ticket(args, ctx):
    """A callback / quote-follow-up request. Staff follow up by phone.

    No order exists for Scoopy Doog, so order_id is never part of this tool.
    Name, phone, and zip are optional in the schema — the model asks for
    them when the customer has not given them — but a ticket with only a
    subject and description is still filed: a partial request beats a
    dropped one, and staff can ask for the missing details on the call.
    """
    ticket_id = store.create_ticket(
        ctx["db_path"],
        ctx["user_id"],
        None,
        args["subject"],
        args["description"],
        args.get("priority", "normal"),
    )
    return {
        "status": "opened",
        "ticket_id": ticket_id,
        "priority": args.get("priority", "normal"),
        "follow_up": "by phone",
        "message": (
            "Request received. Scoopy Doog staff will call you back. "
            "Office hours are Monday-Saturday, 7am-6pm; "
            f"the office number is {OFFICE_PHONE}."
        ),
    }


def check_service_area(args, ctx):
    """Confirm the general service area. Never confirms a specific city.

    The knowledge base publishes only "Orange County, CA" — the exact city
    list is unverified, so any city or zip the customer names gets the same
    answer: the general area plus a deflection to the office. Guessing
    coverage for a real address would send a technician to the wrong place
    or turn a real customer away.
    """
    city = (args.get("city") or "").strip() or None
    zip_code = (args.get("zip_code") or "").strip() or None
    return {
        "service_area": SERVICE_AREA,
        "city": city,
        "zip_code": zip_code,
        "city_confirmed": False,
        "note": (
            "The exact list of cities covered is not published, so I can't "
            "confirm a specific city or address here. For that, call the "
            f"office at {OFFICE_PHONE} or email {OFFICE_EMAIL}."
        ),
        "free_quote": (
            f"For an exact quote, use the Free Quote flow at {FREE_QUOTE_URL} "
            "(enter your zip code and complete signup)."
        ),
    }


TOOLS = {
    "open_ticket": {
        "description": (
            "Open a callback or quote-follow-up request for a Scoopy Doog "
            "customer. Use when the customer asks for a call back, wants a "
            "quote followed up, or has a problem the chat cannot resolve. "
            "Staff follow up by phone; nothing is booked or confirmed here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Customer's name, if they gave it.",
                },
                "phone": {
                    "type": "string",
                    "description": "Callback phone number, if they gave it.",
                },
                "zip_code": {
                    "type": "string",
                    "description": "Customer's zip code, if they gave it.",
                },
                "subject": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Short summary of what they need.",
                },
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "description": "What they asked for, in their words.",
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
    "check_service_area": {
        "description": (
            "Check whether Scoopy Doog serves an area. Returns the general "
            "service area (Orange County, California) and deflects to the "
            "office for any specific city or address — the exact city list "
            "is not published, so never treat this tool as confirming a city."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City the customer asked about, if any.",
                },
                "zip_code": {
                    "type": "string",
                    "description": "Zip code the customer gave, if any.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "handler": check_service_area,
        "side_effect": False,
        "risky": False,
    },
}
