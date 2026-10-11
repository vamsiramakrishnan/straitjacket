"""Delivery observations, kept outside content-addressed evidence.

Rows contain counts and handles, never commands, patterns, output or credentials.
Bytes measure delivery, not provider tokens, cache savings or billed spend.
"""
from __future__ import annotations

import json
import os
import time


def record_tool_call(store, *, op, delivered_bytes, elapsed_seconds,
                     captured_bytes=None, result=None, error=None, producer="sdk"):
    row = {"schema": "ctx.tool-call/v1", "ts": time.time(), "op": op,
        "producer": producer, "delivered_bytes": delivered_bytes,
        "elapsed_seconds": elapsed_seconds, "captured_bytes": captured_bytes,
        "error": error}
    if result is not None:
        row.update(ref=result.ref, delivery=result.delivery,
            command_exit_code=result.command_exit_code, signal=result.signal,
            timed_out=result.timed_out, cancelled=result.cancelled)
        if captured_bytes is not None:
            row["expanded_bytes"] = max(0, delivered_bytes - captured_bytes)
    try:
        data = (json.dumps(row, sort_keys=True) + "\n").encode()
        fd = os.open(store.audit_dir / "tool-calls.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        pass  # Observability never blocks the tool.


def usage_record(usage, *, input_semantics="unknown") -> dict:
    """Preserve SDK usage and label input accounting instead of guessing it.

    `includes_cache`: prompt includes cached tokens. `excludes_cache`: prompt
    is uncached input. `unknown`: no derived totals. Missing counters stay null.
    Candidate output and thinking are distinct reported categories.
    """
    if input_semantics not in {"unknown", "includes_cache", "excludes_cache"}:
        raise ValueError("invalid input token semantics")
    fields = ("prompt_token_count", "cached_content_token_count", "candidates_token_count",
              "thoughts_token_count", "total_token_count")
    raw = {k: usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None) for k in fields}
    valid = all(v is None or type(v) is int and v >= 0 for v in raw.values())
    derived = {"uncached_input": None, "cached_input": raw[fields[1]] if valid else None,
               "gross_input": None, "candidate_output": raw[fields[2]] if valid else None,
               "thinking_output": raw[fields[3]] if valid else None}
    prompt, cached = raw[fields[0]], raw[fields[1]]
    problem = None if valid else "invalid_counter"
    if valid and prompt is not None and cached is not None:
        if input_semantics == "includes_cache":
            if cached > prompt:
                problem = "cached_exceeds_inclusive_prompt"
            else:
                derived.update(uncached_input=prompt-cached, gross_input=prompt)
        elif input_semantics == "excludes_cache":
            derived.update(uncached_input=prompt, gross_input=prompt+cached)
    return {"schema": "ctx.usage/v1", "source": "sdk_reported",
            "input_semantics": input_semantics, "raw": raw, "derived": derived,
            "accounting_error": problem}
