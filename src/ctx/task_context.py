"""Process-scoped task hints and the UserPromptSubmit hook entry point.

No workspace-global last-prompt state. This module is on the hot path of every
prompt (a fresh interpreter each time), so module scope imports json and os
only and the prompt gate (:mod:`ctx.promptgate`) is imported only for a prompt
long enough to need it.
"""
from __future__ import annotations

import json
import os

MAX_QUERY = 4096


def current_query() -> str | None:
    path = os.environ.get("CTX_TASK_CONTEXT_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                row = json.loads(handle.read(MAX_QUERY * 6 + 256))
            value = row.get("query")
        except (OSError, ValueError, AttributeError):
            return None
    else:
        value = os.environ.get("CTX_TASK_QUERY")
    return value[:MAX_QUERY] if isinstance(value, str) and value.strip() else None


def submit_prompt(payload: dict) -> None:
    """Replace even empty/malformed prompts, so old hints cannot survive a turn.

    A prompt that fits MAX_QUERY is saved as before. A longer one is mostly a
    paste: its head and tail are kept, bounded. A prompt with paste markers
    gets an empty hint: the markers' ids are visible to whoever wrote a paste,
    so typed words cannot be told from pasted ones, and the hint feeds digest
    focus."""
    path = os.environ.get("CTX_TASK_CONTEXT_FILE")
    if not path:
        return
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        query = ""
    elif "pasted_content" in prompt:
        query = ""   # typed words cannot be told from pasted ones: no hint (see promptgate.query_hint)
    elif len(prompt) <= MAX_QUERY:
        query = prompt
    else:
        try:
            from ctx.promptgate import query_hint

            query = query_hint(prompt, MAX_QUERY)[:MAX_QUERY]
        except Exception:
            query = prompt[:MAX_QUERY]
    # The wrapper creates this private file; the hook never chooses a shared path.
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"query": query}, handle)


def main_prompt(flavor: str = "claude-code") -> int:
    """UserPromptSubmit hook: save the task hint, then let the prompt gate act.

    Always exits 0 with exactly one JSON object on stdout; any failure lets
    the prompt through unchanged. For a long prompt a deadline is armed
    before ANY work proportional to its size, hint included."""
    import sys

    payload: dict = {}
    try:
        data = json.load(sys.stdin)
        payload = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        payload = {}
    prompt = payload.get("prompt")
    text = "{}"
    # Decide by length first: a short prompt costs nothing more than this.
    if isinstance(prompt, str) and len(prompt) >= 1024:
        text = _long_prompt(payload, flavor)
    else:
        try:
            submit_prompt(payload)
        except OSError:
            pass
    sys.stdout.write(text + "\n")
    return 0


def _long_prompt(payload: dict, flavor: str) -> str:
    text = "{}"
    hinted = False
    try:
        from ctx import promptgate
    except Exception:
        promptgate = None
    timer = promptgate.arm() if promptgate else None
    try:
        try:
            submit_prompt(payload)
            hinted = True
        except Exception:  # noqa: BLE001 - e.g. an unwritable hint file
            pass
        if promptgate is not None:
            out = promptgate.gate(payload, flavor, armed=True)
            if out is not None:
                text = json.dumps(out)
    except BaseException as exc:  # noqa: BLE001 - the deadline is a BaseException
        if promptgate is None or not isinstance(exc, (Exception, promptgate._Deadline)):
            raise
        text = "{}"
    finally:
        if promptgate is not None:
            promptgate.disarm(timer)
    if not hinted:
        try:
            path = os.environ.get("CTX_TASK_CONTEXT_FILE")
            if path:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"query": str(payload.get("prompt", ""))[:MAX_QUERY]}, handle)
        except (OSError, ValueError):
            pass
    return text
