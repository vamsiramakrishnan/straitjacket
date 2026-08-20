"""Acceptance: the universal emission gate (ctx.hook._emission_gate).

An over-budget replaceable PostToolUse result is replaced by a shape-preserving
bounded digest carrying a WORKING `ctx get` retrieval ref; under-budget results
pass through byte-identically. Unknown structured values are capture-only.
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


def _updated_text(d: dict) -> str:
    value = d["hookSpecificOutput"]["updatedToolOutput"]
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return value[0]["text"]
    if "stdout" in value:
        return value["stdout"]
    return value["content"][0]["text"]


def test_under_threshold_passes_through(ws):
    d = _run_post({"tool_name": "Bash", "cwd": str(ws),
                   "tool_response": {"stdout": "small ok", "stderr": ""}})
    assert d == {}  # byte-identical no-op


def test_over_threshold_replaces_with_digest(ws):
    d = _run_post(_mcp_payload(ws, _big_json()))
    hso = d["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    uto = _updated_text(d)
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
    uto = _updated_text(d)
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


def test_antigravity_official_post_tool_payload_has_no_output_to_capture(ws):
    d = _run_post(
        {"hook_event_name": "PostToolUse", "stepIdx": 4, "error": None, "cwd": str(ws)},
        flavor="antigravity",
    )
    assert d == {}
    assert not (ws / ".ctx-session-reads" / "telemetry.jsonl").exists()


def test_claude_mcp_replacement_preserves_content_block_shape(ws):
    d = _run_post(_mcp_payload(ws, _big_json()))
    updated = d["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(updated, list)
    assert updated[0]["type"] == "text"
    assert updated[0]["text"].lstrip().startswith("[ctx ")


def test_claude_bash_replacement_preserves_builtin_shape(ws):
    d = _run_post(
        {
            "tool_name": "Bash",
            "cwd": str(ws),
            "tool_response": {
                "stdout": "out\n" * 10000,
                "stderr": "warning\n" * 10000,
                "interrupted": True,
                "isImage": False,
            },
        }
    )
    updated = d["hookSpecificOutput"]["updatedToolOutput"]
    assert set(updated) == {"stdout", "stderr", "interrupted", "isImage"}
    assert updated["stdout"].lstrip().startswith("[ctx ")
    assert updated["stderr"] == ""
    assert updated["interrupted"] is True
    assert updated["isImage"] is False


def test_claude_mcp_envelope_is_bounded_without_losing_public_shape(ws):
    envelope = {
        "content": [{"type": "text", "text": _big_json()}],
        "isError": False,
        "structuredContent": {"rows": json.loads(_big_json())},
        "_meta": {"trace": "x" * 20000},
    }
    d = _run_post(
        {"tool_name": "mcp__fixture__rows", "cwd": str(ws), "tool_response": envelope}
    )
    updated = d["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(updated, dict)
    assert updated["content"][0]["text"].lstrip().startswith("[ctx ")
    assert updated["isError"] is False
    assert updated["structuredContent"] == {"ctxContained": True}
    assert updated["_meta"] == {"ctxContained": True}
    assert len(json.dumps(updated).encode()) < len(json.dumps(envelope).encode()) // 4


def test_claude_unknown_object_is_capture_only_and_books_no_saving(ws):
    from ctx.retrieval import telemetry_summary
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = _run_post(
        {
            "tool_name": "unknown_object_tool",
            "cwd": str(ws),
            "tool_response": {"rows": json.loads(_big_json())},
        }
    )
    assert d == {}
    wsp = resolve_workspace(str(ws))
    totals = telemetry_summary(Store(wsp.workspace_id))
    assert totals["emitted_bytes"] == totals["raw_bytes"]


def test_codex_text_replacement_preserves_success_contract(ws):
    d = _run_post(
        {
            "tool_name": "functions.exec",
            "cwd": str(ws),
            "tool_response": "line\n" * 8000,
        },
        flavor="codex",
    )

    assert d["continue"] is False
    assert d["stopReason"].lstrip().startswith("[ctx ")
    assert "decision" not in d and "reason" not in d


def test_codex_structured_mcp_envelope_is_captured_without_shape_rewrite(ws):
    envelope = {
        "content": [{"type": "text", "text": _big_json()}],
        "isError": False,
        "structuredContent": {"cursor": "next-page", "count": 60},
        "_meta": {"provider": "fixture"},
    }
    d = _run_post(
        {
            "tool_name": "mcp__fixture__large_result",
            "cwd": str(ws),
            "tool_response": envelope,
        },
        flavor="codex",
    )

    # Codex documents updatedMCPToolOutput as unsupported. Passing the original
    # envelope through is the only contract that preserves the caller's
    # success state and structured fields; digest_output still captured it.
    assert d == {}

    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    wsp = resolve_workspace(str(ws))
    store = Store(wsp.workspace_id)
    run_id = store.db.execute(
        "SELECT id FROM objects WHERE kind='run' ORDER BY created_at DESC, id LIMIT 1"
    ).fetchone()[0]
    manifest = store.get_manifest(run_id)
    blob = manifest["streams"]["stdout"]["blob"].removeprefix("sha256:")
    assert json.loads(store.get_blob(blob)) == envelope


def test_codex_local_function_string_result_stays_a_successful_string(ws):
    # Code mode resolves continue:false with stopReason. A string-returning
    # local function therefore keeps its type while the flood becomes a digest.
    d = _run_post(
        {
            "tool_name": "webrun",
            "cwd": str(ws),
            "tool_response": _big_json(),
        },
        flavor="codex",
    )

    assert d["continue"] is False
    assert isinstance(d["stopReason"], str)
    assert d["stopReason"].lstrip().startswith("[ctx ")


def test_codex_arbitrary_object_result_is_capture_only(ws):
    d = _run_post(
        {
            "tool_name": "local_object_tool",
            "cwd": str(ws),
            "tool_response": {"rows": json.loads(_big_json()), "cursor": "next"},
        },
        flavor="codex",
    )

    assert d == {}


def test_codex_known_string_tool_may_arrive_in_transport_envelope(ws):
    d = _run_post(
        {
            "tool_name": "web__run",
            "cwd": str(ws),
            "tool_response": {
                "content": [{"type": "text", "text": _big_json()}],
                "isError": False,
            },
        },
        flavor="codex",
    )

    assert d["continue"] is False
    assert isinstance(d["stopReason"], str)
    assert d["stopReason"].lstrip().startswith("[ctx ")


def test_error_result_gets_larger_budget(ws):
    # A failing result is evidence: its digest budget is scaled up. Compare a
    # log-shaped payload digested as success vs error; error digest >= success.
    log = "\n".join(f"2026-07-18T00:00:{i:02d}Z ERROR something failed n={i}" for i in range(4000))
    ok = _run_post({"tool_name": "Bash", "cwd": str(ws),
                    "tool_response": {"stdout": log, "stderr": ""}})
    err = _run_post({"tool_name": "Bash", "cwd": str(ws), "is_error": True,
                     "tool_response": {"stdout": log, "stderr": ""}})
    ok_uto = _updated_text(ok)
    err_uto = _updated_text(err)
    assert len(err_uto) >= len(ok_uto)


def test_named_test_flood_is_typed_and_disables_next_speculation(ws):
    """PostToolUse closes the speculative-native loop with wire truth."""
    from ctx import reflex

    command = "pytest tests/test_api.py::test_health -q"
    failure = "\n".join(
        [
            "============================= test session starts =============================",
            "_______________________________ test_health ________________________________",
            "E   AssertionError: unhealthy",
            "tests/test_api.py:42: AssertionError",
            "=========================== short test summary info ===========================",
            "FAILED tests/test_api.py::test_health - AssertionError: unhealthy",
            "============================== 1 failed in 0.01s ==============================",
        ]
        + [f"diagnostic filler {i}: {'x' * 80}" for i in range(400)]
    )
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(ws),
        "is_error": True,
        "tool_response": {"stdout": failure, "stderr": ""},
    }
    d = _run_post(payload)
    rendered = _updated_text(d)
    assert "profile=pytest/" in rendered
    assert "test_api.py::test_health" in rendered
    assert reflex.steering_would_bypass(ws, command) is False


def test_small_named_test_result_remains_byte_identical(ws):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/test_api.py::test_health -q"},
        "cwd": str(ws),
        "tool_response": {"stdout": "1 passed in 0.01s\n", "stderr": ""},
    }
    assert _run_post(payload) == {}
    rows = [
        json.loads(line)
        for line in (ws / ".ctx-session-reads" / "steering-decisions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[-1]["event"] == "steering_result"
    assert rows[-1]["gated"] is False
    assert rows[-1]["raw_bytes"] == len("1 passed in 0.01s\n".encode())


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


def test_antigravity_captures_nonstandard_runner_payload_without_booking_saving(ws):
    """A runner may append output beyond Antigravity's documented payload.

    Capture it opportunistically, but never claim the host substituted it.
    Official Antigravity PostToolUse supplies no result bytes at all.
    """
    from ctx.retrieval import telemetry_summary
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    big = "NEEDLE-XYZ\n" + ("filler\n" * 20000)
    d = _run_post({"tool_name": "run_command", "cwd": str(ws),
                   "tool_response": {"stdout": big, "stderr": ""}},
                  flavor="antigravity")
    assert d == {}  # the only legal output on this host

    w = resolve_workspace(str(ws))
    store = Store(w.workspace_id, retention_days=w.config.store.retention_days)
    totals = telemetry_summary(store)
    assert totals["events"] >= 1
    assert totals["raw_bytes"] > 0
    # raw == emitted: the digest never replaced anything, so the ledger books
    # zero tokens avoided rather than crediting a containment that didn't occur.
    assert totals["emitted_bytes"] == totals["raw_bytes"]
    assert totals["est_tokens_avoided"] == 0
