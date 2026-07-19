"""Acceptance: coverage-corpus wave — cargotest/v1 and table/v1 profiles
(evals/coverage-corpus-2026-07-19.md; SPEC §9 tabular + Cargo rows)."""

import textwrap

CARGO_FAIL = textwrap.dedent("""\
    running 6 tests
    test tests::add_negative ... ok
    test tests::add_works ... ok
    test tests::add_overflow_sat ... FAILED
    test tests::div_by_zero_guarded ... FAILED
    test tests::div_works ... ok
    test tests::div_rounds ... FAILED

    failures:

    ---- tests::add_overflow_sat stdout ----

    thread 'tests::add_overflow_sat' (8559) panicked at src/lib.rs:1:37:
    attempt to add with overflow
    note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


    failures:
        tests::add_overflow_sat
        tests::div_by_zero_guarded
        tests::div_rounds

    test result: FAILED. 3 passed; 3 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.17s
    """)

CARGO_PASS = textwrap.dedent("""\
    running 2 tests
    test a ... ok
    test b ... ok

    test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s

    running 1 test
    test doc ... ok

    test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.02s
    """)

CARGO_COMPILE_ERROR = textwrap.dedent("""\
    Compiling covbench v0.1.0
    error[E0308]: mismatched types
     --> src/lib.rs:2:5
    error: could not compile `covbench` (lib) due to 1 previous error
    """)


def _table(rows: int) -> str:
    lines = ["NAME                         READY   STATUS             RESTARTS   AGE"]
    for i in range(rows):
        status = "CrashLoopBackOff" if i in (3, 11) else "Running"
        ready = "0/1" if i in (3, 11) else "1/1"
        lines.append(
            f"payments-worker-{i:04}-aaaaa   {ready}     {status:<18} 0          {i}d"
        )
    return "\n".join(lines) + "\n"


def _ctx_for(tmp_path, text, argv=("tool",), exit_code=0):
    from ctx.digest.base import DigestContext, StreamView
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(tmp_path))
    out = StreamView("stdout", len(text.encode()), len(text.splitlines()), "text/plain", text, True)
    err = StreamView("stderr", 0, 0, "text/plain", "", True)
    manifest = {
        "argv": list(argv), "cwd": ".", "shell": False,
        "result": {"exitCode": exit_code, "signal": None, "timedOut": False},
        "streams": {"stdout": {"blob": "sha256:x"}, "stderr": {"blob": "sha256:y"}},
    }
    return DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)


def test_cargotest_failing_census(tmp_path):
    from ctx.digest.moreprofs import CargoTestProfile

    p = CargoTestProfile()
    ctx = _ctx_for(tmp_path, CARGO_FAIL, argv=("cargo", "test"), exit_code=101)
    assert p.detect(ctx) == "argv is cargo with libtest result lines"
    body = p.render(ctx)
    assert "tests (exact): passed 3 · failed 3 · ignored 0" in body
    assert "failing: tests::add_overflow_sat · stdout:L4" in body
    assert "failing: tests::div_rounds · stdout:L7" in body
    assert "first panic stdout:L13: src/lib.rs:1:37: attempt to add with overflow" in body


def test_cargotest_pass_aggregates_suites(tmp_path):
    from ctx.digest.moreprofs import CargoTestProfile

    p = CargoTestProfile()
    ctx = _ctx_for(tmp_path, CARGO_PASS, argv=("cargo", "test"))
    assert p.detect(ctx)
    body = p.render(ctx)
    assert "passed 3 · failed 0" in body and "suites ok 2" in body
    assert "failing:" not in body


def test_cargotest_declines_compile_error(tmp_path):
    """A cargo test run that dies in the compiler has no libtest shape —
    it must fall through to the lint/build profiles."""
    from ctx.digest.moreprofs import CargoTestProfile

    ctx = _ctx_for(tmp_path, CARGO_COMPILE_ERROR, argv=("cargo", "test"), exit_code=101)
    assert CargoTestProfile().detect(ctx) is None


def test_table_census_and_minority_evidence(tmp_path):
    from ctx.digest.tableprof import TableProfile

    p = TableProfile()
    ctx = _ctx_for(tmp_path, _table(60), argv=("kubectl", "get", "pods"))
    assert "aligned table" in (p.detect(ctx) or "")
    body = p.render(ctx)
    assert "table (exact): 60 rows × 5 columns" in body
    assert "columns: NAME · READY · STATUS · RESTARTS · AGE" in body
    assert "STATUS (exact): Running 58 · CrashLoopBackOff 2" in body
    # The quiet needle: minority row surfaced verbatim with coordinates.
    # (READY's 0/1 and STATUS's CrashLoopBackOff share L5; evidence lines
    # dedup by coordinate, so one entry carries both.)
    assert "first 0/1 stdout:L5:" in body and "CrashLoopBackOff" in body
    assert "rows at stdout:L2-L61" in body


def test_table_declines_small_and_prose(tmp_path):
    from ctx.digest.tableprof import TableProfile

    p = TableProfile()
    assert p.detect(_ctx_for(tmp_path, _table(8))) is None  # inline regime
    prose = "\n".join(["This is a paragraph of ordinary prose text."] * 40)
    assert p.detect(_ctx_for(tmp_path, prose)) is None
    # pip-list style two-column Title-case header: deliberately not claimed.
    piplist = "Package            Version\n------------------ -------\n" + "\n".join(
        f"pkg{i}               1.0.{i}" for i in range(30)
    )
    assert p.detect(_ctx_for(tmp_path, piplist)) is None


def test_registry_order_lint_still_wins_diagnostics(tmp_path):
    """table/v1 sits after lint/search/build: a diagnostics flood that happens
    to align must keep its lint census."""
    from ctx.digest import detect_profile

    tsc = "\n".join(
        f"main.ts({i},7): error TS2322: Type 'string' is not assignable."
        for i in range(1, 30)
    )
    profile, _ = detect_profile(_ctx_for(tmp_path, tsc, argv=("tsc",), exit_code=1))
    assert profile.version != "table/v1"


UNITTEST_FAIL = textwrap.dedent("""\
    FF..
    ======================================================================
    FAIL: test_scrypt (auth_tests.test_hashers.TestUtilsHashPwd)
    ----------------------------------------------------------------------
    Traceback (most recent call last):
      File "/x/tests/auth_tests/test_hashers.py", line 508, in test_scrypt
        self.check(encoded)
      File "/x/django/contrib/auth/checks.py", line 92, in check
        raise self.failureException(msg)
    AssertionError: False is not true
    ======================================================================
    FAIL: test_rounds (auth_tests.test_hashers.TestUtilsHashPwd)
    ----------------------------------------------------------------------
    Traceback (most recent call last):
      File "/x/tests/auth_tests/test_hashers.py", line 511, in test_rounds
        self.assertEqual(rounds, 12)
    AssertionError: 10 != 12
    ----------------------------------------------------------------------
    Ran 4 tests in 0.412s

    FAILED (failures=2)
    """)


def test_unittest_profile_census_and_innermost_frame(tmp_path):
    """SWE-bench mine receipt (django-13568): census alone dropped the
    gold file carried by the traceback; the innermost frame is decisive
    evidence and must ride the digest."""
    from ctx.digest.moreprofs import UnittestProfile

    p = UnittestProfile()
    ctx = _ctx_for(tmp_path, UNITTEST_FAIL, argv=("python", "tests/runtests.py"), exit_code=1)
    assert p.detect(ctx)
    body = p.render(ctx)
    assert "tests (exact): ran 4 · failures 2 · errors 0" in body
    assert "fail: auth_tests.test_hashers.TestUtilsHashPwd.test_scrypt · stdout:L3" in body
    assert "fail: auth_tests.test_hashers.TestUtilsHashPwd.test_rounds · stdout:L12" in body
    assert 'innermost frame stdout:L8: File "/x/django/contrib/auth/checks.py"' in body
    assert "first failure stdout:L10: AssertionError: False is not true" in body


def test_unittest_profile_declines_pass_free_prose(tmp_path):
    from ctx.digest.moreprofs import UnittestProfile

    assert UnittestProfile().detect(_ctx_for(tmp_path, "Ran a marathon in 4 hours\n")) is None


GO_FAIL_WITH_FRAMES = textwrap.dedent("""\
    --- FAIL: TestMethodNotAllowedNoRoute (0.00s)
        gin_integration_test.go:44: assertion failed
        	Error Trace:	/w/gin_integration_test.go:44
        	            	/root/go/pkg/mod/github.com/stretchr/testify@v1.9.0/assert/assertions.go:56
        	            	/usr/local/go1.24.7/src/testing/testing.go:1734
        	            	/w/gin.go:693
    FAIL
    FAIL	github.com/gin-gonic/gin	0.028s
    """)


def test_gotest_v2_census_and_implicated_frame(tmp_path):
    """SWE-bench multilingual receipt (gin-4003): the implicated frame must
    skip test files, module-cache deps, and GOROOT scaffolding to land on
    the product file the gold patch touches."""
    from ctx.digest.moreprofs import GoTestProfile

    p = GoTestProfile()
    ctx = _ctx_for(tmp_path, GO_FAIL_WITH_FRAMES, argv=("go", "test"), exit_code=1)
    assert p.detect(ctx)
    body = p.render(ctx)
    assert "failing: TestMethodNotAllowedNoRoute · stdout:L1" in body
    assert "implicated frame stdout:L6: gin.go:693" in body
    assert "assertions.go" not in body and "testing.go" not in body
