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
@pytest.mark.parametrize("cmd", [
    "grep -rn TokenBucket .",
    "grep -rnw handle_request src/",
    "rg get_user",
    "git grep -n Foo",
    "grep -rn --include='*.py' router .",
])
def test_symbol_search_is_never_replaced_by_refs(cmd):
    """A symbol lookup answers a different question than a text search: it
    never reads prose or config, and it drops path filters. Searches run as
    themselves; refs is only ever offered as a labelled hint."""
    from ctx.substitute import symbol_hint

    assert collapse(cmd) is None
    hint = symbol_hint(cmd)
    assert hint and "not a substitute for this search" in hint


def test_symbol_grep_is_not_collapsed_when_unresolvable_either():
    assert collapse("grep -rn TokenBucket .", symbols_resolvable=False) is None


@pytest.mark.parametrize("cmd", [
    'grep -rn "TODO: fix" .',
    'grep -rn "return None" src',
])
def test_content_search_is_not_collapsed(cmd):
    from ctx.substitute import symbol_hint

    assert collapse(cmd) is None
    assert symbol_hint(cmd) is None  # not an identifier: no refs hint either


@pytest.mark.parametrize("cmd", ["cat src/app.py", "cat handlers.ts", "cat pkg/main.go"])
def test_cat_source_file_is_not_replaced_by_an_outline(cmd):
    """An outline is not the file's contents: a different answer."""
    assert collapse(cmd) is None


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


def test_pytest_rerun_is_never_replaced_by_the_last_failures():
    """`fails last` reports the previous run without re-running: after an
    edit it answers about code that no longer exists."""
    assert collapse("pytest", failure_available=False) is None
    assert collapse("pytest", failure_available=True) is None


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


def test_glob_scoped_search_runs_as_itself():
    assert collapse('grep -rn "raise ValueError" src/foo/*.py') is None


# ── the hook honours the flag ───────────────────────────────────────────────
def _classify(cmd, workspace):
    return classify({
        "tool_name": "Bash",
        "tool_input": {"command": cmd, "Cwd": str(workspace)},
        "workspacePaths": [str(workspace)],
    })


def test_hook_runs_the_users_own_search_whole(tmp_path):
    # A Python source makes symbols resolvable; the search still runs as
    # itself under bounded capture, and refs rides along only as a hint.
    (tmp_path / "m.py").write_text("class WidgetFactory:\n    pass\n", encoding="utf-8")
    d = _classify("grep -rn WidgetFactory .", tmp_path)
    rw = d.get("rewrite")
    assert rw is not None, f"expected bounded capture, got {d}"
    assert rw["updatedInput"]["command"] == "ctx run --passthrough --shell -- 'grep -rn WidgetFactory .'"
    assert "ctx q 'refs WidgetFactory | group file'" in rw["reason"]
    assert "not a substitute for this search" in rw["reason"]


def test_symbol_grep_without_symbols_also_runs_as_itself(tmp_path):
    d = _classify("grep -rn WidgetFactory .", tmp_path)
    cmd = (d.get("rewrite") or {}).get("updatedInput", {}).get("command", "")
    assert cmd == "ctx run --passthrough --shell -- 'grep -rn WidgetFactory .'", d


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
    reason = d.get("reason", "")
    # Steered to the SAME search in the shell, never to a symbol query as if
    # it were equivalent; refs appears only labelled as a different question.
    assert "Run the same search in the shell" in reason
    assert "rg -n handle_request <path>" in reason
    assert "answers a different question" in reason
    assert "auto-collapsed" not in reason


def test_no_instruction_calls_a_symbol_query_the_same_as_a_search(tmp_path):
    """Every text the guard emits for a search names the search itself."""
    texts = [
        _classify_tool("Grep", {"pattern": "Foo", "output_mode": "content"}, tmp_path)["reason"],
        _classify_tool("Grep", {"pattern": "a b"}, tmp_path)["reason"],
        _classify_tool("Glob", {"pattern": "**/*.py"}, tmp_path)["reason"],
        _classify_tool("grep_search", {"Query": "Foo"}, tmp_path)["reason"],
        _classify("grep -rn Foo .", tmp_path)["rewrite"]["reason"],
    ]
    for text in texts:
        assert "auto-collapsed" not in text and "same op" not in text, text
        if "refs" in text:
            assert "different question" in text.lower(), text


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
