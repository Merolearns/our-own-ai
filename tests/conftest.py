import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["AGENT_EMBEDDER"] = "fake"
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("DEMO_CUSTOMER_PASSWORD", None)
os.environ.pop("DEMO_STAFF_PASSWORD", None)

import store  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    store.init_db(path)
    return path


@pytest.fixture
def customer(db):
    user_id = store.create_user(db, "alice", "x")
    store.seed_parcelco(db, user_id)
    return user_id


@pytest.fixture
def staff(db):
    return store.create_user(db, "sam", "x", "staff")


@pytest.fixture
def ctx(db, customer):
    return {"db_path": db, "user_id": customer, "trace_id": "t1", "role": "customer", "agent": "parcelco"}


class FakeClient:
    """Plays back scripted replies; an Exception in the script is raised."""

    def __init__(self, provider, script):
        self.provider = provider
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None, model=None, timeout=None, temperature=None):
        self.calls.append({"messages": messages, "tools": tools, "model": model, "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def reply(text="ok", tool_calls=None, provider="deepseek", model="deepseek-flash", prompt=10, completion=5):
    return {
        "text": text,
        "tool_calls": tool_calls or [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": 0},
        "model": model,
        "provider": provider,
        "finish_reason": "stop",
    }


@pytest.fixture
def fake():
    return FakeClient, reply
