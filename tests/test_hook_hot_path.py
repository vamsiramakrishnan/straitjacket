"""Acceptance: the PreToolUse guard's hot-path latency contract.

`hook.py` runs in a fresh interpreter on every intercepted tool call, so its
import graph is paid thousands of times a session and was measured as the
dominant term in per-call latency — far larger than classification itself.
These tests pin the three properties that keep it that way, so a future edit
that quietly re-adds a heavy import or a per-call full scan fails loudly
instead of costing milliseconds per tool call:

  1. the import graph excludes the known-expensive stdlib modules;
  2. `_failure_available` is incremental over the append-only intervention
     ledger, not a rescan (O(N) per call / O(N^2) per session);
  3. `_symbols_resolvable` is a bounded level-order scan, so a large unrelated
     subtree can neither slow it down without bound nor make it answer False
     for a repo with Python at depth 1.
"""

import json
import subprocess
import sys
from pathlib import Path

from ctx.hook import _LEDGER_DIR_NAME, _failure_available, _symbols_resolvable
from ctx.substitute import Substitution, collapse

# Each of these costs milliseconds to import and none is needed at runtime by
# the guard: `dataclasses` drags in inspect -> ast/dis/tokenize/linecache,
# `typing` is unnecessary because `from __future__ import annotations` makes
# annotations strings, and `pathlib` is replaceable by `os.path` for the joins
# and existence checks the hot path actually does.
_FORBIDDEN = ("typing", "pathlib", "dataclasses", "inspect", "ast", "dis",
              "tokenize", "linecache", "tempfile", "subprocess", "argparse")

# Every module hook.py imports per call, so the same discipline binds them.
_HOT_MODULES = ("ctx.hook", "ctx.engagement", "ctx.reflex", "ctx.substitute")

_PROBE = f"','.join(m for m in {_FORBIDDEN!r} if m in sys.modules)"


def _interpreter_baseline() -> set[str]:
    """Which forbidden modules this interpreter loads before any of our code.

    Not hypothetical: Python 3.13 imports `linecache` during startup, so a bare
    `m in sys.modules` check reads as "the guard imported it" on 3.13 and passes
    on 3.11/3.12. What the contract actually cares about is the cost *we* add,
    so measure the floor and subtract it.
    """
    out = subprocess.run([sys.executable, "-c", f"import sys\nprint({_PROBE})"],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    return {m for m in out.stdout.strip().split(",") if m}


def test_hot_path_import_graph_excludes_expensive_modules():
    src = str(Path(__file__).resolve().parent.parent / "src")
    code = (
        "import sys\n"
        f"for m in {_HOT_MODULES!r}:\n"
        "    __import__(m)\n"
        f"print({_PROBE})\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    loaded = {m for m in out.stdout.strip().split(",") if m}
    added = loaded - _interpreter_baseline()
    assert added == set(), (
        f"hot-path modules now import {', '.join(sorted(added))} — see the "
        "latency contract in hook.py. Move it under `if TYPE_CHECKING:` or into "
        "the function that needs it."
    )


def test_guard_entry_point_does_not_load_expensive_modules():
    """The same check through the real entry point, so a lazy import that only
    fires on a live command path is caught too."""
    root = Path(__file__).resolve().parent.parent
    payload = json.dumps({"session_id": "s", "tool_name": "Bash",
                          "tool_input": {"command": "grep -rn Foo .",
                                         "Cwd": str(root)},
                          "workspacePaths": [str(root)]})
    code = (
        "import sys\n"
        "from ctx.cli import main\n"
        "main()\n"
        f"sys.stderr.write({_PROBE})\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, "hook", "claude-code", "pre-tool-use"],
        input=payload, capture_output=True, text=True,
        env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"}, cwd=str(root))
    assert json.loads(out.stdout)  # exactly one decision was emitted
    loaded = {m for m in out.stderr.strip().split(",") if m}
    added = loaded - _interpreter_baseline()
    assert added == set(), f"entry point loaded {', '.join(sorted(added))}"


# ---------------------------------------------------------------- P6 cursor
def _ledger(ws: Path) -> Path:
    d = ws / _LEDGER_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d / "interventions.jsonl"


def _append(path: Path, doc: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(doc) + "\n")


_PYTEST_LINE = {"family": "pytest", "op": "intervention_emitted"}


def test_failure_available_tracks_appends(tmp_path):
    path = _ledger(tmp_path)
    path.write_text("", encoding="utf-8")
    assert _failure_available(str(tmp_path)) is False
    for i in range(200):
        _append(path, {"family": "grep", "i": i})
    assert _failure_available(str(tmp_path)) is False
    _append(path, _PYTEST_LINE)
    # the cursor must not stop the later append from being seen
    assert _failure_available(str(tmp_path)) is True
    _append(path, {"family": "grep", "i": 999})
    assert _failure_available(str(tmp_path)) is True


def test_failure_available_missing_ledger_is_false(tmp_path):
    assert _failure_available(str(tmp_path)) is False
    assert _failure_available(None) is False


def test_failure_available_rescans_after_rotation(tmp_path):
    path = _ledger(tmp_path)
    _append(path, _PYTEST_LINE)
    assert _failure_available(str(tmp_path)) is True
    # a truncated / rotated ledger must not leave a stale True behind
    path.write_text("", encoding="utf-8")
    _append(path, {"family": "grep", "i": 0})
    assert _failure_available(str(tmp_path)) is False


def test_failure_available_never_splits_a_partial_line(tmp_path):
    path = _ledger(tmp_path)
    line = json.dumps(_PYTEST_LINE)
    path.write_text(line[:20], encoding="utf-8")  # caught mid-append
    assert _failure_available(str(tmp_path)) is False
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line[20:] + "\n")
    assert _failure_available(str(tmp_path)) is True


def test_failure_available_survives_a_corrupt_cursor(tmp_path):
    path = _ledger(tmp_path)
    _append(path, _PYTEST_LINE)
    assert _failure_available(str(tmp_path)) is True
    (tmp_path / _LEDGER_DIR_NAME / "failure-available.json").write_text(
        "{not json", encoding="utf-8")
    # a broken cursor degrades to a full scan, never to a wrong answer
    assert _failure_available(str(tmp_path)) is True


# ----------------------------------------------------------- P7 bounded scan
def test_symbols_resolvable_finds_shallow_python_past_a_large_subtree(tmp_path):
    """A `.py` at depth 1 must be found even when a sibling subtree holds far
    more files than the scan budget. The old depth-first `os.walk` could spend
    the whole budget inside the sibling and answer False."""
    heavy = tmp_path / "vendor"
    d = heavy
    for lvl in range(6):
        d = d / f"l{lvl}"
        d.mkdir(parents=True)
        for j in range(400):
            (d / f"f{j}.ts").write_text("x", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text("x", encoding="utf-8")
    assert _symbols_resolvable(str(tmp_path)) is True


def test_symbols_resolvable_false_without_python(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("x", encoding="utf-8")
    assert _symbols_resolvable(str(tmp_path)) is False
    assert _symbols_resolvable(None) is False


def test_symbols_resolvable_honours_a_scip_index(tmp_path):
    (tmp_path / "index.scip").write_text("", encoding="utf-8")
    assert _symbols_resolvable(str(tmp_path)) is True


# ------------------------------------------------- probes are only paid lazily
def test_collapse_does_not_consult_probes_it_cannot_use():
    """Both probes do real I/O but only two recogniser branches read them, so
    the guard hands them over as thunks. A command that matches no shape, or a
    shape that needs neither answer, must never call them."""
    calls = []
    probes = {
        "failure_available": lambda: (calls.append("failure"), True)[1],
        "symbols_resolvable": lambda: (calls.append("symbols"), True)[1],
    }
    for cmd in ("ls -la", "echo hi", "git status", "npm install",
                "cat notes.txt", "grep -n foo one_file.py",
                "grep -rn Foo . | wc -l", "pytest -k test_x"):
        collapse(cmd, **probes)
    assert calls == [], f"probes evaluated for commands that cannot use them: {calls}"

    # a bare-identifier recursive grep needs `symbols_resolvable` and nothing else
    collapse("grep -rn WidgetFactory .", **probes)
    assert calls == ["symbols"]

    # a bare whole-suite pytest re-run needs `failure_available` and nothing else
    calls.clear()
    collapse("pytest", **probes)
    assert calls == ["failure"]


def test_collapse_still_accepts_plain_bools():
    assert collapse("grep -rn Widget .", symbols_resolvable=False).shape == "grep_content"
    assert collapse("grep -rn Widget .", symbols_resolvable=True).shape == "grep_symbol"
    assert collapse("pytest", failure_available=False) is None
    assert collapse("pytest", failure_available=True).rung == "failure-slice"


def test_substitution_keeps_frozen_dataclass_semantics():
    """`Substitution` is hand-rolled to keep `dataclasses` off the hot path;
    it must behave exactly like the frozen dataclass it replaced."""
    a = Substitution("c", "r", "ru", "sh")
    b = Substitution(command="c", reason="r", rung="ru", shape="sh")
    assert a == b and hash(a) == hash(b)
    assert a != Substitution("other", "r", "ru", "sh")
    assert a.__eq__(object()) is NotImplemented
    assert repr(a) == ("Substitution(command='c', reason='r', rung='ru', shape='sh')")
    for mutate in (lambda: setattr(a, "command", "x"),
                   lambda: setattr(a, "brand_new", 1),
                   lambda: delattr(a, "command")):
        try:
            mutate()
        except AttributeError:
            pass
        else:
            raise AssertionError("Substitution must stay frozen")
