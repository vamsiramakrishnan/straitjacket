"""Acceptance: anticipatory span inlining (mechanism E) — the pytest digest
carries the first failure region inline so the common next question costs
zero retrieval hops, while staying budget-gated and deterministic."""

import textwrap

PYTEST_OUT = textwrap.dedent("""\
    ============================= test session starts ==============================
    collected 3 items

    tests/test_x.py .F.                                                      [100%]

    =================================== FAILURES ===================================
    _______________________________ test_page_bounds _______________________________

        def test_page_bounds():
    >       assert page_bounds(7, 3)[-1] == (6, 7)
    E       assert (6, 9) == (6, 7)

    tests/test_x.py:12: AssertionError
    =========================== short test summary info ============================
    FAILED tests/test_x.py::test_page_bounds - assert (6, 9) == (6, 7)
    ========================= 1 failed, 2 passed in 0.03s ==========================
    """)


def _ctx_for(tmp_path, text, result_tokens=1200):
    from ctx.digest.base import DigestContext, StreamView
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text(
        f"version = 1\n[budgets]\nresult_tokens = {result_tokens}\n",
        encoding="utf-8",
    )
    ws = resolve_workspace(str(tmp_path))
    out = StreamView("stdout", len(text.encode()), len(text.splitlines()), "text/plain", text, True)
    err = StreamView("stderr", 0, 0, "text/plain", "", True)
    manifest = {
        "argv": ["pytest", "-q"],
        "cwd": ".",
        "shell": False,
        "result": {"exitCode": 1, "signal": None, "timedOut": False},
        "streams": {"stdout": {"blob": "sha256:x"}, "stderr": {"blob": "sha256:y"}},
    }
    return DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)


def test_first_failure_region_is_inlined(tmp_path):
    from ctx.digest.pytestprof import PytestProfile

    body = PytestProfile().render(_ctx_for(tmp_path, PYTEST_OUT))
    assert "first failure stdout:L" in body
    # The failure detail is present verbatim, prefixed as inline evidence.
    assert "    | >       assert page_bounds(7, 3)[-1] == (6, 7)" in body
    assert "    | E       assert (6, 9) == (6, 7)" in body


def test_inlining_is_budget_gated(tmp_path):
    from ctx.digest.pytestprof import PytestProfile

    body = PytestProfile().render(_ctx_for(tmp_path, PYTEST_OUT, result_tokens=400))
    assert "first failure stdout:L" in body  # the pointer survives
    assert "    | " not in body  # the inline body does not


def test_inlining_is_deterministic(tmp_path):
    from ctx.digest.pytestprof import PytestProfile

    a = PytestProfile().render(_ctx_for(tmp_path, PYTEST_OUT))
    b = PytestProfile().render(_ctx_for(tmp_path, PYTEST_OUT))
    assert a == b
