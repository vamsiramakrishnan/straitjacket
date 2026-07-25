"""The universal emission gate must fail CLOSED (finding C1).

``ctx.hook._emission_gate`` is what stops unbounded raw tool output reaching
the model. Its docstring used to promise "Fail-open: any error -> None", which
meant a bug inside gate code silently released the whole flood, with no digest,
no retrieval handle and no telemetry. Over budget, an internal error must
degrade to "bounded + retrievable" -- never to "everything", never to
"nothing", and never to a crashed tool call.

Every test here fails against the pre-fix module.
"""


from __future__ import annotations

import io
import json
import re
import subprocess
import sys

import pytest


# --------------------------------------------------------------- fixtures
@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    return d


def _run_post(payload: dict, flavor: str = "claude-code") -> dict:
    from ctx import hook

    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = io.StringIO()
    try:
        hook.main_post_tool_use(flavor)
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return json.loads(out)


def _run_pre(payload: dict, flavor: str = "antigravity") -> dict:
    from ctx import hook

    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    sys.stdout = io.StringIO()
    try:
        hook.main_pre_tool_use(flavor)
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return json.loads(out)


def _big(n: int = 60) -> str:
    return json.dumps([{"sha": f"c{i:04d}", "msg": "x" * 400, "n": i} for i in range(n)])


def _mcp_payload(ws, text, tool="mcp__github__list_commits") -> dict:
    return {
        "tool_name": tool,
        "cwd": str(ws),
        "tool_response": [{"type": "text", "text": text}],
    }


def _failures(ws) -> list[dict]:
    path = ws / ".ctx-session-reads" / "guard-failures.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ============================================================ C1: the gate
def test_gate_internal_error_withholds_raw_output(ws, monkeypatch):
    """A bug inside the digest path must not release the raw bytes."""
    import ctx.digest

    def boom(*a, **k):
        raise RuntimeError("digest exploded")

    monkeypatch.setattr(ctx.digest, "digest_output", boom)
    raw = _big()
    d = _run_post(_mcp_payload(ws, raw))
    uto = d["hookSpecificOutput"]["updatedToolOutput"]
    assert raw not in uto                                  # raw withheld
    assert len(uto.encode()) < len(raw.encode()) // 2      # bounded
    assert "gate" in uto.lower()                           # says the gate failed


def test_gate_internal_error_carries_a_retrieval_handle(ws, monkeypatch):
    """Fail-closed must not mean data loss: the content stays reachable."""
    import ctx.digest

    monkeypatch.setattr(
        ctx.digest, "digest_output", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    raw = _big()
    uto = _run_post(_mcp_payload(ws, raw))["hookSpecificOutput"]["updatedToolOutput"]
    m = re.search(r"blob:([0-9a-f]{6,64})", uto)
    assert m, uto

    from ctx.retrieval import Selector, get
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    wsp = resolve_workspace(str(ws))
    store = Store(wsp.workspace_id)
    # the handle resolves through the ordinary retrieval path ...
    out = get(store, wsp, f"blob:{m.group(1)}", Selector())
    assert "[ctx get blob:" in out
    # ... and the bytes behind it are the original, losslessly
    full = store.resolve_id(m.group(1), kinds=("blob",))
    assert store.blob_path(full).read_bytes() == raw.encode()


def test_gate_internal_error_is_reported_as_telemetry(ws, monkeypatch):
    import ctx.digest

    monkeypatch.setattr(
        ctx.digest, "digest_output", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    _run_post(_mcp_payload(ws, _big()))
    rows = _failures(ws)
    assert rows, "a failing emission gate must leave a signal"
    assert any(r.get("op") == "emission_gate" for r in rows)
    assert any(r.get("error") == "RuntimeError" for r in rows)


def test_gate_falls_back_to_a_file_handle_when_the_store_is_broken(ws, monkeypatch):
    """Both the digest and the artifact store broken → still bounded, still
    retrievable (a real path under the session ledger)."""
    import ctx.digest
    import ctx.store

    def boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(ctx.digest, "digest_output", boom)
    monkeypatch.setattr(ctx.store, "Store", boom)
    raw = _big()
    uto = _run_post(_mcp_payload(ws, raw))["hookSpecificOutput"]["updatedToolOutput"]
    assert raw not in uto
    spill = list((ws / ".ctx-session-reads" / "gate-fallback").glob("*.txt"))
    assert spill, "content must be retained somewhere retrievable"
    assert spill[0].read_text(encoding="utf-8") == raw
    assert spill[0].name in uto


def test_gate_never_raises_out_of_the_hook(ws, monkeypatch):
    """A failed gate degrades; it never crashes the agent's tool call."""
    import ctx.digest
    import ctx.store

    def boom(*a, **k):
        raise RuntimeError("everything is broken")

    monkeypatch.setattr(ctx.digest, "digest_output", boom)
    monkeypatch.setattr(ctx.store, "Store", boom)
    monkeypatch.setattr("ctx.hook._note_guard_failure", boom, raising=False)
    d = _run_post(_mcp_payload(ws, _big()))
    assert isinstance(d, dict)  # one JSON object on stdout, no traceback


def test_gate_still_passes_small_and_absent_results_through(ws, monkeypatch):
    """Fail-closed must not turn 'nothing to do' into a replacement."""
    import ctx.digest

    monkeypatch.setattr(
        ctx.digest, "digest_output", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert _run_post({"tool_name": "Bash", "cwd": str(ws),
                      "tool_response": {"stdout": "tiny", "stderr": ""}}) == {}
    assert _run_post({"tool_name": "Bash", "cwd": str(ws)}) == {}
    assert _run_post({"cwd": str(ws), "tool_response": None}) == {}


def test_gate_runs_even_when_a_nudge_blows_up(ws, monkeypatch):
    """The gate is the safety net; an unrelated governor must not skip it."""
    def boom(*a, **k):
        raise RuntimeError("nudge exploded")

    monkeypatch.setattr("ctx.hook._navigation_nudge", boom)
    raw = _big()
    d = _run_post(_mcp_payload(ws, raw))
    assert "updatedToolOutput" in d["hookSpecificOutput"]


