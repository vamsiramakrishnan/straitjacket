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


@pytest.mark.parametrize("cmd", [
    'grep -rn "TODO: fix" .',
    'grep -rn "return None" src',
])
def test_content_search_collapses_to_search(cmd):
    sub = collapse(cmd)
    assert sub is not None and sub.shape == "grep_content"
    assert sub.command.startswith("ctx q 'search ")
    assert "| files'" in sub.command


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


def test_glob_hint_carried_through():
    sub = collapse('grep -rn "raise ValueError" src/foo/*.py')
    assert sub is not None and "--glob '*.py'" in sub.command


# ── the hook honours the flag ───────────────────────────────────────────────
def _classify(cmd, workspace):
    return classify({
        "tool_name": "Bash",
        "tool_input": {"command": cmd, "Cwd": str(workspace)},
        "workspacePaths": [str(workspace)],
    })


def test_hook_substitutes_when_collapse_enabled(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\ncollapse = true\n', encoding="utf-8")
    d = _classify("grep -rn WidgetFactory .", tmp_path)
    rw = d.get("rewrite")
    assert rw is not None, f"expected a substitution, got {d}"
    assert rw["updatedInput"]["command"] == "ctx q 'refs WidgetFactory | group file'"


def test_hook_leaves_command_untouched_when_collapse_disabled(tmp_path):
    # no collapse flag: the recursive grep is not rewritten to a ctx q op
    (tmp_path / "ctx.toml").write_text('version = 1\n', encoding="utf-8")
    d = _classify("grep -rn WidgetFactory .", tmp_path)
    rw = d.get("rewrite") or {}
    substituted = rw.get("updatedInput", {}).get("command", "")
    assert not substituted.startswith("ctx q 'refs"), \
        f"collapse fired without the flag: {d}"
