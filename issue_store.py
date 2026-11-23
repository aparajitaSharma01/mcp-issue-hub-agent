"""
issue_store.py
Read-only access to the sample issue file.

Nothing in this file writes to disk. The MCP server and the final checks
both use these functions, so the checks read the same ground truth that
the tools serve, without going through the model.
"""

import hashlib
import json
import os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(PROJECT_DIR, "sample_data", "issues.json")


def load_issues():
    with open(DATA_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)
    return data["issues"]


def list_issues(state="open", label=""):
    if state not in ["open", "closed", "all"]:
        return {"error": "state must be 'open', 'closed', or 'all'"}

    found = []
    for issue in load_issues():
        if state != "all" and issue["state"] != state:
            continue
        if label != "" and label not in issue["labels"]:
            continue
        found.append({
            "number": issue["number"],
            "title": issue["title"],
            "state": issue["state"],
            "labels": issue["labels"],
        })
    return {"count": len(found), "issues": found}


def get_issue(number):
    for issue in load_issues():
        if issue["number"] == number:
            return {"issue": issue}
    return {"error": "Issue #" + str(number) + " not found"}


def open_bug_numbers():
    """Ground truth for the bug_summary task, computed directly from the file."""
    numbers = []
    for issue in load_issues():
        if issue["state"] == "open" and "bug" in issue["labels"]:
            numbers.append(issue["number"])
    return numbers


def data_file_hash():
    """SHA-256 of the sample data. Compared before and after a run to prove nothing changed."""
    with open(DATA_FILE, "rb") as file:
        return hashlib.sha256(file.read()).hexdigest()
