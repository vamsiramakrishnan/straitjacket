"""Acceptance: the universal emission gate (ctx.hook._emission_gate).

An over-budget PostToolUse tool result is replaced by a bounded digest carrying
a WORKING `ctx get` retrieval ref; under-budget results pass through
byte-identically. Claude-code flavor only. Fail-open throughout.
"""

import io
import json
import subprocess
import sys

import pytest


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


def _big_json(n=60) -> str:
    return json.dumps([{"sha": f"c{i:04d}", "msg": "x" * 400, "n": i} for i in range(n)])


def _mcp_payload(ws, text, tool="mcp__github__list_commits") -> dict:
    return {"tool_name": tool, "cwd": str(ws), "tool_response": [{"type": "text", "text": text}]}


def test_under_threshold_passes_through(ws):
    d = _run_post({"tool_name": "Bash", "cwd": str(ws),
                   "tool_response": {"stdout": "small ok", "stderr": ""}})
    assert d == {}  # byte-identical no-op


def test_over_threshold_replaces_with_digest(ws):
    d = _run_post(_mcp_payload(ws, _big_json()))
    hso = d["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    uto = hso["updatedToolOutput"]
    assert "[ctx run:" in uto and "ctx get run:" in uto
    assert "records (head):" in uto  # json/v1 head-N fired
    assert len(uto.encode()) < len(_big_json().encode()) // 4  # real compression


def test_replacement_is_deterministic(ws):
    a = _run_post(_mcp_payload(ws, _big_json()))
    b = _run_post(_mcp_payload(ws, _big_json()))
    assert a == b  # content-addressed id is a pure function of (bytes, tool)


def test_retrieval_ref_resolves_to_original(ws):
    text = _big_json()
    d = _run_post(_mcp_payload(ws, text))
    uto = d["hookSpecificOutput"]["updatedToolOutput"]
    # pull run:<short> out of the digest header
    import re

    m = re.search(r"run:([0-9a-f]{12})", uto)
    assert m
    short = m.group(1)
    from ctx.retrieval import Selector, get
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    wsp = resolve_workspace(str(ws))
    store = Store(wsp.workspace_id)
    out = get(store, wsp, f"run:{short}#stdout", Selector(json_pointer="/0"))
    # retrieval prepends a 2-line header ([ctx get …] / selector: …)
    payload = out[out.index("{"):]
    rec0 = json.loads(payload)
    assert rec0["sha"] == json.loads(text)[0]["sha"]  # exact first record recovered


def test_idempotence_guard_skips_own_digest(ws):
    # A tool result that is already a ctx digest must not be re-digested.
    d = _run_post({"tool_name": "Bash", "cwd": str(ws),
                   "tool_response": "[ctx run:abcdef012345 profile=json/v1]\n" + "y" * 40000})
    assert d == {}


def test_ctx_tools_are_never_digested(ws):
    d = _run_post({"tool_name": "mcp__ctx__ctx", "cwd": str(ws),
                   "tool_response": _big_json()})
    assert d == {}


def test_antigravity_flavor_never_replaces(ws):
    # Antigravity's published PostToolUse contract permits exactly one output,
    # `{}` — no replacement and no nudge. Over-threshold must still emit it.
    d = _run_post(_mcp_payload(ws, _big_json(), tool="grep_search"), flavor="antigravity")
    assert d == {}


def test_error_result_gets_larger_budget(ws):
    # A failing result is evidence: its digest budget is scaled up. Compare a
    # log-shaped payload digested as success vs error; error digest >= success.
    log = "\n".join(f"2026-07-18T00:00:{i:02d}Z ERROR something failed n={i}" for i in range(4000))
    ok = _run_post({"tool_name": "Bash", "cwd": str(ws),
                    "tool_response": {"stdout": log, "stderr": ""}})
    err = _run_post({"tool_name": "Bash", "cwd": str(ws), "is_error": True,
                     "tool_response": {"stdout": log, "stderr": ""}})
    ok_uto = ok["hookSpecificOutput"]["updatedToolOutput"]
    err_uto = err["hookSpecificOutput"]["updatedToolOutput"]
    assert len(err_uto) >= len(ok_uto)


def test_mixed_content_blocks_persisted_losslessly(ws):
    # A mixed [big text + image] result must not silently drop the image:
    # the persisted artifact must contain it (lossless-on-disk). Regression
    # for the review-panel finding at hook.py:_normalize_tool_response.
    from ctx.hook import _normalize_tool_response

    img = "A" * 2000  # stand-in base64 blob
    tr = [{"type": "text", "text": "x" * 20000}, {"type": "image", "data": img}]
    stdout, _ = _normalize_tool_response(tr)
    assert img in stdout  # the image block survived into what gets persisted
    # all-text still takes the clean join path (no json wrapping)
    txt, _ = _normalize_tool_response([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert txt == "a\nb"


def test_ctx_retrieval_output_not_redigested(ws):
    # A large `ctx get` slice run via Bash starts with "[ctx get …]"; the
    # recursion guard must let it pass through untouched (not re-digest it).
    for header in ("[ctx get run:abc123#stdout]", "[ctx search run:abc123]", "[ctx stats run:abc123]"):
        d = _run_post({"tool_name": "Bash", "cwd": str(ws),
                       "tool_response": {"stdout": header + "\n" + "z" * 40000, "stderr": ""}})
        assert d == {}, header


def test_fail_open_on_garbage(ws):
    # Missing tool_response, non-dict, unresolvable — all yield {} not a crash.
    assert _run_post({"tool_name": "Bash", "cwd": str(ws)}) == {}
    assert _run_post({"cwd": str(ws), "tool_response": None}) == {}
