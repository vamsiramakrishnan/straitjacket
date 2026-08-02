"""Four defects from bug-bash round 10, pinned as mechanisms.

Two of them are the unswept halves of mechanisms built in earlier rounds:
`Profile.extract` reached one runner, and the boundary rule that fixed path
globs, intent keywords and MCP providers had not reached the guard's own
command prefixes.
"""

from __future__ import annotations

import pytest


# ------------------------- a grant is token-bounded; a restriction is not
def test_an_allow_prefix_does_not_admit_a_different_token():
    """config.py documents "prefix matches against the canonical argv" and
    the match was a raw startswith, so an allow entry ending mid-token
    admitted a DIFFERENT token sharing its opening characters. Fourth
    instance of the boundary class on this branch, and the first where it
    grants authority rather than shaping output."""
    from ctx.hook import _grants_match

    p = "git push origin main"
    assert _grants_match("git push origin main", p) is True
    assert _grants_match("git push origin main --force", p) is True, "extra ARGS still match"
    assert _grants_match("git push origin main-hotfix --force", p) is False
    assert _grants_match("git push origin mainline", p) is False


def test_a_deny_prefix_stays_deliberately_unbounded():
    """The asymmetry already documented for has_meta a few lines away:
    denying more is always safe, allowing more is not. Tightening the deny
    side would have quietly NARROWED a safety rule while fixing the grant
    side -- a fix in one direction opening a hole in the other."""
    from ctx.hook import _restricts_match

    p = "rm -rf /tmp/scratch"
    assert _restricts_match("rm -rf /tmp/scratch", p) is True
    assert _restricts_match("rm -rf /tmp/scratch/inner", p) is True
    assert _restricts_match("rm -rf /tmp/scratchpad-important", p) is True


def test_the_boundary_is_load_bearing_end_to_end():
    """Demonstrated where the prefix rule is the ONLY thing that could allow
    the command -- `git push` is permitted by the default posture either way,
    so it cannot show this."""
    from ctx.hook import classify_command

    policy = {
        "mode": "guarded", "unknown_command": "force_ask", "internal_error": "allow",
        "steering": "auto", "collapse": False,
        "allow_commands": ["pytest -q tests/unit"], "deny_commands": [],
        "promoted_commands": [], "demoted_commands": [],
    }
    assert classify_command("pytest -q tests/unit", policy)["decision"] == "allow"
    assert classify_command("pytest -q tests/unit -x", policy)["decision"] == "allow"
    assert classify_command("pytest -q tests/unittest_all", policy)["decision"] != "allow"


def test_no_raw_startswith_survives_on_the_policy_prefixes():
    """The invariant: a new prefix rule must pick a side deliberately."""
    import pathlib
    import re

    import ast
    import inspect

    from ctx import hook

    # Exempt the two matchers BY FUNCTION rather than by line text: they are
    # the only places a raw startswith on the canonical argv is the answer,
    # and a line-text exemption would also excuse any future line that
    # happened to mention their names.
    src_path = pathlib.Path(inspect.getfile(hook))
    text = src_path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "_grants_match", "_restricts_match"
        ):
            exempt.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    offenders = [
        f"{i}: {ln.strip()}"
        for i, ln in enumerate(text.splitlines(), 1)
        if re.search(r"canonical\.startswith\(", ln) and i not in exempt
    ]
    assert not offenders, (
        "route policy prefixes through _grants_match (token-bounded) or "
        "_restricts_match (deliberately not):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------- the extract hook reaches every runner
@pytest.mark.parametrize("name", ["GoTestProfile", "CargoTestProfile",
                                  "JestProfile", "UnittestProfile"])
def test_every_test_runner_profile_can_extract(name):
    """Round 9 gave the fact tier a Profile.extract hook and wired ONE
    profile to it; round 10 found the other three still falling back to the
    pytest extractor, so a captured Go/Cargo/Jest failure recorded zero fail
    rows. Building a mechanism is half the work; sweeping every site is the
    other half."""
    from ctx.digest import moreprofs

    profile = getattr(moreprofs, name)()
    assert type(profile).extract is not moreprofs.Profile.extract, (
        f"{name} still inherits the no-op extractor"
    )


def test_go_extraction_finds_the_assertion_coordinate(state_home, workspace_dir):
    from conftest import make_store, make_ws
    from ctx.digest.base import DigestContext
    from ctx.digest.moreprofs import GoTestProfile
    from ctx.execution import run_capture
    import sys

    out = (
        "=== RUN   TestAdd\n"
        "    add_test.go:12: got 3, want 4\n"
        "--- FAIL: TestAdd (0.00s)\n"
        "FAIL\n"
        "FAIL\texample.com/m\t0.002s\n"
    )
    payload = workspace_dir / "go.txt"
    payload.write_text(out, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = run_capture(
        ws, [sys.executable, "-c",
             "import sys;sys.stdout.write(open(sys.argv[1]).read())", str(payload)],
        store=store,
    )
    graph = GoTestProfile().extract(DigestContext.load(store, ws, cap.manifest, focus=None))
    assert graph.outcome == "fail"
    assert [i.id for i in graph.items] == ["TestAdd"]
    assert graph.items[0].location == "add_test.go:12", (
        "go test prints the assertion line UNDER `=== RUN`, before the "
        "`--- FAIL:` banner -- the search has to look backwards"
    )


def test_a_runner_with_no_failures_reports_pass():
    """The graph vocabulary is ("pass","fail",...) -- getting it wrong once
    raised inside a fail-open caller and read as an empty census."""
    from ctx.digest.moreprofs import GoTestProfile, _graph_of

    g = _graph_of(GoTestProfile(), [])
    assert g.outcome == "pass" and g.items == ()


# ------------------------------------ an accounting line about its own cut
def test_the_truncation_note_reports_what_was_kept():
    """`shown≈N` reported the nominal BUDGET, so after the line-boundary trim
    discarded content the note overclaimed how much was shown -- an
    accounting line wrong about its own accounting."""
    import re

    from ctx.textutil import bounded, encode_exact, estimate_tokens

    text = "\n".join(f"line {i} " + "x" * 70 for i in range(400))
    out = bounded(text, 40)
    m = re.search(r"shown≈(\d+) of ≈(\d+)", out)
    assert m, out
    body = out.split("\n[ctx:truncated")[0]
    actual = estimate_tokens(len(encode_exact(body)))
    assert int(m.group(1)) == actual, (
        f"note claims {m.group(1)}, body is {actual}"
    )
    assert int(m.group(1)) <= int(m.group(2))


# --------------------------- a mutation may not exceed what was reviewed
def test_preview_declares_the_files_it_hid():
    from ctx.astgrep import bounds  # noqa: F401  (import guard only)

    # The meta contract the CLI renders from.
    import inspect

    from ctx import astgrep

    src = inspect.getsource(astgrep.rewrite_preview)
    assert "preview_omitted" in src, "the hidden count must be reported"
    assert "files_previewed" in src


def test_apply_refuses_to_exceed_the_reviewed_set():
    """A bounded DISPLAY with a declared remainder is the house style for a
    READ. For a mutation, "you reviewed 200, I changed 210" is not something
    a note repairs -- the extra ten are already written."""
    import inspect

    from ctx.commands import rewrite

    src = inspect.getsource(rewrite)
    assert "apply refused" in src
    assert "beyond the" in src
