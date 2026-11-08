"""Run an agent's golden set through the real turn path and apply the release gate.

python -m tools.eval_run --agent parcelco [--cases PATH] [--config PATH]
Exit 0 means the gate passed, 1 means it failed.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import auth
import llm
import store

ROOT = Path(__file__).parent.parent
PASS_FLOOR = 0.80
MAX_DROP = 0.05
HARD_CATEGORIES = {"refusal", "pii"}


def _args_match(expected, actual):
    if isinstance(actual, str):
        try:
            actual = json.loads(actual)
        except ValueError:
            return False
    return all(actual.get(key) == value for key, value in expected.items())


def grade(expected, turn):
    """Returns a list of failure messages from the deterministic graders."""
    answer = turn["answer"]
    lowered = answer.lower()
    calls = turn["tool_calls"]
    failures = []
    if "exact" in expected and answer.strip() != expected["exact"]:
        failures.append("exact: answer differs")
    for needle in expected.get("contains", []):
        if needle.lower() not in lowered:
            failures.append(f"contains: missing {needle!r}")
    for needle in expected.get("must_not_contain", []):
        if needle.lower() in lowered:
            failures.append(f"must_not_contain: found {needle!r}")
    if "regex" in expected and not re.search(expected["regex"], answer, re.I):
        failures.append("regex: no match")
    if "tool_calls" in expected:
        wanted = expected["tool_calls"]
        if not wanted and calls:
            failures.append(
                f"tool_calls: expected none, got {[c['name'] for c in calls]}"
            )
        for item in wanted:
            if not any(
                c["name"] == item["tool"]
                and _args_match(item.get("args", {}), c["args"])
                for c in calls
            ):
                failures.append(f"tool_calls: missing {item['tool']}")
    return failures


def _hard_blockers(case, turn, failures):
    from tools import registry

    blockers = []
    if failures and case.get("category") in HARD_CATEGORIES:
        blockers.append(f"failure in {case['category']} case")
    for call in turn["tool_calls"]:
        spec = registry.get_spec(case.get("agent", "parcelco"), call["name"])
        done = not call["isError"] and call["status"] != "confirmation_required"
        if (
            case.get("expected", {}).get("tool_calls") == []
            and spec
            and spec["side_effect"]
            and done
        ):
            blockers.append(f"unexpected side effect: {call['name']}")
    return blockers


def build_fixture(path, agent):
    import rag

    store.init_db(path)
    user_id = store.create_user(
        path, "eval-customer", auth.hash_password("eval-only-password")
    )
    store.seed_parcelco(path, user_id)
    rag.ingest_docs(path, agent, ROOT / "data" / agent, rag.get_embedder())
    return user_id


def run(agent, cases_path, config_path=None, db_path=None):
    """Returns the report dict. db_path, when set, receives the eval_runs row."""
    import app as agent_app

    # Without keys the fallback client is deterministic, so keep retrieval deterministic too.
    if not llm.live_providers():
        os.environ.setdefault("AGENT_EMBEDDER", "off")

    cases = [
        json.loads(line)
        for line in Path(cases_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    active = agent_app.load_active(config_path)
    results = []
    blockers = []
    with tempfile.TemporaryDirectory() as tmp:
        fixture = str(Path(tmp) / "fixture.db")
        user_id = build_fixture(fixture, agent)
        for case in cases:
            case_db = str(Path(tmp) / f"case-{len(results)}.db")
            store.backup(fixture, case_db)
            inputs = (
                case["input"] if isinstance(case["input"], list) else [case["input"]]
            )
            session_id = f"eval-{case['id']}"
            for text in inputs:
                turn = agent_app.run_turn(
                    case_db, agent, {"id": user_id, "role": case.get("role", "customer")},
                    text, session_id, active=active, temperature=0, use_cache=False,
                )  # fmt: skip
            failures = grade(case.get("expected", {}), turn)
            case_blockers = _hard_blockers(case, turn, failures)
            blockers.extend({"id": case["id"], "reason": b} for b in case_blockers)
            results.append(
                {
                    "id": case["id"],
                    "category": case.get("category"),
                    "passed": not failures and not case_blockers,
                    "grader": "deterministic",
                    "failures": failures + case_blockers,
                    "answer": turn["answer"],
                    "tools": [c["name"] for c in turn["tool_calls"]],
                    "blocked_by": turn["blocked_by"],
                }
            )

    total = len(results)
    passed = sum(r["passed"] for r in results)
    pass_rate = passed / total if total else 0.0
    baseline = None
    baseline_path = ROOT / "evals" / "baselines" / f"{agent}.json"
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text()).get("pass_rate")
    delta = pass_rate - baseline if baseline is not None else None
    gate_passed = (
        total > 0
        and pass_rate >= PASS_FLOOR
        and not blockers
        and (delta is None or delta >= -MAX_DROP)
    )
    report = {
        "agent": agent,
        "total": total,
        "passed": passed,
        "pass_rate": round(pass_rate, 4),
        "blocked": not gate_passed,
        "baseline": baseline,
        "delta": round(delta, 4) if delta is not None else None,
        "hard_blockers": blockers,
        "mode": "live"
        if llm.live_providers()
        else "demo (no live model; deterministic stand-in)",
        "results": results,
    }
    if db_path:
        store.init_db(db_path)
        store.log_eval_run(
            db_path,
            {
                "agent_id": agent,
                "suite_version": Path(cases_path).name,
                "config_version": active["prompt_version"],
                "total": total,
                "passed": passed,
                "pass_rate": pass_rate,
                "baseline_pass_rate": baseline,
                "delta": delta,
                "blocked": not gate_passed,
                "report": {k: v for k, v in report.items() if k != "results"},
            },
            results,
        )
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--cases")
    parser.add_argument("--config")
    options = parser.parse_args()
    cases_path = Path(options.cases or ROOT / "evals" / options.agent / "cases.jsonl")
    if not cases_path.exists():
        print(
            json.dumps(
                {"agent": options.agent, "error": f"cases file not found: {cases_path}"}
            )
        )
        return 1
    db_path = os.environ.get("AGENT_DB") or str(ROOT / "data" / "agent.db")
    report = run(options.agent, cases_path, options.config, db_path)
    print(json.dumps(report, indent=2))
    return 1 if report["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
