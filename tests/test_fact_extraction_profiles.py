"""The fact tier knows every runner the digest tier knows.

`facts.derive_run` called `extract_pytest` unconditionally, so the census
knew exactly one runner while the digest knew several. A captured
`python -m unittest` run rendered as `unittest/v1` with real failures and
inserted ZERO fail rows -- and `ctx q 'fails last'` then answered "no
captured test run" about a run captured seconds earlier. The census is
supposed to BE the work queue.

Extraction is now a Profile hook, so a new runner feeds the census the day
it can extract rather than becoming the next silent blind spot.
"""

from __future__ import annotations

import pytest


#: NOTE the traceback path is a placeholder replaced per-test: a location is
#: an ADDRESS, so it is only emitted when the file is genuinely inside the
#: workspace. The first version of this file hard-coded `/repo/test_foo.py`
#: -- outside any test workspace -- and asserted the basename fallback as
#: correct, pinning a fabricated address as the contract.
UNITTEST_OUTPUT = """\
test_bad (test_foo.MyTest.test_bad) ... FAIL

======================================================================
FAIL: test_bad (test_foo.MyTest.test_bad)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "{TESTFILE}", line 6, in test_bad
    self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 1 test in 0.001s

FAILED (failures=1)
"""

OLD_STYLE = UNITTEST_OUTPUT.replace(
    "FAIL: test_bad (test_foo.MyTest.test_bad)", "FAIL: test_bad (test_foo.MyTest)"
)


def _extract(text: str, workspace_dir):
    text = text.replace("{TESTFILE}", str(workspace_dir / "test_foo.py"))
    from conftest import make_store, make_ws
    from ctx.digest import detect_profile
    from ctx.digest.base import DigestContext
    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    import sys

    payload = workspace_dir / "out.txt"
    payload.write_text(text, encoding="utf-8")
    cap = run_capture(
        ws,
        [sys.executable, "-c",
         "import sys;sys.stdout.write(open(sys.argv[1]).read())", str(payload)],
        store=store,
    )
    dctx = DigestContext.load(store, ws, cap.manifest, focus=None)
    profile, _ = detect_profile(dctx)
    return profile, profile.extract(dctx)


def test_the_unittest_profile_extracts_failures(state_home, workspace_dir):
    profile, graph = _extract(UNITTEST_OUTPUT, workspace_dir)
    assert profile.version == "unittest/v1"
    assert graph is not None, "the profile the digest renders with must extract too"
    assert graph.outcome == "fail"
    assert len(graph.items) == 1
    item = graph.items[0]
    assert item.kind == "failing_test"
    assert item.failure_class == "AssertionError"
    assert item.location == "test_foo.py:6", (
        "an in-workspace traceback path becomes a repo-relative ADDRESS"
    )


def test_the_identity_is_not_doubled_on_modern_unittest(state_home, workspace_dir):
    """Python 3.11+ prints `FAIL: test_x (mod.Class.test_x)` -- the method is
    already in the parens -- while older versions print `(mod.Class)`.
    Appending unconditionally gave `mod.Class.test_x.test_x`."""
    _, graph = _extract(UNITTEST_OUTPUT, workspace_dir)
    assert graph.items[0].id == "test_foo.MyTest.test_bad"


def test_the_older_parenthesised_form_still_resolves(state_home, workspace_dir):
    _, graph = _extract(OLD_STYLE, workspace_dir)
    assert graph.items[0].id == "test_foo.MyTest.test_bad"


def test_the_outcome_vocabulary_is_the_graph_s_own():
    """("pass","fail","error","warning","unknown") -- not the run-outcome
    words used elsewhere. Getting it wrong raised inside a fail-open caller,
    so a broken extractor and a clean run looked identical from outside."""
    from ctx.evidence import EvidenceGraph

    with pytest.raises(ValueError, match="unknown outcome"):
        EvidenceGraph(family="x", profile_version="x/v1", outcome="failure",
                      aggregate={}, items=(), artifacts={})


def test_every_profile_answers_the_extract_hook():
    """None is a legitimate answer -- "this profile has no extractor yet" --
    but the hook must exist on all of them, so the fact tier can ask without
    knowing which runner it is looking at."""
    from ctx.digest import _PROFILES

    for profile in _PROFILES:
        assert hasattr(profile, "extract"), profile.version


def test_the_pytest_profile_still_extracts(state_home, workspace_dir):
    """The rerouting must not cost the runner that already worked."""
    pytest_output = (
        "========================= test session starts ==========================\n"
        "collected 1 item\n\n"
        "tests/test_app.py F                                              [100%]\n\n"
        "=============================== FAILURES ===============================\n"
        "______________________________ test_beta _______________________________\n"
        "    def test_beta():\n"
        ">       assert compute() == 2\n"
        "E       AssertionError: assert 3 == 2\n\n"
        "tests/test_app.py:7: AssertionError\n"
        "======================= short test summary info ========================\n"
        "FAILED tests/test_app.py::test_beta - AssertionError: assert 3 == 2\n"
        "===================== 1 failed in 0.02s ================================\n"
    )
    profile, graph = _extract(pytest_output, workspace_dir)
    assert profile.version.startswith("pytest")
    assert graph is not None and len(graph.items) >= 1


def test_an_out_of_workspace_path_gets_no_location(state_home, workspace_dir):
    """`relativize` falls back to the BASENAME for a path outside the root --
    fine for a display label, wrong for an address: `/elsewhere/test_foo.py`
    came back as `test_foo.py`, which reads as repo-relative and resolves to
    nothing. A test runner's traceback carries absolute paths and its files
    are often outside the workspace, so the extractors hit this constantly.
    No location beats a location that lies."""
    outside = UNITTEST_OUTPUT.replace("{TESTFILE}", "/elsewhere/test_foo.py")
    _, graph = _extract(outside, workspace_dir)
    assert graph.items[0].location is None
    assert graph.items[0].id == "test_foo.MyTest.test_bad", "still a real finding"


def test_rel_if_inside_never_fabricates(state_home, workspace_dir):
    from conftest import make_ws

    ws = make_ws(workspace_dir)
    assert ws.rel_if_inside(str(workspace_dir / "a" / "b.py")) == "a/b.py"
    assert ws.rel_if_inside("/elsewhere/b.py") is None
    # the display helpers keep their forgiving behaviour on purpose
    assert ws.relativize_as_asked("/elsewhere/b.py") == "b.py"
