"""Replacement surface — the collapse-substitution layer.

Two levels: the pure recogniser (``ctx.substitute.collapse``) maps each
loop-shape to the right collapsed op and leaves everything else alone; the
hook honours it only when ``guard.collapse`` is enabled, substituting under
the agent's own command via ``updatedInput``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx.hook import classify  # noqa: E402
from ctx.substitute import collapse  # noqa: E402


# ── the pure recogniser ────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd,expected", [
    ("grep -rn TokenBucket .", "ctx q 'refs TokenBucket | group file'"),
    ("grep -rnw handle_request src/", "ctx q 'refs handle_request | group file'"),
    ("rg get_user", "ctx q 'refs get_user | group file'"),
    ("git grep -n Foo", "ctx q 'refs Foo | group file'"),
    ("grep -rn --include='*.py' router .", "ctx q 'refs router | group file'"),
])
def test_symbol_search_collapses_to_refs(cmd, expected):
    sub = collapse(cmd)
    assert sub is not None and sub.command == expected
    assert sub.shape == "grep_symbol" and sub.rung == "reuse-index"


def test_symbol_grep_degrades_to_search_when_unresolvable():
    # symbols_resolvable=False → a symbol hunt becomes bounded content search,
    # never nothing (the agent is never stranded)
    sub = collapse("grep -rn TokenBucket .", symbols_resolvable=False)
    assert sub is not None and sub.shape == "grep_content"
    assert sub.command == "ctx q 'search TokenBucket | files'"


@pytest.mark.parametrize("cmd", [
    'grep -rn "TODO: fix" .',
    'grep -rn "return None" src',
])
def test_content_search_collapses_to_search(cmd):
    sub = collapse(cmd)
    assert sub is not None and sub.shape == "grep_content"
    assert sub.command.startswith("ctx q 'search ")
    assert "| files'" in sub.command


@pytest.mark.parametrize("cmd,rel", [
    ("cat src/app.py", "src/app.py"),
    ("cat handlers.ts", "handlers.ts"),
    ("cat pkg/main.go", "pkg/main.go"),
])
def test_cat_source_file_collapses_to_skeleton(cmd, rel):
    sub = collapse(cmd)
    assert sub is not None and sub.shape == "cat_skeleton"
    assert sub.command == f"ctx stats repo:{rel}"
    assert sub.rung == "skeleton-first"


@pytest.mark.parametrize("cmd", [
    "cat notes.txt", "cat config.json", "cat README.md",  # not source we outline
    "cat a.py b.py",                                        # concatenation, not a read
    "cat data.csv",
])
def test_cat_non_source_or_multi_is_left_alone(cmd):
    assert collapse(cmd) is None


def test_single_file_grep_is_left_alone():
    # bounded already (one file) — handled by the -m cap elsewhere, not here
    assert collapse("grep -n foo bar.py") is None


@pytest.mark.parametrize("cmd", ["ls -la", "cat file.txt", "python build.py", "echo hi"])
def test_unrelated_commands_pass_through(cmd):
    assert collapse(cmd) is None


@pytest.mark.parametrize("cmd", [
    "grep -rn Foo . | wc -l",            # counting — not a listing
    "grep -rn Foo . | cut -d: -f1",      # field extraction
    "grep -rn Foo . | awk '{print $2}'",
    "grep -rn Foo . > out.txt",          # redirect
    "grep -rn Foo . && echo done",       # chain
    "echo $(grep -rn Foo .)",            # command substitution
])
def test_compound_commands_never_clobbered(cmd):
    # a pipe/redirect/chain changes what the agent asked for — must pass through
    assert collapse(cmd) is None


def test_pytest_rerun_gated_on_a_captured_failure():
    # no failure on record → do not touch a (possibly first) run
    assert collapse("pytest", failure_available=False) is None
    # failure on record → collapse the whole-suite re-run to the slice
    sub = collapse("pytest", failure_available=True)
    assert sub is not None and sub.command == "ctx q 'fails last | in-changed'"
    assert sub.rung == "failure-slice"


def test_pytest_already_narrowed_is_left_alone():
    for cmd in ("pytest -k test_x", "pytest tests/test_a.py", "pytest --lf"):
        assert collapse(cmd, failure_available=True) is None


def _assert_runnable(command: str) -> None:
    """The substituted command must actually parse. A collapse that emits
    something ctx cannot run is worse than no collapse: the agent gets an
    exit-2 error instead of the answer it asked for."""
    import shlex

    from ctx.cli import _build_parser

    argv = shlex.split(command)
    assert argv[0] == "ctx"
    _build_parser().parse_args(argv[1:])


def test_glob_hint_carried_through():
    sub = collapse('grep -rn "raise ValueError" src/foo/*.py')
    # The glob rides INSIDE the query string, because `ctx q`'s own parser
    # takes only [--trace] and the query -- a top-level --glob made the
    # substituted command exit 2. This asserts the command PARSES, which is
    # the property that matters; the old assertion pinned the literal broken
    # spelling.
    assert sub is not None and "--glob *.py" in sub.command
    _assert_runnable(sub.command)


# ── the hook honours the flag ───────────────────────────────────────────────
def _classify(cmd, workspace):
    return classify({
        "tool_name": "Bash",
        "tool_input": {"command": cmd, "Cwd": str(workspace)},
        "workspacePaths": [str(workspace)],
    })


def test_hook_substitutes_by_default(tmp_path):
    # collapse is the default posture — no config needed. A Python source makes
    # symbols resolvable, so a symbol grep collapses to refs.
    (tmp_path / "m.py").write_text("class WidgetFactory:\n    pass\n", encoding="utf-8")
    d = _classify("grep -rn WidgetFactory .", tmp_path)
    rw = d.get("rewrite")
    assert rw is not None, f"expected a substitution by default, got {d}"
    assert rw["updatedInput"]["command"] == "ctx q 'refs WidgetFactory | group file'"


def test_symbol_grep_degrades_to_search_without_symbols(tmp_path):
    # no Python / no index → refs can't resolve, so the symbol grep degrades to
    # bounded content search rather than stranding the agent.
    d = _classify("grep -rn WidgetFactory .", tmp_path)
    cmd = (d.get("rewrite") or {}).get("updatedInput", {}).get("command", "")
    assert cmd == "ctx q 'search WidgetFactory | files'", d


def test_break_glass_off_switch(tmp_path):
    # guard.collapse=false disables the whole posture (not a second mode — an
    # off-switch): the recursive grep runs untouched.
    (tmp_path / "m.py").write_text("class WidgetFactory: pass\n", encoding="utf-8")
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\ncollapse = false\n', encoding="utf-8")
    d = _classify("grep -rn WidgetFactory .", tmp_path)
    cmd = (d.get("rewrite") or {}).get("updatedInput", {}).get("command", "")
    assert not cmd.startswith("ctx q "), f"off-switch failed: {d}"


# ── native search tools are removed from the surface under collapse ─────────
def _classify_tool(tool_name, tool_input, workspace):
    return classify({
        "tool_name": tool_name,
        "tool_input": {**tool_input, "Cwd": str(workspace)},
        "workspacePaths": [str(workspace)],
    })


def test_native_grep_denied_and_redirected_by_default(tmp_path):
    # collapse is default → native search is off the surface, denied+redirected
    d = _classify_tool("Grep", {"pattern": "handle_request", "output_mode": "content"}, tmp_path)
    assert d.get("decision") == "deny", f"native Grep should be denied: {d}"
    assert "ctx q 'refs handle_request" in d.get("reason", "")


def test_native_grep_capped_not_denied_when_off(tmp_path):
    # break-glass off → the old behaviour returns: content grep gets a
    # head_limit cap, not a deny
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\ncollapse = false\n', encoding="utf-8")
    d = _classify_tool("Grep", {"pattern": "x", "output_mode": "content"}, tmp_path)
    assert d.get("decision") != "deny"


def test_wrap_removes_native_search_by_default(tmp_path):
    from ctx.wrap import _with_collapse_tool_removal
    # default posture → Grep/Glob removed from the surface, no config needed
    out = _with_collapse_tool_removal(["-p", "do it"], tmp_path)
    assert out[:3] == ["--disallowedTools", "Grep", "Glob"]
    # break-glass off → untouched
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\ncollapse = false\n', encoding="utf-8")
    assert _with_collapse_tool_removal(["-p", "x"], tmp_path) == ["-p", "x"]
