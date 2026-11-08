"""Scoopy Doog agent: tools, allow-list, host routing, demo turns, eval subset.

Parcelco behaviour is asserted unchanged here too: the same demo turns
that passed before this build must still pass, and the Parcelco eval gate
is re-run separately (tools/eval_run.py --agent parcelco).
"""

import json

import pytest

import app as agent_app
import rag
import store
from tools import eval_run, registry

SCOOPY_HOST = "scoopy.40.160.149.104.nip.io"
AGENTS_HOST = "agents.40.160.149.104.nip.io"


@pytest.fixture
def scoopy_db(db):
    rag.ingest_docs(db, "scoopy", agent_app.ROOT / "data" / "scoopy", rag.FakeEmbedder())
    return db


def scoopy_ctx(db, customer):
    return {
        "db_path": db,
        "user_id": customer,
        "trace_id": "t1",
        "role": "customer",
        "agent": "scoopy",
    }


def turn(db, customer, text, session="s1", **kw):
    kw.setdefault("sleep", lambda _: None)
    active = agent_app.load_active(agent_app.ROOT / "config" / "scoopy.json")
    return agent_app.run_turn(
        db, "scoopy", {"id": customer, "role": "customer"}, text, session,
        active=active, router_state={}, **kw,
    )


# Registry and allow-list


def test_scoopy_tools_are_offered_to_every_role():
    for role in ("customer", "staff", "admin"):
        names = {tool["name"] for tool in registry.list_tools("scoopy", role)}
        assert names == {"open_ticket", "check_service_area"}
    assert registry.list_tools("scoopy", "nobody") == []


def test_agents_do_not_see_each_others_tools():
    assert registry.get_spec("scoopy", "issue_refund") is None
    assert registry.get_spec("scoopy", "check_order_status") is None
    assert registry.get_spec("parcelco", "check_service_area") is None
    parcelco_names = {t["name"] for t in registry.list_tools("parcelco", "customer")}
    assert parcelco_names == {"check_order_status", "issue_refund", "open_ticket"}


def test_open_ticket_creates_a_ticket_without_an_order(db, customer):
    ctx = scoopy_ctx(db, customer)
    out = registry.call_tool(
        "open_ticket",
        {"subject": "Call back", "description": "Please call me about a quote."},
        ctx,
    )
    assert out["isError"] is False
    data = json.loads(out["content"])
    assert data["status"] == "opened" and data["ticket_id"] == 1
    tickets = store.list_tickets(db, customer)
    assert len(tickets) == 1 and tickets[0]["order_id"] is None


def test_open_ticket_rejects_an_empty_subject(db, customer):
    ctx = scoopy_ctx(db, customer)
    out = registry.call_tool(
        "open_ticket", {"subject": "", "description": "x"}, ctx
    )
    assert out["isError"] is True
    assert store.list_tickets(db, customer) == []


def test_check_service_area_never_confirms_a_city(db, customer):
    ctx = scoopy_ctx(db, customer)
    out = registry.call_tool(
        "check_service_area", {"city": "Irvine", "zip_code": "92626"}, ctx
    )
    data = json.loads(out["content"])
    assert data["service_area"] == "Orange County, California"
    assert data["city"] == "Irvine" and data["city_confirmed"] is False
    assert "not published" in data["note"] and "(714) 933-8654" in data["note"]


def test_scoopy_cannot_call_parcelco_tools(db, customer):
    ctx = scoopy_ctx(db, customer)
    out = registry.call_tool(
        "issue_refund", {"order_id": "1001", "amount": 5, "reason": "other"}, ctx
    )
    assert out["isError"] is True and "Unknown tool" in out["content"]


# Host routing


@pytest.mark.parametrize(
    "host,expected",
    [
        (SCOOPY_HOST, "scoopy"),
        ("SCOOPY.40.160.149.104.NIP.IO", "scoopy"),
        (SCOOPY_HOST + ":443", "scoopy"),
        (AGENTS_HOST, "parcelco"),
        ("localhost", "parcelco"),
        ("127.0.0.1:5000", "parcelco"),
        ("example.com", "parcelco"),
        (None, "parcelco"),
    ],
)
def test_resolve_agent(host, expected):
    assert agent_app.resolve_agent(host) == expected


# Demo turns (no live model)


def test_demo_pricing_turn_is_grounded(scoopy_db, customer):
    out = turn(scoopy_db, customer, "What do Scoopy Doog plans cost per month?")
    assert out["demo"] is True and out["sources"]
    assert "$60" in out["answer"] and "$100" in out["answer"]


def test_demo_refusal_turn_deflects_to_free_quote(scoopy_db, customer):
    out = turn(scoopy_db, customer, "What is the exact price for a weekly visit?")
    assert "not published" in out["answer"] and "Free Quote" in out["answer"]
    assert "$60" not in out["answer"] and "$100" not in out["answer"]


def test_demo_callback_turn_opens_a_ticket(scoopy_db, customer):
    out = turn(scoopy_db, customer, "Please call me back about a quote for my yard.")
    assert [c["name"] for c in out["tool_calls"]] == ["open_ticket"]
    assert "ticket" in out["answer"]
    assert len(store.list_tickets(scoopy_db, customer)) == 1


def test_demo_greeting_names_scoopy_not_nova(scoopy_db, customer):
    out = turn(scoopy_db, customer, "Hi!")
    assert "Scoopy Doog" in out["answer"] and "Nova" not in out["answer"]


def test_demo_label_case_never_repeats_the_label(scoopy_db, customer):
    out = turn(scoopy_db, customer, "Are you a residential sanitation service?")
    assert "dog waste removal" in out["answer"]
    assert "residential sanitation" not in out["answer"]


# HTTP: the same app serves both hosts


@pytest.fixture
def web(tmp_path):
    application = agent_app.create_app(db_path=str(tmp_path / "web.db"))
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


def signup(client, name="alice", password="password123"):
    return client.post("/register", data={"username": name, "password": password})


@pytest.fixture
def alice(web):
    client = web.test_client()
    signup(client)
    return client


def test_healthz_needs_both_agents(web):
    assert web.test_client().get("/healthz").status_code == 200


def test_scoopy_login_page_is_branded_scoopy(web, alice):
    client = web.test_client()
    assert client.post(
        "/login", data={"username": "alice", "password": "password123"}
    ).status_code == 302
    page = client.get("/login", base_url=f"https://{SCOOPY_HOST}").text
    assert "Log in to Scoopy Doog" in page and "Nova" not in page
    page = client.get("/login", base_url=f"https://{AGENTS_HOST}").text
    assert "Log in to Nova" in page


def test_scoopy_chat_answers_pricing_over_its_host(web):
    # The test client's cookie jar is per-host, so the signup happens on
    # the same host as the chat call — otherwise the session cookie is not
    # sent and the API answers 401.
    client = web.test_client()
    assert client.post(
        "/register",
        data={"username": "alice", "password": "password123"},
        base_url=f"https://{SCOOPY_HOST}",
    ).status_code == 302
    data = client.post(
        "/api/chat",
        json={"message": "What do Scoopy Doog plans cost per month?"},
        base_url=f"https://{SCOOPY_HOST}",
    ).get_json()
    assert "$60" in data["answer"] and data["sources"]


def test_parcelco_host_answers_parcelco_over_its_host(web):
    client = web.test_client()
    assert client.post(
        "/register",
        data={"username": "alice", "password": "password123"},
        base_url=f"https://{AGENTS_HOST}",
    ).status_code == 302
    data = client.post(
        "/api/chat",
        json={"message": "What is your refund policy?"},
        base_url=f"https://{AGENTS_HOST}",
    ).get_json()
    assert "$50" in data["answer"]


# Eval subset through the real runner


def test_eval_subset_passes(tmp_path):
    cases = [
        {
            "id": "s1",
            "agent": "scoopy",
            "category": "faq",
            "input": "What do Scoopy Doog plans cost per month?",
            "expected": {"contains": ["$60"], "tool_calls": []},
        },
        {
            "id": "s2",
            "agent": "scoopy",
            "category": "refusal",
            "input": "What is the exact price for a weekly visit?",
            "expected": {
                "contains": ["not published", "Free Quote"],
                "must_not_contain": ["$60"],
                "tool_calls": [],
            },
        },
        {
            "id": "s3",
            "agent": "scoopy",
            "category": "tool",
            "input": "Please call me back about a quote for my yard.",
            "expected": {
                "contains": ["ticket"],
                "tool_calls": [{"tool": "open_ticket"}],
            },
        },
    ]
    path = tmp_path / "c.jsonl"
    path.write_text("\n".join(json.dumps(case) for case in cases))
    report = eval_run.run(
        "scoopy", path, config_path=str(agent_app.ROOT / "config" / "scoopy.json")
    )
    assert report["total"] == 3 and report["passed"] == 3
    assert report["blocked"] is False
