"""
llm.py
Calls a local model through Ollama. Change the model with the AGENT_MODEL
environment variable, for example: AGENT_MODEL=qwen3:8b
"""

import os

import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL_NAME = os.environ.get("AGENT_MODEL", "qwen3:8b")


def call_model(messages):
    request_body = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    response = requests.post(OLLAMA_URL, json=request_body, timeout=180)
    response.raise_for_status()
    return response.json()["message"]["content"]
