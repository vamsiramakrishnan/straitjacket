"""Acceptance: rtk-inspired wave — lint/v1 diagnostics profile, scaffold-slim
inline emission, failure-asymmetric budgets, and `ctx gain`."""

import subprocess
import sys
import textwrap

import pytest

ESLINT_OUT = textwrap.dedent("""\
    /proj/src/app1.js
       1:1   error  Unexpected var, use let or const instead  no-var
       1:18  error  Strings must use singlequote              quotes
       2:9   error  Expected '===' and instead saw '=='       eqeqeq
       2:27  error  Missing semicolon                         semi

    /proj/src/app2.js
       3:1   error  Unexpected var, use let or const instead  no-var
       4:9   warning  Some warning here                       eqeqeq
       5:1   error  Missing semicolon                         semi
       6:1   error  Missing semicolon                         semi

    ✖ 8 problems (7 errors, 1 warning)
    """)

RUFF_OUT = "\n".join(
    f"F841 [*] Local variable `v{i}` is assigned to but never used\n"
    f" --> mod{i % 2}.py:{i + 1}:5\n  |"
    for i in range(12)
)

TSC_OUT = "\n".join(
    f"main.ts({i},7): error TS2322: Type 'string' is not assignable to type 'number'."
    for i in range(1, 12)
)


def _ctx_for(tmp_path, text):
    from ctx.digest.base import DigestContext, StreamView
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(tmp_path))
    out = StreamView("stdout", len(text.encode()), len(text.splitlines()), "text/plain", text, True)
    err = StreamView("stderr", 0, 0, "text/plain", "", True)
    manifest = {
        "argv": ["lint"], "cwd": ".", "shell": False,
        "result": {"exitCode": 1, "signal": None, "timedOut": False},
        "streams": {"stdout": {"blob": "sha256:x"}, "stderr": {"blob": "sha256:y"}},
    }
    return DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)


def test_lint_profile_eslint_stylish(tmp_path):
    from ctx.digest.lintprof import LintProfile

    p = LintProfile()
    ctx = _ctx_for(tmp_path, ESLINT_OUT)
    assert p.detect(ctx)
    body = p.render(ctx)
    assert "diagnostics (exact): 8 · error 7 · warning 1" in body
    assert "no-var×2" in body and "semi×3" in body  # by rule
    assert "src/app1.js×4" in body  # by file, shortened to 2 components
    assert "first diagnostic stdout:L" in body  # stream-qualified coordinates
    assert "|    1:1   error" in body  # region inlined


def test_lint_profile_ruff_new_format_and_tsc(tmp_path):
    from ctx.digest.lintprof import LintProfile

    p = LintProfile()
    ctx = _ctx_for(tmp_path, RUFF_OUT)
    assert p.detect(ctx) is not None
    body = p.render(ctx)
    assert "diagnostics (exact): 12" in body
    assert "F841×12" in body

    p2 = LintProfile()
    ctx2 = _ctx_for(tmp_path, TSC_OUT)
    assert p2.detect(ctx2)
    assert "TS2322×11" in p2.render(ctx2)


def test_lint_stderr_diagnostics_get_local_coordinates(tmp_path):
    """PR-review regression: stdout noise before stderr diagnostics must not
    shift span coordinates — each diagnostic keeps its stream + local line."""
    from ctx.digest.base import DigestContext, StreamView
    from ctx.digest.lintprof import LintProfile
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(tmp_path))
    noise = "\n".join(f"building chunk {i}..." for i in range(50))
    out = StreamView("stdout", len(noise.encode()), 50, "text/plain", noise, True)
    err = StreamView("stderr", len(TSC_OUT.encode()), len(TSC_OUT.splitlines()),
                     "text/plain", TSC_OUT, True)
    manifest = {
        "argv": ["tsc"], "cwd": ".", "shell": False,
        "result": {"exitCode": 1, "signal": None, "timedOut": False},
        "streams": {"stdout": {"blob": "sha256:x"}, "stderr": {"blob": "sha256:y"}},
    }
    ctx = DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)
    p = LintProfile()
    assert p.detect(ctx)
    body = p.render(ctx)
    # First diagnostic is stderr line 1 — NOT combined line 51.
    assert "first diagnostic stderr:L1-" in body
    assert "L51" not in body
    assert "#stderr --lines 1:" in body  # suggestion targets the right stream
    assert "| main.ts(1,7)" in body  # inline slice from stderr, not stdout


def test_lint_profile_declines_prose(tmp_path):
    from ctx.digest.lintprof import LintProfile

    prose = "\n".join(f"just a log line {i} with no diagnostics" for i in range(100))
    assert LintProfile().detect(_ctx_for(tmp_path, prose)) is None


# --------------------------------------------------- slim inline + asymmetry
@pytest.fixture()
def ws_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


def test_small_success_output_has_minimal_scaffold(ws_store):
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    ws, store = ws_store
    cap = run_capture(ws, [sys.executable, "-c", "print('v 1.2.3')"], store=store)
    digest, _ = render_run_digest(store, ws, cap.manifest)
    assert "output (complete):" in digest and "v 1.2.3" in digest
    # Scaffold-slim: no cwd/stdout-stats/coverage lines, no indentation.
    assert "cwd:" not in digest
    assert "coverage:" not in digest
    assert "\n  v 1.2.3" not in digest  # content is unindented
    # Total overhead over raw content is small and bounded. The budget is
    # relative to the rendered command line — the digest legitimately
    # carries it, and interpreter paths vary by environment (a venv-deep
    # sys.executable must not fail a fixed budget).
    cmd_line = " ".join([sys.executable, "-c", "print('v 1.2.3')"])
    assert len(digest) < len(cmd_line) + len("v 1.2.3") + 160


def test_failure_budget_factor_applied(ws_store, tmp_path, monkeypatch, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    (ws.root / "ctx.toml").write_text(
        "version = 1\n[budgets]\ndigest_tokens = 60\nfailure_budget_factor = 3.0\n",
        encoding="utf-8",
    )
    code = (
        "import sys\n"
        "[print(f'ERROR: item {i} failed with code {i%7}') for i in range(4000)]\n"
        "sys.exit(1)\n"
    )
    rc = main(["--workspace", str(ws.root), "run", "--", sys.executable, "-c", code])
    assert rc == 3  # failing command propagates
    fail_out = capsys.readouterr().out
    # A failing run gets 3x the emission budget of the same-size success.
    code_ok = code.replace("sys.exit(1)", "")
    main(["--workspace", str(ws.root), "run", "--", sys.executable, "-c", code_ok])
    ok_out = capsys.readouterr().out
    assert len(fail_out) > len(ok_out) * 1.5


def test_cmd_gain_reports_savings(ws_store, capsys):
    from ctx.cli import main
    from ctx.retrieval import record_telemetry

    ws, store = ws_store
    record_telemetry(store, "run", 400_000, 2_000)
    record_telemetry(store, "search", 90_000, 1_000)
    rc = main(["--workspace", str(ws.root), "gain"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ctx gain" in out
    assert "est tokens kept out of context: 121,750" in out
    assert "run" in out and "search" in out
    # Host-neutral dollar framing: no session model recorded here. A min->max
    # sweep of the price table spans ~100x, which is not a number a human can
    # act on, so quote ONE figure at the table's mid-tier fallback and name the
    # assumption (plus how to make it exact) instead of a meaningless band.
    assert "input-priced" in out
    assert "no model seen yet" in out
    assert "/Mtok in" in out
    assert "ctx wrap" in out  # tells the user how to get their real rate
    for vendor_model in ("sonnet", "haiku"):
        assert vendor_model not in out  # no Claude-specific hardcode leaks


def test_cmd_gain_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "s2"))
    from ctx.cli import main

    d = tmp_path / "empty"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    assert main(["--workspace", str(d), "gain"]) == 1
