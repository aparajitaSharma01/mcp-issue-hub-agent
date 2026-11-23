"""
issue_server.py
A local MCP server (stdio) that exposes the sample issues as tools.

Tools:
  list_issues  - read-only
  get_issue    - read-only
  close_issue  - a WRITE tool. It is listed so the agent can see it exists,
                 but the agent's policy does not permit it, and this server
                 never changes any data even if it is called.

Set the environment variable FAIL_ON_ISSUE=<number> to make get_issue fail
for that issue. This simulates a tool failure for testing.

Do not print to stdout in this file: stdout carries the MCP protocol.
"""

import json
import os

from mcp.server.fastmcp import FastMCP

import issue_store

server = FastMCP("issue-hub")

FAIL_ON_ISSUE = os.environ.get("FAIL_ON_ISSUE", "")


@server.tool()
def list_issues(state: str = "open", label: str = "") -> str:
    """List issues in the sample repository (read-only).
    state: 'open', 'closed', or 'all'. label: optional label filter, for example 'bug'.
    Returns number, title, state, and labels for each issue. Does not include the body."""
    return json.dumps(issue_store.list_issues(state, label))


@server.tool()
def get_issue(number: int) -> str:
    """Read one issue in full, including its body, labels, and comments (read-only)."""
    if FAIL_ON_ISSUE != "" and str(number) == FAIL_ON_ISSUE:
        raise RuntimeError("Simulated backend failure while reading issue #" + str(number))
    return json.dumps(issue_store.get_issue(number))


@server.tool()
def close_issue(number: int, reason: str = "") -> str:
    """Close an issue. This is a WRITE action that would modify repository records."""
    result = {"error": "Refused by server: this demo server is read-only. No changes were made."}
    return json.dumps(result)


if __name__ == "__main__":
    server.run()
