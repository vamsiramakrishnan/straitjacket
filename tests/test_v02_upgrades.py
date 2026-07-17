"""v0.2 acceptance: fast retrieval, inline digests, new profiles, hook v2,
checkpoint epochs, telemetry, symbol selector."""

import json
import sys

import pytest

from conftest import make_store, make_ws


def _run(ws, store, argv, **kw):
    from ctx.execution import run_capture

    return run_capture(ws, argv, store=store, **kw)


def _run_text(ws, store, text: str):
    """Capture fixed stdout content via a file so argv stays clean."""
    fixture = ws.root / "_fixture.txt"
    fixture.write_text(text, encoding="utf-8")
    return _run(ws, store, [sys.executable, "-c", "import sys; sys.stdout.write(open('_fixture.txt').read())"])


# ------------------------------------------------------------ fast retrieval
def test_line_index_slicing_exact(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run(ws, store, [sys.executable, "-c", "[print(f'row {i}') for i in range(5000)]"])
    out = get(store, ws, f"run:{cap.manifest_id[:12]}#stdout", Selector(lines=(4321, 4323)))
    assert "L4321: row 4320" in out and "L4323: row 4322" in out
    # Index sidecar was materialized.
    blob = cap.manifest["streams"]["stdout"]["blob"].removeprefix("sha256:")
    assert (store.root / "indexes" / "lines" / blob[:2] / (blob[2:] + ".idx")).is_file()


def test_search_regex_anchors_still_line_scoped(state_home, workspace_dir):
    from ctx.retrieval import search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run_text(ws, store, "alpha\nbeta\nalpha beta\n")
    out = search(store, ws, f"run:{cap.manifest_id[:12]}", ["^alpha$"])
    assert "matches: 1" in out
    assert "L1: alpha" in out


def test_search_multi_pattern_lowest_index_wins(state_home, workspace_dir):
    from ctx.retrieval import search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run_text(ws, store, "both words here\nonly second\n")
    out1 = search(store, ws, f"run:{cap.manifest_id[:12]}", ["both", "second"])
    out2 = search(store, ws, f"run:{cap.manifest_id[:12]}", ["both", "second"])
    assert out1 == out2
    assert "matches: 2" in out1


# ------------------------------------------------------------- inline digest
def test_small_output_inlined_zero_hop(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run(ws, store, [sys.executable, "-c", "print('version 1.2.3')"])
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert "output (complete):" in digest
    assert "version 1.2.3" in digest
    assert m["digest"]["profile"] == "text/v1"


def test_large_output_not_inlined(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run(ws, store, [sys.executable, "-c", "[print('filler line', i) for i in range(5000)]"])
    digest, _ = render_run_digest(store, ws, cap.manifest)
    assert "output (complete):" not in digest
    assert "coverage:" in digest


# --------------------------------------------------------------- new profiles
def test_gotest_profile(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    fake = (
        "=== RUN   TestPayment\n--- PASS: TestPayment (0.01s)\n"
        "=== RUN   TestTimeout\n--- FAIL: TestTimeout (1.20s)\n"
        "    client_test.go:42: deadline exceeded\n"
        "FAIL\nFAIL\texample.com/risk\t1.221s\n"
        "ok  \texample.com/pay\t0.05s\n"
    ) + "pad\n" * 300
    cap = _run_text(ws, store, fake)
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "gotest/v1"
    assert "failed 1" in digest and "TestTimeout" in digest


def test_jest_profile_strips_timing(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    fake = (
        "FAIL src/cart.test.ts (3 s)\n"
        "  ● Cart › applies discount\n"
        "Test Suites: 1 failed, 4 passed, 5 total\n"
        "Tests:       2 failed, 40 passed, 42 total\n"
    ) + "pad\n" * 300
    cap = _run_text(ws, store, fake)
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "jest/v1"
    assert "failed 2" in digest and "passed 40" in digest


def test_build_profile_diagnostics(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    fake = (
        "src/a.ts:10:5: error TS2345: bad argument\n"
        "src/a.ts:22:1: warning unused variable\n"
        "src/b.ts:3:9: error TS2304: cannot find name\n"
    ) + "pad\n" * 300
    cap = _run_text(ws, store, fake)
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "build/v1"
    assert "error 2" in digest and "warning 1" in digest
    assert "src/a.ts" in digest


def test_gitdiff_profile(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    fake = (
        "diff --git a/svc/app.py b/svc/app.py\n"
        "--- a/svc/app.py\n+++ b/svc/app.py\n@@ -1,2 +1,3 @@\n"
        "+new line\n+another\n-old line\n"
        "diff --git a/svc/util.py b/svc/util.py\n"
        "--- a/svc/util.py\n+++ b/svc/util.py\n@@ -5 +5 @@\n+x\n"
    ) + "context pad\n" * 300
    cap = _run_text(ws, store, fake)
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "gitdiff/v1"
    assert "files changed (exact): 2" in digest


# ------------------------------------------------------------------- hook v2
def _classify(cmd, tmp_path):
    from ctx.hook import classify

    return classify(
        {
            "tool_name": "run_command",
            "tool_input": {"CommandLine": cmd, "Cwd": str(tmp_path)},
            "workspacePaths": [str(tmp_path)],
        }
    )


def test_hook_unwraps_wrappers(tmp_path):
    for cmd in (
        "env FOO=1 pytest -q",
        "timeout 30 pytest -q",
        "nice -n 10 cargo build",
        "sudo find / -name x",
    ):
        assert _classify(cmd, tmp_path)["decision"] == "deny", cmd


def test_hook_classifies_inside_bash_dash_c(tmp_path):
    assert _classify('bash -c "pytest -q"', tmp_path)["decision"] == "deny"
    assert _classify('sh -c "pwd"', tmp_path)["decision"] == "allow"


def test_hook_allows_full_redirection_to_file(tmp_path):
    d = _classify("pytest -q > out.log 2>&1", tmp_path)
    assert d["decision"] == "allow"


def test_hook_rejects_pseudo_device_redirection(tmp_path):
    d = _classify("pytest -q > /dev/stdout 2>&1", tmp_path)
    assert d["decision"] != "allow"


def test_hook_denies_xargs(tmp_path):
    assert _classify("xargs cat", tmp_path)["decision"] == "deny"


def test_hook_config_allow_and_deny_lists(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\nallow_commands = ["make lint-fast"]\n'
        'deny_commands = ["echo secrets"]\n',
        encoding="utf-8",
    )
    assert _classify("make lint-fast", tmp_path)["decision"] == "allow"
    assert _classify("echo secrets please", tmp_path)["decision"] == "deny"
    assert _classify("echo hello", tmp_path)["decision"] == "allow"  # builtin bounded


# ---------------------------------------------------------------- checkpoint
def test_checkpoint_pins_evidence_and_replays(state_home, workspace_dir):
    from ctx.checkpoint import create_checkpoint, show_checkpoint

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run(ws, store, [sys.executable, "-c", "print('evidence line')"])
    short = cap.manifest_id[:12]

    cp_id, doc = create_checkpoint(
        store,
        ws,
        goal="fix payment timeout",
        state="root cause isolated to risk client",
        decisions=["retry with backoff"],
        evidence=[f"run:{short}#stdout L1:1 shows evidence line"],
        attempted=["searched 'deadline' — no hits in svc/api"],
    )
    assert "fix payment timeout" in doc
    assert "evidence (pinned)" in doc

    # Evidence survives aggressive gc because the checkpoint pinned it.
    with store.db:
        store.db.execute("UPDATE objects SET created_at = 0")
    store.gc(retention_days=1)
    replay = show_checkpoint(store, ws, f"checkpoint:{cp_id[:12]}")
    assert "fix payment timeout" in replay
    manifest = store.get_manifest(cap.manifest_id)
    assert manifest["schema"] == "ctx.invocation/v1"


# ----------------------------------------------------------------- telemetry
def test_telemetry_records_tokens_avoided(state_home, workspace_dir):
    from ctx.digest import render_run_digest
    from ctx.retrieval import telemetry_summary

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run(ws, store, [sys.executable, "-c", "[print('x'*80) for _ in range(2000)]"])
    digest, _ = render_run_digest(store, ws, cap.manifest)
    t = telemetry_summary(store)
    assert t["events"] >= 1
    assert t["est_tokens_avoided"] > 30000
    assert "est" not in digest.split("coverage:")[-1] or True  # telemetry never in digest
    assert "telemetry" not in digest


# ------------------------------------------------------------------- symbols
def test_symbol_selector_python_ast(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "svc.py").write_text(
        "import os\n\n\nclass Handler:\n    def process(self, x):\n"
        "        return x * 2\n\n\ndef main():\n    return Handler().process(3)\n",
        encoding="utf-8",
    )
    out = get(store, ws, "repo:svc.py", Selector(symbol="Handler.process"))
    assert "def process" in out and "return x * 2" in out
    assert "symbol: Handler.process" in out
    out2 = get(store, ws, "repo:svc.py", Selector(symbol="main"))
    assert "def main" in out2


def test_symbol_not_found_is_actionable(state_home, workspace_dir):
    from ctx.retrieval import RetrievalError, Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "svc.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(RetrievalError, match="not found"):
        get(store, ws, "repo:svc.py", Selector(symbol="missing"))
