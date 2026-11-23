"""
policy.py
Tool-access policy, enforced in Python before any tool runs.

The model is told which tools are permitted, but the model's word is not
trusted: every proposed tool call passes through check_tool_permission().
"""

PERMITTED_TOOLS = ["list_issues", "get_issue"]


def check_tool_permission(tool_name, discovered_tool_names):
    if tool_name not in discovered_tool_names:
        return "unknown", "'" + tool_name + "' is not a tool on the MCP server."
    if tool_name not in PERMITTED_TOOLS:
        return "blocked", "'" + tool_name + "' exists on the server but is NOT permitted for this agent."
    return "allowed", "'" + tool_name + "' is permitted."
