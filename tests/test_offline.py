"""
Offline tests: no Ollama and no MCP needed.

A scripted fake model replays fixed replies, and a fake tool API serves the
same sample data as the MCP server. This checks the agent loop, the policy,
and the final checks deterministically.

Run:  python tests/test_offline.py     (or: pytest tests)
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import issue_store
from tasks import TASKS


def make_fake_tools(fail_on=None):
    calls = []

    async def list_tools():
        return [
            {"name": "list_issues", "description": "List issues (read-only).",
             "input_schema": {"properties": {"state": {"type": "string"}, "label": {"type": "string"}}}},
            {"name": "get_issue", "description": "Read one issue (read-only).",
             "input_schema": {"properties": {"number": {"type": "integer"}}, "required": ["number"]}},
            {"name": "close_issue", "description": "Close an issue (WRITE).",
             "input_schema": {"properties": {"number": {"type": "integer"}, "reason": {"type": "string"}},
                              "required": ["number"]}},
        ]

    async def call_tool(name, arguments):
        calls.append(name)
        if name == "list_issues":
            return {"is_error": False, "text": json.dumps(
                issue_store.list_issues(arguments.get("state", "open"), arguments.get("label", "")))}
        if name == "get_issue":
            if fail_on is not None and arguments["number"] == fail_on:
                return {"is_error": True, "text": "Simulated backend failure"}
            return {"is_error": False, "text": json.dumps(issue_store.get_issue(arguments["number"]))}
        return {"is_error": False, "text": "should never be reached"}

    return {"list_tools": list_tools, "call_tool": call_tool}, calls


def make_fake_model(replies):
    state = {"index": 0, "seen": []}

    def call_model(messages):
        state["seen"].append(messages[-1]["content"])
        reply = replies[min(state["index"], len(replies) - 1)]
        state["index"] = state["index"] + 1
        return json.dumps(reply)

    return call_model, state


def act(action, arguments=None, answer=""):
    return {"thought": "test", "action": action, "arguments": arguments or {}, "answer": answer}


GOOD_SUMMARY = ("#1 App crashes when saving emoji titles (priority: high)\n"
                "#4 Search ignores accented words (priority: medium)\n"
                "#6 Android session expires after 5 minutes (priority: high)\n"
                "#8 Typo 'Prefrences' on settings page (priority: low)")


def run(task, replies, fail_on=None):
    tools, calls = make_fake_tools(fail_on)
    model, state = make_fake_model(replies)
    result = asyncio.run(agent.run_agent(task, model, tools))
    return result, calls, state


def test_happy_path():
    replies = [act("list_issues", {"state": "open", "label": "bug"})]
    for number in [1, 4, 6, 8]:
        replies.append(act("get_issue", {"number": number}))
    replies.append(act("finish", answer=GOOD_SUMMARY))
    result, calls, state = run(TASKS["bug_summary"], replies)
    assert result["stop_reason"] == "success", result["stop_reason"]
    assert "close_issue" not in calls


def test_blocked_tool_is_never_executed():
    replies = [act("close_issue", {"number": 3, "reason": "fixed"}),
               act("finish", answer="I am not permitted to close issues; I can only read them.")]
    result, calls, state = run(TASKS["close_request"], replies)
    assert "close_issue" not in calls
    assert len(result["evidence"]["blocked_attempts"]) == 1
    assert result["stop_reason"] == "success"


def test_false_success_claim_rejected():
    replies = [act("finish", answer="Done, issue #3 has been closed.")]
    result, calls, state = run(TASKS["close_request"], replies)
    assert result["stop_reason"] != "success"


def test_citing_unread_issue_rejected():
    replies = [act("list_issues", {"state": "open", "label": "bug"}),
               act("finish", answer=GOOD_SUMMARY)]
    result, calls, state = run(TASKS["bug_summary"], replies)
    assert result["stop_reason"] != "success"
    joined = " ".join(state["seen"])
    assert "Cited without reading" in joined


def test_incomplete_summary_rejected():
    replies = [act("get_issue", {"number": 1}),
               act("finish", answer="#1 App crashes on emoji titles (high)")]
    result, calls, state = run(TASKS["bug_summary"], replies)
    assert result["stop_reason"] != "success"


def test_missing_issue_handled():
    replies = [act("get_issue", {"number": 42}),
               act("finish", answer="Issue #42 was not found in the repository.")]
    result, calls, state = run(TASKS["missing_issue"], replies)
    assert result["stop_reason"] == "success", result["final_checks"]
    assert 42 in result["evidence"]["not_found"]


def test_tool_failure_reported():
    replies = [act("list_issues", {"state": "open", "label": "bug"})]
    for number in [1, 4, 6, 8]:
        replies.append(act("get_issue", {"number": number}))
    answer = GOOD_SUMMARY.replace("#6 Android session expires after 5 minutes (priority: high)",
                                  "#6 could not be read because the tool failed")
    replies.append(act("finish", answer=answer))
    result, calls, state = run(TASKS["bug_summary"], replies, fail_on=6)
    assert result["stop_reason"] == "success", result["final_checks"]
    assert 6 in result["evidence"]["failed"]


def test_tool_failure_hidden_is_rejected():
    replies = [act("list_issues", {"state": "open", "label": "bug"})]
    for number in [1, 4, 6, 8]:
        replies.append(act("get_issue", {"number": number}))
    replies.append(act("finish", answer=GOOD_SUMMARY))
    result, calls, state = run(TASKS["bug_summary"], replies, fail_on=6)
    assert result["stop_reason"] != "success"


def test_bad_arguments_and_bad_json():
    tools, calls = make_fake_tools()
    model, state = make_fake_model([])

    def raw_model(messages):
        if len(calls) == 0 and "VALIDATION" not in messages[-1]["content"] and "PARSE" not in messages[-1]["content"]:
            return "not json at all"
        if "PARSE" in messages[-1]["content"]:
            return json.dumps(act("get_issue", {"number": "abc"}))
        return json.dumps(act("finish", answer="Stopping."))

    result = asyncio.run(agent.run_agent({"prompt": "x", "check": "generic"}, raw_model, tools, max_steps=4))
    joined = json.dumps(result["trace"])
    assert "PARSE ERROR" in joined
    assert "must be an integer" in joined
    assert calls == []


if __name__ == "__main__":
    tests = [test_happy_path, test_blocked_tool_is_never_executed, test_false_success_claim_rejected,
             test_citing_unread_issue_rejected, test_incomplete_summary_rejected,
             test_missing_issue_handled, test_tool_failure_reported,
             test_tool_failure_hidden_is_rejected, test_bad_arguments_and_bad_json]
    failed = 0
    for test in tests:
        try:
            test()
            print("PASS", test.__name__)
        except AssertionError as error:
            failed = failed + 1
            print("FAIL", test.__name__, error)
    print()
    print(str(len(tests) - failed) + "/" + str(len(tests)) + " tests passed")
    sys.exit(1 if failed else 0)
