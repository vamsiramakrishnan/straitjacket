"""One name and one join for the house ledger directory (R2).

``.ctx-session-reads`` was spelled inline across the tree under four
different local names (``_LEDGER_DIR``, ``_LEDGER_DIR_NAME``,
``_GENERATION_EXCLUDE_DIR``, ``_SNAPSHOT_EXCLUDE_DIR``) and two join styles,
with several sites re-deriving a parent from a subpath. Nothing disagreed —
the cost was that the next change had to land in every copy. These tests pin
the single definition and the properties the inline copies relied on.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from ctx.sessiondir import LEDGER_DIR_NAME, session_reads_dir, session_reads_path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "ctx"


# ----------------------------------------------------------------- the join
def test_bare_directory():
    assert session_reads_dir("/ws") == "/ws/.ctx-session-reads"
    assert session_reads_dir("/ws") == os.path.join("/ws", LEDGER_DIR_NAME)


@pytest.mark.parametrize(
    "parts,tail",
    [
        ((), ""),
        (("proxy", "window.json"), "/proxy/window.json"),
        (("gate-fallback",), "/gate-fallback"),
        (("guard-failures.jsonl",), "/guard-failures.jsonl"),
        (("sess-123.count",), "/sess-123.count"),
    ],
)
def test_subpaths_are_asked_for_not_re_derived(parts, tail):
    """The subpath forms the old call sites built by hand, including the ones
    that used to construct a parent and join onto it separately."""
    assert session_reads_dir("/ws", *parts) == "/ws/.ctx-session-reads" + tail


def test_accepts_str_and_pathlike_identically(tmp_path):
    assert session_reads_dir(tmp_path) == session_reads_dir(str(tmp_path))
    assert session_reads_path(tmp_path) == session_reads_path(str(tmp_path))


@pytest.mark.parametrize("root", ["/ws", "/ws/", "rel/ws", ".", "/a b/c'd", "/ünï/wß"])
def test_path_flavour_is_an_adapter_not_a_second_definition(root):
    """``session_reads_path`` must be exactly ``Path(session_reads_dir(...))``
    — the call sites that hold a ``Path`` and the ones that hold a ``str``
    cannot be allowed to drift into two different joins."""
    assert session_reads_path(root, "proxy", "window.json") == Path(
        session_reads_dir(root, "proxy", "window.json")
    )


def test_trailing_separator_on_the_root_does_not_double_up():
    assert session_reads_dir("/ws/") == "/ws/.ctx-session-reads"


def test_relative_root_stays_relative():
    """Several callers pass ``.`` or a repo-relative root; the old inline
    joins never absolutized and neither may this one."""
    assert session_reads_dir(".") == "./.ctx-session-reads"
    assert not os.path.isabs(session_reads_dir("rel/ws"))


# ------------------------------------------------- the hot-path constraint
def test_module_imports_os_only():
    """:mod:`ctx.hook` imports this module on every intercepted tool call.
    ``pathlib`` (~4.2 ms) must stay out of its module scope — the ``Path``
    flavour takes it lazily. ``tests/test_hook_hot_path.py`` enforces the
    end-to-end version of this; here we pin the cause."""
    tree = ast.parse((SRC / "sessiondir.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert names == ["__future__", "os"]


# ------------------------------------------------------- no copies survive
def test_the_literal_is_spelled_exactly_once_in_python():
    """The N+1th-author guard: a new call site must reach for the accessor,
    not retype the string."""
    offenders = []
    for py in sorted((REPO / "src").rglob("*.py")) + sorted((REPO / "evals").rglob("*.py")):
        if py.name == "sessiondir.py":
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            # Docstrings and comments may name the directory freely; only a
            # string literal in code is a duplicate definition.
            if re.search(r"""['"]\.ctx-session-reads['"]""", line):
                offenders.append(f"{py.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "the ledger directory name must come from ctx.sessiondir, not a "
        f"literal: {offenders}"
    )


def test_surviving_private_aliases_are_the_shared_constant():
    """Four modules each had their own name for the same string. ``ctx.hook``
    keeps its historical spelling for readability, but only as an import
    alias — object identity is what makes it not a second definition."""
    from ctx import hook

    assert hook._LEDGER_DIR_NAME is LEDGER_DIR_NAME


def test_rust_shim_agrees_with_the_python_constant():
    """The native post-tool-use shim reimplements the hook in Rust and spells
    the directory in its own language. ``tests/test_native_hook.py`` would
    catch drift only when the binary happens to be built; this reads the
    source, so it runs everywhere (same discipline as R8/R9)."""
    rs = (REPO / "native" / "ctx-hook-native" / "src" / "main.rs").read_text(
        encoding="utf-8"
    )
    spelled = set(re.findall(r'"(\.ctx-[a-z-]+)"', rs))
    assert spelled == {LEDGER_DIR_NAME}, spelled


# ------------------------------------------------ the real call sites agree
def test_every_writer_lands_under_one_directory(tmp_path):
    """End-to-end: drive the ledger writers that used to build the path four
    different ways and assert nothing landed beside the directory."""
    from ctx import engagement, hook, resolver

    ws = str(tmp_path)
    engagement.note_call(ws)
    hook._ledger_charge(ws, "sess-01", 4096)
    hook._note_guard_failure(ws, op="o", stage="s", exc=ValueError("x"))
    hook._note_collapse(ws, shape="grep", rung="2")
    resolver._write_reader_state(ws, {"k": 1})

    written = {p.name for p in (tmp_path / LEDGER_DIR_NAME).iterdir()}
    assert written == {
        "engagement.json",
        "sess-01.count",
        "guard-failures.jsonl",
        "collapse.jsonl",
        "reader.json",
    }
    # nothing outside it
    assert {p.name for p in tmp_path.iterdir()} == {LEDGER_DIR_NAME}
