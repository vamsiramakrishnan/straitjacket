"""Operational telemetry and the per-turn retrieval budget gate.

Kept strictly outside stable digests (SPEC §17); failures are swallowed —
telemetry must never block a retrieval call.
"""

from __future__ import annotations

from ctx.store import Store
from ctx.textutil import estimate_tokens
from ctx.workspace import Workspace


_RUN_REF_RE = None


def run_handle(ref_text: object) -> str | None:
    """``run:<hex>`` when ``ref_text`` addresses a run, else None. Only the
    content-addressed id is kept (never a path or a selector)."""
    global _RUN_REF_RE
    if _RUN_REF_RE is None:
        import re

        _RUN_REF_RE = re.compile(r"^run:([0-9a-f]{6,64})(?:[#\s]|$)")
    m = _RUN_REF_RE.match(str(ref_text or "").strip())
    return f"run:{m.group(1)}" if m else None


def record_telemetry(store: Store, op: str, raw_bytes: int, emitted_bytes: int,
                     ref: str | None = None) -> None:
    """Append an operational telemetry event. ``ref`` (a ``get``/``search``
    of a run) records which run handle was fetched from, so a digest's
    follow-up fetches can be counted (``ctx replay --picker``)."""
    import json as _json
    import time as _time

    try:
        path = store.audit_dir / "telemetry.jsonl"
        event = {
            "ts": _time.time(),
            "op": op,
            "raw_bytes": raw_bytes,
            "emitted_bytes": emitted_bytes,
            "est_tokens_avoided": max(0, (raw_bytes - emitted_bytes) // 4),
        }
        handle = run_handle(ref)
        if handle:
            event["ref"] = handle
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        pass


def telemetry_summary(store: Store) -> dict[str, int]:
    import json as _json

    totals = {"events": 0, "raw_bytes": 0, "emitted_bytes": 0, "est_tokens_avoided": 0}
    path = store.audit_dir / "telemetry.jsonl"
    if not path.is_file():
        return totals
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                ev = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            totals["events"] += 1
            for key in ("raw_bytes", "emitted_bytes", "est_tokens_avoided"):
                totals[key] += int(ev.get(key, 0))
    except OSError:
        pass
    return totals


def charge_turn_budget(store: Store, ws: Workspace, emitted_text: str) -> str | None:
    """Enforce the cumulative per-turn retrieval budget when conversation and
    turn identifiers are available (env-provided by the harness)."""
    import os

    conv = os.environ.get("CTX_CONVERSATION_ID")
    turn = os.environ.get("CTX_TURN_ID")
    if not conv or not turn:
        return None
    # encode_exact, not a bare encode: a byte-exact --bytes result carries
    # lone surrogates, and strict UTF-8 would raise here -- turning a correct
    # answer into a crash at the accounting step.
    from ctx.textutil import encode_exact

    tokens = estimate_tokens(len(encode_exact(emitted_text)))
    total = store.add_turn_tokens(conv, turn, tokens)
    limit = ws.config.budgets.turn_retrieval_tokens
    if total > limit:
        return (
            f"[ctx budget] turn retrieval budget exceeded: ≈{total} of {limit} tokens. "
            "Narrow selectors, or checkpoint the conversation into a new epoch."
        )
    return None
