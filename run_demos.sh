#!/bin/bash
# Runs all six demo cases. Each run is saved under runs/.
# Uses the project's virtual environment if it exists, so the demos work
# even when the venv is not activated in this terminal.
set -u
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python"
fi
echo "Using Python: $PY"

echo "### 1. Main task: summarize open bug issues"
"$PY" agent.py --task bug_summary
sleep 1

echo "### 2. Problem case: user asks for a write action (close an issue)"
"$PY" agent.py --task close_request
sleep 1

echo "### 2b. Problem case: same request, but the model is NOT told which tools are permitted"
echo "     (the model may try close_issue; the Python policy must block it)"
"$PY" agent.py --task close_request_no_hint
sleep 1

echo "### 3. Problem case: missing information (issue #42 does not exist)"
"$PY" agent.py --task missing_issue
sleep 1

echo "### 4. Problem case: tool failure while reading issue #6"
"$PY" agent.py --task bug_summary --inject-failure 6
sleep 1

echo "### 5. Problem case: MCP server unavailable"
"$PY" agent.py --task bug_summary --server no_such_server.py