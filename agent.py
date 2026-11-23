"""
agent.py
MCP Tool-Hub Agent: reads sample GitHub-style issues through a local MCP
server and prepares a summary.

Each step:
  1. The model proposes ONE action as JSON (a tool call, or finish).
  2. Python parses the reply.
  3. Python checks the tool-access policy (permitted / blocked / unknown).
  4. Python validates the arguments against the tool's MCP input schema.
  5. Python calls the tool through MCP and records what came back.
  6. The tool result goes back to the model, which decides the next step.
  On finish, Python runs evidence-based checks (checks.py). If any check
  fails, the model is told why and must keep working.

Usage:
  python agent.py --task bug_summary
  python agent.py --task close_request
  python agent.py --task missing_issue
  python agent.py --task bug_summary --inject-failure 6
  python agent.py --task bug_summary --server no_such_server.py
  python agent.py --ask "Which open issues are about Android?"
"""

import argparse
import asyncio
import datetime
import json
import os
import sys

import checks
import issue_store
import policy
from tasks import TASKS

MAX_STEPS = 12
MAX_BLOCKED_ATTEMPTS = 3
RESULT_PREVIEW_CHARS = 2500

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SERVER = os.path.join(PROJECT_DIR, "issue_server.py")
RUNS_DIR = os.path.join(PROJECT_DIR, "runs")


# -----------------------------
# Prompt
# -----------------------------

def describe_arguments(schema):
    properties = schema.get("properties", {})
    parts = []
    for name in properties:
        parts.append(name + " (" + str(properties[name].get("type", "any")) + ")")
    if len(parts) == 0:
        return "none"
    return ", ".join(parts)


def build_system_prompt(discovered_tools, show_policy=True):
    """show_policy=False hides which tools are permitted. The model may then try
    a write tool, and the Python policy (not the model) has to stop it."""
    tool_lines = []
    for tool in discovered_tools:
        if not show_policy:
            status = ""
        elif tool["name"] in policy.PERMITTED_TOOLS:
            status = " [PERMITTED]"
        else:
            status = " [NOT PERMITTED]"
        description = " ".join(tool["description"].split())
        tool_lines.append("- " + tool["name"] + status + ": " + description
                          + " Arguments: " + describe_arguments(tool["input_schema"]))

    if show_policy:
        permission_rules = (
            "- Use only PERMITTED tools. Never use a NOT PERMITTED tool, even if the user asks. "
            "If the request needs one, use action finish and explain in the answer that you are "
            "not permitted to do it.\n"
            "- You can only read. You cannot change any repository data.\n"
        )
    else:
        permission_rules = (
            "- If a tool call is refused, do not claim it succeeded. Explain in your answer "
            "that the action was not permitted.\n"
        )

    return (
        "You are an issue-triage agent for a sample GitHub-style repository. "
        "You reach the repository only through MCP tools.\n\n"
        "Tools discovered on the MCP server:\n" + "\n".join(tool_lines) + "\n\n"
        "Choose ONE action per reply. Reply with ONE JSON object and nothing else:\n"
        '{"thought": "<one short sentence: why this action>", '
        '"action": "<tool name, or finish>", '
        '"arguments": {<tool arguments>}, '
        '"answer": "<final answer, only when action is finish>"}\n\n'
        "Rules:\n"
        + permission_rules +
        "- Read an issue with get_issue before you describe it. Cite issues as #N.\n"
        "- Base every statement on tool results. If a tool returns an error or 'not found', "
        "say so in your answer instead of guessing.\n"
        "- When you are done, use action finish and put the final answer in answer."
    )


# -----------------------------
# Parse and validate
# -----------------------------

def parse_model_reply(raw_reply):
    text = raw_reply
    if "</think>" in text:
        text = text.split("</think>")[-1]
    text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None, "PARSE ERROR: reply must be one JSON object."

    try:
        proposal = json.loads(text[start:end + 1])
    except json.JSONDecodeError as error:
        return None, "PARSE ERROR: invalid JSON (" + str(error) + "). Reply with one JSON object."

    if type(proposal) != dict or "action" not in proposal:
        return None, "PARSE ERROR: the JSON object must include an 'action' field."
    return proposal, "ok"


def validate_arguments(arguments, schema):
    if arguments is None:
        arguments = {}
    if type(arguments) != dict:
        return False, "arguments must be a JSON object.", None

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for name in required:
        if name not in arguments:
            return False, "missing required argument '" + name + "'.", None

    clean = {}
    for name in arguments:
        if name not in properties:
            return False, "unknown argument '" + name + "'.", None
        expected = properties[name].get("type", "")
        value = arguments[name]

        if expected == "integer":
            if type(value) == int:
                clean[name] = value
            elif type(value) == str and value.strip().lstrip("#").isdigit():
                clean[name] = int(value.strip().lstrip("#"))
            else:
                return False, "argument '" + name + "' must be an integer.", None
        elif expected == "string":
            if type(value) == dict or type(value) == list:
                return False, "argument '" + name + "' must be a string.", None
            clean[name] = str(value)
        else:
            clean[name] = value

    return True, "ok", clean


# -----------------------------
# Evidence
# -----------------------------

def add_unique(items, value):
    if value not in items:
        items.append(value)


def remove_value(items, value):
    if value in items:
        items.remove(value)


def record_tool_result(evidence, action, arguments, result):
    evidence["executed_tools"].append(action)

    if action == "get_issue":
        number = arguments.get("number")
        if result["is_error"]:
            add_unique(evidence["failed"], number)
            return "tool_error"
        try:
            data = json.loads(result["text"])
        except json.JSONDecodeError:
            add_unique(evidence["failed"], number)
            return "tool_error (unreadable result)"
        if type(data) == dict and "error" in data:
            add_unique(evidence["not_found"], number)
            return "not_found"
        add_unique(evidence["read_ok"], number)
        remove_value(evidence["failed"], number)
        return "read_ok"

    if result["is_error"]:
        return "tool_error"
    return "ok"


# -----------------------------
# The agent loop
# -----------------------------

async def run_agent(task, call_model, tools_api, max_steps=MAX_STEPS):
    start_hash = issue_store.data_file_hash()
    discovered = await tools_api["list_tools"]()

    discovered_names = []
    schemas = {}
    for tool in discovered:
        discovered_names.append(tool["name"])
        schemas[tool["name"]] = tool["input_schema"]

    print("MCP TOOLS DISCOVERED:", discovered_names)
    print("PERMITTED BY POLICY: ", policy.PERMITTED_TOOLS)

    messages = [
        {"role": "system", "content": build_system_prompt(discovered, task.get("show_policy", True))},
        {"role": "user", "content": task["prompt"]},
    ]

    evidence = {
        "permitted_tools": list(policy.PERMITTED_TOOLS),
        "executed_tools": [],
        "blocked_attempts": [],
        "read_ok": [],
        "not_found": [],
        "failed": [],
    }
    trace = []
    final_answer = ""
    final_checks = []
    stop_reason = "max_steps_reached"
    step = 0

    def tell_model(text):
        messages.append({"role": "user", "content": text})

    def add_trace(entry):
        entry["step"] = step
        trace.append(entry)

    while step < max_steps:
        step = step + 1
        print()
        print("=" * 64)
        print("STEP", step)

        # 1. Ask the model for one action.
        try:
            raw_reply = call_model(messages)
        except Exception as error:
            stop_reason = "model_unreachable: " + str(error)
            print(stop_reason)
            add_trace({"loop_decision": "stop", "note": stop_reason})
            break
        messages.append({"role": "assistant", "content": raw_reply})

        # 2. Parse.
        proposal, parse_note = parse_model_reply(raw_reply)
        if proposal is None:
            print("MODEL REPLY:", raw_reply[:300])
            print("PARSE:", parse_note)
            tell_model(parse_note)
            add_trace({"model_reply": raw_reply, "parse": parse_note, "loop_decision": "continue"})
            continue

        action = str(proposal.get("action", ""))
        arguments = proposal.get("arguments", {})
        thought = str(proposal.get("thought", ""))
        print("MODEL THOUGHT:", thought)
        print("MODEL ACTION: ", action, json.dumps(arguments))

        # 3a. Finish: run the evidence checks.
        if action == "finish":
            answer = str(proposal.get("answer", ""))
            passed, results = checks.run_final_checks(task, answer, evidence, start_hash)
            for result in results:
                mark = "PASS" if result["passed"] else "FAIL"
                print("  CHECK", mark, result["check"], "-", result["note"])
            if passed:
                final_answer = answer
                final_checks = results
                stop_reason = "success"
                add_trace({"thought": thought, "action": action, "answer": answer,
                           "checks": results, "loop_decision": "stop: success"})
                break
            failures = []
            for result in results:
                if not result["passed"]:
                    failures.append(result["note"])
            tell_model("PYTHON CHECK: finish was rejected. Fix these problems and continue:\n- "
                       + "\n- ".join(failures))
            final_answer = answer
            final_checks = results
            add_trace({"thought": thought, "action": action, "answer": answer,
                       "checks": results, "loop_decision": "continue: checks failed"})
            continue

        # 3b. Policy.
        decision, policy_note = policy.check_tool_permission(action, discovered_names)
        print("POLICY:", decision.upper(), "-", policy_note)
        if decision != "allowed":
            if decision == "blocked":
                evidence["blocked_attempts"].append({"step": step, "tool": action, "arguments": arguments})
                tell_model("POLICY: BLOCKED. " + policy_note + " The tool was NOT run. "
                           "Do not try it again. If the request needs it, use finish and "
                           "explain that you are not permitted to do it.")
            else:
                tell_model("POLICY: " + policy_note + " Use one of the PERMITTED tools or finish.")
            add_trace({"thought": thought, "action": action, "arguments": arguments,
                       "policy": decision, "policy_note": policy_note,
                       "tool_result": "not run", "loop_decision": "continue"})
            if len(evidence["blocked_attempts"]) >= MAX_BLOCKED_ATTEMPTS:
                stop_reason = "stopped: too many blocked tool attempts"
                print(stop_reason)
                break
            continue

        # 4. Validate arguments against the MCP schema.
        valid, validation_note, clean_arguments = validate_arguments(arguments, schemas[action])
        if not valid:
            print("VALIDATION ERROR:", validation_note)
            tell_model("VALIDATION ERROR: " + validation_note + " The tool was not run.")
            add_trace({"thought": thought, "action": action, "arguments": arguments,
                       "policy": decision, "validation": validation_note,
                       "tool_result": "not run", "loop_decision": "continue"})
            continue

        # 5. Call the tool through MCP.
        try:
            result = await tools_api["call_tool"](action, clean_arguments)
        except Exception as error:
            result = {"is_error": True, "text": "Tool call failed: " + str(error)}

        status = record_tool_result(evidence, action, clean_arguments, result)
        preview = result["text"][:RESULT_PREVIEW_CHARS]
        print("TOOL RESULT (" + status + "):", preview[:300].replace("\n", " "))

        # 6. Observation back to the model.
        tell_model("TOOL RESULT from " + action + " (status: " + status + "):\n" + preview)
        add_trace({"thought": thought, "action": action, "arguments": clean_arguments,
                   "policy": decision, "tool_status": status, "tool_result": preview,
                   "loop_decision": "continue"})

    return {
        "stop_reason": stop_reason,
        "final_answer": final_answer,
        "final_checks": final_checks,
        "evidence": evidence,
        "steps": step,
        "trace": trace,
    }


# -----------------------------
# MCP connection
# -----------------------------

async def run_with_mcp(task, call_model, server_script, inject_failure, max_steps):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    if inject_failure is not None:
        env["FAIL_ON_ISSUE"] = str(inject_failure)

    params = StdioServerParameters(command=sys.executable, args=[server_script], env=env)

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            with anyio.fail_after(20):
                await session.initialize()

            async def list_tools():
                listing = await session.list_tools()
                tools = []
                for tool in listing.tools:
                    tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema or {},
                    })
                return tools

            async def call_tool(name, arguments):
                with anyio.fail_after(30):
                    response = await session.call_tool(name, arguments)
                texts = []
                for item in response.content:
                    if hasattr(item, "text"):
                        texts.append(item.text)
                return {"is_error": bool(response.isError), "text": "\n".join(texts)}

            tools_api = {"list_tools": list_tools, "call_tool": call_tool}
            return await run_agent(task, call_model, tools_api, max_steps)


# -----------------------------
# Saving and main
# -----------------------------

def save_run(task_name, task, result):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(RUNS_DIR, stamp + "_" + task_name)
    os.makedirs(folder, exist_ok=True)

    with open(os.path.join(folder, "trace.json"), "w", encoding="utf-8") as file:
        json.dump(result["trace"], file, indent=2, ensure_ascii=False)

    summary = {
        "task": task_name,
        "prompt": task["prompt"],
        "stop_reason": result["stop_reason"],
        "steps": result["steps"],
        "final_answer": result["final_answer"],
        "final_checks": result["final_checks"],
        "evidence": result["evidence"],
    }
    with open(os.path.join(folder, "result.json"), "w", encoding="utf-8") as file:
        json.dump(summary, file, indent=2, ensure_ascii=False)
    return folder


def main():
    parser = argparse.ArgumentParser(description="MCP Tool-Hub Agent")
    parser.add_argument("--task", choices=list(TASKS.keys()), default="bug_summary")
    parser.add_argument("--ask", default="", help="Free-text request (generic checks only)")
    parser.add_argument("--inject-failure", type=int, default=None,
                        help="Make get_issue fail for this issue number")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Path to the MCP server script")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    args = parser.parse_args()

    if args.ask != "":
        task_name = "custom"
        task = {"prompt": args.ask, "check": "generic"}
    else:
        task_name = args.task
        task = TASKS[args.task]
    if args.inject_failure is not None:
        task_name = task_name + "_fail" + str(args.inject_failure)

    import llm
    print("TASK:", task["prompt"])
    print("MODEL:", llm.MODEL_NAME)

    if not os.path.exists(args.server):
        task_name = task_name + "_server_down"
    elif args.server != DEFAULT_SERVER:
        task_name = task_name + "_custom_server"

    try:
        if not os.path.exists(args.server):
            raise FileNotFoundError("MCP server script not found: " + args.server)
        result = asyncio.run(run_with_mcp(task, llm.call_model, args.server,
                                          args.inject_failure, args.max_steps))
    except Exception as error:
        # anyio wraps startup errors in an ExceptionGroup; show the real cause.
        while hasattr(error, "exceptions") and len(error.exceptions) > 0:
            error = error.exceptions[0]
        message = str(error)
        if message == "":
            message = type(error).__name__
        print()
        print("MCP SERVER UNAVAILABLE:", message)
        print("No tools could be reached, so the agent did not run and claims nothing.")
        result = {"stop_reason": "mcp_server_unavailable: " + message, "final_answer": "",
                  "final_checks": [], "evidence": {}, "steps": 0, "trace": []}

    folder = save_run(task_name, task, result)

    print()
    print("=" * 64)
    print("STOP REASON:", result["stop_reason"])
    if result["final_answer"] != "":
        print("FINAL ANSWER:")
        print(result["final_answer"])
    print("Saved:", folder)


if __name__ == "__main__":
    main()