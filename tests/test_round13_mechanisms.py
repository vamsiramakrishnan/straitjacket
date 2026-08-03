"""Ten defects from bug-bash round 13 (10/10 confirmed, precision 1.0).

Three of them are the boundary class -- its FIFTH appearance on this branch
after path globs, intent keywords, MCP provider names and the guard's
command prefixes. One is the bounds class at five more sites. Both now have
adoption invariants; see tests/test_bounds_adoption.py and
test_no_unanchored_segment_containment below.
"""

from __future__ import annotations

import pytest


# ------------------------------------------- boundary class, 5th appearance
def test_is_production_matches_path_segments_not_substrings():
    """`"test/" in rel` filed `latest/releases.py` and
    `contests/leaderboard.py` as test code, dropping real production callers
    out of the first-party group of every callers/callees answer."""
    from ctx.callgraph import _is_production

    assert _is_production("src/ctx/hook.py") is True
    assert _is_production("latest/releases.py") is True
    assert _is_production("contests/leaderboard.py") is True
    assert _is_production("protests/x.py") is True
    assert _is_production("tests/test_hook.py") is False
    assert _is_production("src/test/helper.py") is False
    assert _is_production("evals/devex/run.py") is False


def test_reflex_shell_flag_stops_at_the_separator():
    """`"--shell" in rest` scanned the whole argv INCLUDING the wrapped
    command, so a command whose own arguments mention the literal token
    `--shell` was signed as `ctx run --shell`."""
    from ctx.reflex import command_signature

    # A wrapped command mentioning `--shell` must sign exactly as it would
    # without it: the token belongs to the WRAPPED argv, not to ctx.
    # `ctx run -- X` must reduce to X's OWN signature. Under the defect the
    # literal token made it take the --shell path, which signs only the
    # program name and throws the rest of the argv away -- collapsing two
    # genuinely different commands onto one signature, in the component
    # whose entire job is telling re-runs apart.
    wrapped = "grep -rn --shell src/"
    assert command_signature(f"ctx run -- {wrapped}") == command_signature(wrapped)


def test_scip_local_symbols_have_no_descriptor_name():
    """SCIP's local convention is `local <id>`, and the word "local" matched
    the identifier regex -- so a local symbol returned the plausible-looking
    name "local" where the docstring promises None."""
    from ctx.scip_ingest import descriptor_name

    assert descriptor_name("local 3") is None
    assert descriptor_name("local 12") is None
    assert descriptor_name("scip-python python . . `mod`/Klass#method().") is not None


def test_no_unanchored_segment_containment():
    """The invariant for the class itself.

    Five times now a token meant as a whole path segment / whole word was
    tested with a bare `in`. This catches the shape at its most common
    spelling: a containment test against a tuple/frozenset of path-segment
    tokens. It is deliberately narrow -- a broad "no `in` anywhere" rule
    would be noise -- but it covers the site that keeps recurring.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "ctx"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # `any(seg in rel for seg in SOMETHING_WITH_SLASHES)`
            if not isinstance(node, ast.GeneratorExp):
                continue
            cmp_node = node.elt
            if not (isinstance(cmp_node, ast.Compare) and cmp_node.ops
                    and isinstance(cmp_node.ops[0], ast.In)):
                continue
            target = cmp_node.comparators[0]
            if not isinstance(target, ast.Name):
                continue
            # Only flag when the haystack name suggests a PATH.
            if target.id in ("rel", "path", "rel_path", "filename"):
                offenders.append(
                    f"{path.relative_to(src)}:{node.lineno}: "
                    f"substring test against {target.id!r}"
                )
    assert not offenders, (
        "a path-segment token tested with a bare `in` matches any path that "
        "merely CONTAINS it (latest/ contains test/):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------- a substitution preserves meaning
@pytest.mark.parametrize("cmd", [
    "grep -rv NEEDLE src/",
    "grep -r --invert-match NEEDLE src/",
    "grep -rL NEEDLE src/",
    "grep -rc NEEDLE src/",
    "grep -rvn NEEDLE src/",
])
def test_semantic_grep_flags_are_never_collapsed(cmd):
    """`-v` inverts the match, so collapsing to `ctx q search` returned the
    files that DO contain the pattern when the caller asked for the ones
    that do not -- the opposite answer, silently."""
    from ctx.substitute import collapse

    assert collapse(cmd, failure_available=False, symbols_resolvable=False) is None


def test_word_match_is_still_collapsed():
    """`-w` is the one semantic flag the substitution PRESERVES: word-boundary
    matching is exactly what `ctx q refs` does."""
    from ctx.substitute import collapse

    sub = collapse(
        "grep -rnw handle_request src/", failure_available=False, symbols_resolvable=True
    )
    assert sub is not None and "refs" in sub.command


# ------------------------------------------ zero is an answer, five more sites
def test_plan_op_caps_honour_an_explicit_zero():
    """`int(args.get(k, D) or D)` is the zero-means-unset spelling with a
    call on the left, which the bounds adoption test could not see."""
    import inspect

    from ctx import plan_ops

    src = inspect.getsource(plan_ops)
    assert 'or DEFAULT_ROW_CAP)' not in src, "the `or DEFAULT` idiom must be gone"
    assert src.count("bounds.explicit(args.get(") >= 5


# ------------------------------------- a missing value is not a value
def test_multi_flag_refuses_a_following_flag():
    """_flag has always refused a missing value; _multi_flag swallowed the
    next token whatever it was, so `--glob --ext py` globbed for the literal
    string "--ext"."""
    from ctx.query import QueryError, _multi_flag

    assert _multi_flag(["--glob", "*.py"], "--glob") == ["*.py"]
    with pytest.raises(QueryError):
        _multi_flag(["--glob", "--ext", "py"], "--glob")
    with pytest.raises(QueryError):
        _multi_flag(["--glob"], "--glob")


# ------------------------------- the documented error, not a raw crash
def test_read_blob_lines_reports_a_collected_blob_by_name(state_home, workspace_dir):
    """gc sweeps blobs but not their line-index sidecars, so line_index()
    succeeded and the open() then did not -- a raw FileNotFoundError where
    every other missing-object path raises UnknownIdError (exit 2)."""
    from conftest import make_store, make_ws
    from ctx.store import UnknownIdError

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(b"one\ntwo\nthree\n")
    store.line_index(blob)  # materialise the sidecar
    store.blob_path(blob).unlink()  # simulate the sweep
    with pytest.raises(UnknownIdError):
        store.read_blob_lines(blob, 1, 2)


# ------------------------- pytest writes class-scoped headers in dotted form
def test_block_start_matches_a_class_scoped_nodeid():
    """The summary line is "::"-qualified and the traceback header is
    DOTTED, so a "::"-only comparison never matched for ANY class-based
    test and every one of them silently lost its traceback block."""
    from ctx.rundiff import _block_start

    blocks = [("TestFoo.test_bar", 12), ("test_plain", 40)]
    assert _block_start(blocks, "tests/t.py::TestFoo::test_bar") == 12
    assert _block_start(blocks, "tests/t.py::test_plain") == 40
    assert _block_start(blocks, "tests/t.py::TestFoo::test_missing") is None


# -------------------------------- one file set, whichever rung produced it
def test_both_listing_rungs_agree_about_symlinks(state_home, workspace_dir):
    """`is_file()` FOLLOWS a link, so the git rung listed tracked symlinks
    regardless of the documented `follow_symlinks = false` while the walk
    rung honoured it -- the file set depending on whether git was available."""
    from conftest import make_ws

    (workspace_dir / "real.py").write_text("x = 1\n", encoding="utf-8")
    (workspace_dir / "link.py").symlink_to(workspace_dir / "real.py")
    ws = make_ws(workspace_dir)
    assert ws.config.workspace.follow_symlinks is False
    files = ws.list_files()
    assert "real.py" in files
    assert "link.py" not in files, "follow_symlinks = false must hold on every rung"


# ------------------------------------- a count nobody gates will drift
def test_no_hardcoded_command_count_in_prose():
    """The docstring claimed 34 while the dispatch table had 36 and the
    --help footer computed its own. A number with no gate drifts; the fix is
    to stop asserting it in prose, not to correct it once."""
    import re

    from ctx import cliux

    assert not re.search(r"has \d+ commands", cliux.__doc__ or ""), (
        "derive the count from all_commands() instead of restating it"
    )
    assert len(cliux.all_commands()) >= 30
