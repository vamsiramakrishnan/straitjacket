"""Process-scoped task hints. No workspace-global last-prompt state."""
from __future__ import annotations

import json
import os
from pathlib import Path

MAX_QUERY = 4096


def current_query() -> str | None:
    path = os.environ.get("CTX_TASK_CONTEXT_FILE")
    if path:
        try:
            with Path(path).open(encoding="utf-8") as handle:
                row = json.loads(handle.read(MAX_QUERY * 6 + 256))
            value = row.get("query")
        except (OSError, ValueError, AttributeError):
            return None
    else:
        value = os.environ.get("CTX_TASK_QUERY")
    return value[:MAX_QUERY] if isinstance(value, str) and value.strip() else None


def submit_prompt(payload: dict) -> None:
    """Replace even empty/malformed prompts, so old hints cannot survive a turn."""
    path = os.environ.get("CTX_TASK_CONTEXT_FILE")
    if not path:
        return
    prompt = payload.get("prompt")
    query = prompt[:MAX_QUERY] if isinstance(prompt, str) else ""
    # The wrapper creates this private file; the hook never chooses a shared path.
    with Path(path).open("w", encoding="utf-8") as handle:
        json.dump({"query": query}, handle)


def main_prompt() -> int:
    import sys

    try:
        payload = json.load(sys.stdin)
        submit_prompt(payload if isinstance(payload, dict) else {})
    except (OSError, ValueError):
        try:
            submit_prompt({})
        except OSError:
            pass
    print("{}")
    return 0
