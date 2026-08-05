"""The replacement surface, widened to the commands every repo actually runs.

This surface recognised three shapes (grep-family, `cat`, `pytest`) while the
filter binaries in this space intercept 100+. The gap was never architectural:
a substitution only ships when a bounded `ctx` op means the SAME thing, and
nobody had walked the common commands looking for those pairs.

Most of this file is negative cases, deliberately. A recogniser that fires too
eagerly is worse than one that never fires — it answers a question the operator
did not ask, silently, under their own command. Every rung here has to hold the
line "same question, better answer", and the tests that matter are the ones
pinning where it must decline.
"""

from __future__ import annotations

import pytest

from ctx.substitute import collapse


def _cmd(s):
    sub = collapse(s)
    return sub.command if sub else None


# --------------------------------------------------------------- exact reads
@pytest.mark.parametrize(
    "command,expected",
    [
        ("head -n 20 src/a.py", "ctx get repo:src/a.py --lines 1:20"),
        ("head -n20 src/a.py", "ctx get repo:src/a.py --lines 1:20"),
        # the obsolete-but-ubiquitous form; GNU and BSD both still take it
        ("head -20 src/a.py", "ctx get repo:src/a.py --lines 1:20"),
        # POSIX default is 10 lines
        ("head src/a.py", "ctx get repo:src/a.py --lines 1:10"),
        ("sed -n '5,40p' src/a.py", "ctx get repo:src/a.py --lines 5:40"),
        ("sed -n 5,40p src/a.py", "ctx get repo:src/a.py --lines 5:40"),
    ],
)
def test_range_reads_become_addressed_reads(command, expected):
    """Same bytes, plus an address and a continuation that advances."""
    assert _cmd(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "head -c 100 a.py",       # byte mode is a different range unit
        "head -n -5 a.py",        # "all but the last 5" — not a prefix at all
        "head -0 a.py",           # empty output; nothing to address
        "head -n 5 a.py b.py",    # multi-file mode interleaves banners
        "head -q -n 5 a.py",      # -q changes the framing
        "head",                   # stdin, not a file
        "tail -n 20 a.py",        # NOT handled: `ctx get` has no from-the-end
        #                           window, so any mapping would guess
        "sed -i 's/a/b/' f.py",   # an edit, not a read
        "sed -n '5,40p'",         # stdin
        "sed -n '40,5p' f.py",    # inverted range selects nothing
        "sed -n '0,5p' f.py",     # sed is 1-based; 0 is not a line
        "sed -e '1d' -n '2,3p' f.py",  # a real script, not a range
    ],
)
def test_range_reads_decline_when_the_meaning_would_change(command):
    assert _cmd(command) is None


# ------------------------------------------------------------------- listings
@pytest.mark.parametrize(
    "command,expected",
    [
        ("find . -name '*.py'", "ctx q 'corpus --glob *.py'"),
        ("find src -name '*.ts'", "ctx q 'corpus --glob src/**/*.ts'"),
        ("find src -name '*.ts' -type f", "ctx q 'corpus --glob src/**/*.ts'"),
        ("ls -R", "ctx q corpus"),
        ("ls -R src", "ctx q 'corpus --glob src/**'"),
        ("tree", "ctx q corpus"),
    ],
)
def test_recursive_listings_become_bounded_corpus_listings(command, expected):
    """A recursive walk descends into node_modules/.git/target — usually most
    of its own output. The corpus listing is ignore-aware and carries a
    coverage receipt, and the stream composes onward."""
    assert _cmd(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "ls",                              # flat ls is cheap and honest
        "ls -la",                          # ditto, and -l carries metadata
        "ls -R a b",                       # several roots
        "find . -type d",                  # directories are a different question
        "find . -name '*.py' -newer x",    # a second predicate
        "find . -name a -name b",          # two patterns
        "find . -name '*.py' -exec rm {} ;",   # DESTRUCTIVE — never rewrite
        "find . -name '*.py' -delete",         # ditto
        "find . -size +1M",                # not a name hunt
        "find .",                          # no pattern to translate
    ],
)
def test_listings_decline_when_the_question_differs(command):
    assert _cmd(command) is None


def test_a_destructive_find_is_never_rewritten():
    """Explicit, because this is the one that would actually hurt.

    `-exec`/`-delete` mean the command has an effect. Substituting a read for
    it would silently not do the thing the operator asked for — and they would
    believe it had.
    """
    for command in (
        "find . -name '*.pyc' -delete",
        "find . -name '*.tmp' -exec rm -f {} +",
        "find build -name '*.o' -exec strip {} ;",
    ):
        assert collapse(command) is None


# ---------------------------------------------------------------- line counts
def test_wc_l_becomes_the_outline_that_answers_the_real_question():
    assert _cmd("wc -l src/ctx/hook.py") == "ctx stats repo:src/ctx/hook.py"


@pytest.mark.parametrize(
    "command",
    [
        "wc -l a.py b.py",     # a total line, not a per-file question
        "wc -c a.py",          # bytes
        "wc a.py",             # lines+words+bytes
        "wc -lw a.py",         # combined
        "wc -l data.csv",      # not a source file: no outline to give
        "wc -l",               # stdin
    ],
)
def test_wc_declines_outside_the_single_source_file_case(command):
    assert _cmd(command) is None


# ------------------------------------------------------- the standing rules
@pytest.mark.parametrize(
    "command",
    [
        "head -n 20 a.py | grep foo",
        "head -n 20 a.py > out.txt",
        "find . -name '*.py' && echo done",
        "ls -R; pwd",
        "wc -l a.py || true",
    ],
)
def test_a_composed_command_is_never_clobbered(command):
    """A pipe, redirect or chain means the operator is composing. Rewriting one
    half changes what the whole thing means, so the surface declines outright —
    the rule predates these rungs and must keep covering them."""
    assert _cmd(command) is None


def test_substitutions_are_shell_safe_and_quoted():
    """Every emitted command is a string someone will run. Paths come from the
    operator, so they are untrusted fields."""
    import shlex

    for command in (
        "head -n 5 src/a.py",
        "sed -n '1,2p' src/a.py",
        "wc -l src/ctx/hook.py",
        "find src -name '*.py'",
        "ls -R src",
    ):
        sub = collapse(command)
        assert sub is not None
        # It must parse as a single well-formed command line.
        toks = shlex.split(sub.command)
        assert toks[0] == "ctx"
        assert all(";" not in t and "&&" not in t for t in toks[:2])


def test_a_path_with_shell_metacharacters_is_declined_or_quoted():
    """A filename can contain anything. If we cannot render it safely we must
    decline rather than emit something that would execute differently."""
    import shlex

    for command in (
        "head -n 5 'src/a;rm -rf b.py'",
        "wc -l 'src/$(whoami).py'",
        "find . -name '*.py;x'",
    ):
        sub = collapse(command)
        if sub is not None:
            toks = shlex.split(sub.command)
            assert toks[0] == "ctx"
            assert len(toks) >= 2


@pytest.mark.parametrize(
    "command,shape",
    [
        ("head -n 5 a.py", "head_lines"),
        ("sed -n '1,5p' a.py", "sed_range"),
        ("wc -l a.py", "wc_lines"),
        ("find . -name '*.py'", "find_name"),
        ("ls -R", "ls_recursive"),
    ],
)
def test_every_rung_declares_a_distinct_shape(command, shape):
    """`shape` is what the intervention ledger scores. Two rungs sharing a
    shape makes their adoption indistinguishable in the receipts."""
    sub = collapse(command)
    assert sub is not None and sub.shape == shape


def test_head_replacement_returns_the_same_lines(workspace_dir, state_home):
    """Equivalence, not plausibility.

    The claim a substitution makes is "same question, better answer". If
    `ctx get --lines 1:N` ever stopped returning exactly the first N lines,
    every `head` in every harnessed session would quietly return something
    else under the operator's own command.
    """
    from conftest import make_store, make_ws

    from ctx._retrieval.get import Selector, get

    src = workspace_dir / "a.py"
    body = "".join(f"line {i}\n" for i in range(1, 51))
    src.write_text(body, encoding="utf-8")

    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    out = get(store, ws, "repo:a.py", Selector(lines=(1, 20)))

    shown = [ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("L")]
    assert shown == body.splitlines()[:20], "ctx get --lines 1:20 != head -n 20"


def test_find_replacement_selects_the_same_files(workspace_dir, state_home):
    """`find <dir> -name '<glob>'` vs `corpus --glob <dir>/**/<glob>`.

    The two differ by design on ignored paths — that is the point of the
    substitution — so this pins the agreement on files that are NOT ignored,
    which is the set the operator was asking about.
    """
    from conftest import make_store, make_ws

    from ctx.query import run_query

    (workspace_dir / "pkg").mkdir()
    (workspace_dir / "pkg" / "deep").mkdir()
    expected = ["pkg/a.py", "pkg/b.py", "pkg/deep/c.py"]
    for rel in expected:
        (workspace_dir / rel).write_text("x = 1\n", encoding="utf-8")
    (workspace_dir / "pkg" / "notes.md").write_text("no\n", encoding="utf-8")

    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    text, code = run_query(ws, store, "corpus --glob pkg/**/*.py")
    assert code == 0
    for rel in expected:
        assert rel in text, f"{rel} missing — the glob translation lost a file"
    assert "notes.md" not in text


#: Every command a recogniser can emit, one per rung. A new rung belongs here.
_ALL_RUNG_COMMANDS = [
    "grep -rn WidgetFactory .",
    "cat src/a.py",
    "head -n 20 src/a.py",
    "sed -n '5,40p' src/a.py",
    "wc -l src/a.py",
    "find . -name '*.py'",
    "ls -R",
    "tree",
]


def test_every_substitution_installs_a_bounded_ctx_op():
    """The invariant that makes overriding a DENY safe.

    A substitution replaces the guard's decision outright — including a deny —
    and that is the design: the flagship case (`grep -rn X .`) is denied as
    unbounded, and the replacement surface exists to hand back something
    runnable instead of a refusal. Substituting only on allow was tried while
    writing these rungs and is wrong; it disables the surface on exactly the
    commands it exists for.

    What makes the override safe is therefore NOT the decision being replaced
    but the command being installed. Every rung must emit a bounded `ctx` op,
    so a denied flood becomes something that cannot flood. Left to each
    recogniser's good manners this is one careless rung away from a hole, so
    it is asserted here over all of them at once.
    """
    for command in _ALL_RUNG_COMMANDS:
        sub = collapse(command, failure_available=True, symbols_resolvable=True)
        if sub is None:
            continue
        assert sub.command.startswith("ctx "), (
            f"{command!r} substituted a NON-ctx command {sub.command!r} — this "
            "can replace a deny, so it must be bounded by construction"
        )
        verb = sub.command.split()[1]
        assert verb in {"q", "get", "stats", "search", "map", "def", "refs", "diag"}, (
            f"{command!r} installs `ctx {verb}` — not one of the bounded "
            "retrieval verbs. Execution verbs (run/py/seq/eval/job) must never "
            "be installed by a substitution that can override a deny."
        )


# ------------------------------------------------------- scope preservation
def test_a_directory_scoped_glob_is_never_widened():
    """`src/ctx/*.py` must not become `*.py`.

    `_scope_hint` ran an extension branch before its general glob branch and
    returned only the matched tail, so a search scoped to one directory became
    a search of the entire repository. For a bare `*.py` the two are identical,
    which is why it read as correct; it only widened once a directory prefix
    was present. The extra matches a widened search returns are
    indistinguishable from real ones, which is what makes this worse than
    declining.
    """
    from ctx.substitute import _scope_hint

    assert _scope_hint(["src/ctx/*.py"]) == "src/ctx/*.py"
    assert _scope_hint(["tests/*.py"]) == "tests/*.py"
    assert _scope_hint(["*.py"]) == "*.py"          # bare glob unchanged
    assert _scope_hint(["src/ctx/"]) == "src/ctx/**"
    assert _scope_hint(["a.py", "b.py"]) is None    # not expressible as one


def test_the_preserved_glob_actually_matches_only_that_directory():
    """Scope preservation is only real if the glob dialect agrees."""
    from ctx.pathglob import matches

    assert matches("src/ctx/hook.py", "src/ctx/*.py")
    assert not matches("src/ctx/_retrieval/get.py", "src/ctx/*.py")
    assert not matches("tests/test_a.py", "src/ctx/*.py")


# --------------------------------------------- non-recursive multi-file grep
@pytest.mark.parametrize(
    "command",
    [
        "grep -n pat src/ctx/*.py",   # a glob: the shell expands it to many
        "grep -n pat src/ctx",        # a bare directory: also many
    ],
)
def test_a_non_recursive_grep_over_many_files_collapses(command):
    """`-r` was the wrong discriminator.

    A non-recursive grep was declined wholesale as "single-file and therefore
    already bounded". True of `grep -n pat one.py`; false of
    `grep -n pat src/ctx/*.py`, which the shell expands into however many
    files match. What matters is whether the target names one file or many.
    """
    sub = collapse(command, symbols_resolvable=False)
    assert sub is not None and sub.command.startswith("ctx q ")


def test_a_single_file_grep_is_still_left_alone():
    """The corpus says this is 88% of uncovered greps, and declining is right:
    one file is already bounded, and the `-m` cap covers it."""
    assert collapse("grep -n pat src/ctx/hook.py", symbols_resolvable=False) is None


def test_reasons_name_the_replacement_and_the_cost():
    """The reason string is what the model reads. It has to say what to run
    and why, or the substitution reads as an unexplained refusal."""
    for command in (
        "head -n 5 a.py", "sed -n '1,5p' a.py", "wc -l a.py",
        "find . -name '*.py'", "ls -R",
    ):
        sub = collapse(command)
        assert sub is not None
        assert sub.reason.startswith("CTX_CONTEXT_GUARD:")
        assert "ctx " in sub.reason
        assert len(sub.reason) > 80
