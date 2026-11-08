"""Print a recorded turn, re-run it with the current config and diff the answers.

python -m tools.replay <trace_id>
The re-run happens on a throwaway copy of the database, so no side effect repeats.
"""

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path

import store

ROOT = Path(__file__).parent.parent


def replay(db_path, trace_id, client_factory=None, embedder=None):
    """Returns {"trace", "steps", "new_answer", "diff"}, or None if the trace is unknown."""
    import app as agent_app

    trace = store.get_trace(db_path, trace_id, include_all=True)
    if trace is None:
        return None
    steps = store.trace_steps(db_path, trace_id)
    user = store.get_user_by_id(db_path, trace["user_id"]) or {"role": "customer"}
    with tempfile.TemporaryDirectory() as tmp:
        scratch = str(Path(tmp) / "replay.db")
        store.backup(db_path, scratch)
        turn = agent_app.run_turn(
            scratch,
            trace["agent_id"],
            {"id": trace["user_id"], "role": user["role"]},
            trace["input_text"],
            f"replay-{trace_id}",
            client_factory,
            embedder,
            temperature=0,
            use_cache=False,
        )
    diff = "\n".join(
        difflib.unified_diff(
            (trace["final_answer"] or "").splitlines(),
            turn["answer"].splitlines(),
            "recorded",
            "replayed",
            lineterm="",
        )
    )
    return {"trace": trace, "steps": steps, "new_answer": turn["answer"], "diff": diff}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_id")
    options = parser.parse_args()
    db_path = os.environ.get("AGENT_DB") or str(ROOT / "data" / "agent.db")
    outcome = replay(db_path, options.trace_id)
    if outcome is None:
        print(f"trace not found: {options.trace_id}")
        return 1
    print(
        json.dumps(
            {"trace": outcome["trace"], "steps": outcome["steps"]},
            indent=2,
            default=str,
        )
    )
    print("\nReplayed answer:\n" + outcome["new_answer"])
    print("\nDiff:\n" + (outcome["diff"] or "(identical)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
