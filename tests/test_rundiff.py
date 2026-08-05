"""Acceptance for `ctx diff run:A run:B` (ROADMAP M-D): failure deltas with
resolvable spans, template deltas, no-delta accounting, determinism, budget."""

import re
import sys

import pytest

from conftest import make_store, make_ws

PASSING = """\
========================= test session starts ==========================
collected 2 items

tests/test_app.py ..                                             [100%]

========================== 2 passed in 0.01s ===========================
"""

FAILING = """\
========================= test session starts ==========================
collected 2 items

tests/test_app.py .F                                             [100%]

=============================== FAILURES ===============================
______________________________ test_beta _______________________________
    def test_beta():
>       assert compute() == 2
E       AssertionError: assert 3 == 2

tests/test_app.py:7: AssertionError
======================= short test summary info ========================
FAILED tests/test_app.py::test_beta - AssertionError: assert 3 == 2
===================== 1 failed, 1 passed in 0.02s ======================
"""


def _capture_text(tmp_path, ws, store, name, text, code=0):
    from ctx.execution import run_capture

    payload = tmp_path / name
    payload.write_text(text, encoding="utf-8")
    script = "import sys;sys.stdout.write(open(sys.argv[1]).read());sys.exit(int(sys.argv[2]))"
    return run_capture(
        ws, [sys.executable, "-c", script, str(payload), str(code)], store=store
    )


def _span_ids(out: str) -> list[str]:
    return re.findall(r"span ([0-9a-f]{10})\b", out)


def test_new_failure_surfaces_with_resolvable_span(state_home, workspace_dir, tmp_path):
    from ctx.retrieval import Selector, get
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    a = _capture_text(tmp_path, ws, store, "a.txt", PASSING, code=0)
    b = _capture_text(tmp_path, ws, store, "b.txt", FAILING, code=1)

    out = run_diff(store, ws, f"run:{a.manifest_id[:12]}", f"run:{b.manifest_id[:12]}")
    assert "new failures: 1" in out
    assert "tests/test_app.py::test_beta" in out
    assert "exit 0 → 1" in out
    # Coordinate points at the traceback block header in B.
    m = re.search(r"test_beta · B stdout:L(\d+)-L\d+ · span ([0-9a-f]{10})", out)
    assert m, out
    sid = m.group(2)
    resolved = get(store, ws, f"run:{b.manifest_id[:12]}#stdout", Selector(span=sid))
    assert "AssertionError" in resolved
    # next: teaches retrieval of the most salient new-in-B evidence.
    assert f"--span {sid}" in out


def test_resolved_failures_listed(state_home, workspace_dir, tmp_path):
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    a = _capture_text(tmp_path, ws, store, "a.txt", FAILING, code=1)
    b = _capture_text(tmp_path, ws, store, "b.txt", PASSING, code=0)

    out = run_diff(store, ws, f"run:{a.manifest_id[:12]}", f"run:{b.manifest_id[:12]}")
    assert "resolved: 1" in out
    assert "tests/test_app.py::test_beta" in out
    assert "new failures" not in out


def test_identical_runs_no_behavioral_delta(state_home, workspace_dir, tmp_path):
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_text(tmp_path, ws, store, "a.txt", PASSING, code=0)
    ref = f"run:{cap.manifest_id[:12]}"

    out = run_diff(store, ws, ref, ref)
    assert "no behavioral delta" in out
    assert "command: identical · cwd: identical" in out
    assert "lines ·" in out  # size accounting still present


def test_log_template_only_in_b_with_span(state_home, workspace_dir, tmp_path):
    from ctx.retrieval import Selector, get
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    base = "\n".join(
        f"2026-07-17 10:00:{i % 60:02d} INFO worker {i} heartbeat ok" for i in range(500)
    )
    extra = "\n".join(
        f"2026-07-17 10:09:{i:02d} ERROR disk {i} full on node {i}" for i in range(20)
    )
    a = _capture_text(tmp_path, ws, store, "a.log", base + "\n", code=0)
    b = _capture_text(tmp_path, ws, store, "b.log", base + "\n" + extra + "\n", code=0)

    out = run_diff(store, ws, f"run:{a.manifest_id[:12]}", f"run:{b.manifest_id[:12]}")
    assert "only in B: 1" in out
    assert "ERROR disk <*> full on node <*>" in out
    assert "20× L501:" in out
    sids = _span_ids(out)
    assert sids
    resolved = get(store, ws, f"run:{b.manifest_id[:12]}#stdout", Selector(span=sids[0]))
    assert "ERROR disk 0 full on node 0" in resolved


def test_determinism_byte_identical(state_home, workspace_dir, tmp_path):
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    a = _capture_text(tmp_path, ws, store, "a.txt", PASSING, code=0)
    b = _capture_text(tmp_path, ws, store, "b.txt", FAILING, code=1)
    ra, rb = f"run:{a.manifest_id[:12]}", f"run:{b.manifest_id[:12]}"

    assert run_diff(store, ws, ra, rb) == run_diff(store, ws, ra, rb)


def test_output_within_result_budget(state_home, workspace_dir, tmp_path):
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    noisy_a = "\n".join(f"line kind-{i} value {i * 7}" for i in range(3000))
    noisy_b = "\n".join(f"other kind-{i} payload {i * 3}" for i in range(3000))
    a = _capture_text(tmp_path, ws, store, "a.txt", noisy_a, code=0)
    b = _capture_text(tmp_path, ws, store, "b.txt", noisy_b, code=0)

    out = run_diff(store, ws, f"run:{a.manifest_id[:12]}", f"run:{b.manifest_id[:12]}")
    # bounded() enforces budget_tokens * 4 bytes plus a short truncation note.
    limit = ws.config.budgets.result_tokens * 4 + 200
    assert len(out.encode("utf-8")) <= limit


def test_unknown_ref_error_actionable(state_home, workspace_dir):
    from ctx.retrieval import RetrievalError
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    with pytest.raises(RetrievalError) as exc:
        run_diff(store, ws, "run:deadbeefcafe", "run:deadbeefcafe")
    msg = str(exc.value)
    assert "run:deadbeefcafe" in msg
    assert "ctx run" in msg  # tells the caller how to produce a run ref

    with pytest.raises(RetrievalError) as exc2:
        run_diff(store, ws, "repo:src", "run:deadbeefcafe")
    assert "run:" in str(exc2.value)


def test_cli_diff_wired(state_home, workspace_dir, tmp_path, capsys):
    from ctx.cli import main as cli_main
    from ctx.rundiff import run_diff  # noqa: F401 - import parity with lazy wiring

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    a = _capture_text(tmp_path, ws, store, "a.txt", PASSING, code=0)
    b = _capture_text(tmp_path, ws, store, "b.txt", FAILING, code=1)
    rc = cli_main(
        ["--workspace", str(workspace_dir), "diff",
         f"run:{a.manifest_id[:12]}", f"run:{b.manifest_id[:12]}"]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "[ctx diff run:" in captured.out
    assert "new failures: 1" in captured.out


def test_removed_dead_code_stays_gone(state_home):
    import ctx.execution
    import ctx.store

    assert not hasattr(ctx.execution, "verify_manifest_shape")
    assert not hasattr(ctx.execution, "manifest_short_id")
    assert not hasattr(ctx.store, "StoredObject")
    assert not hasattr(ctx.store.Store, "kind_of")


# --------------------------------------------- a claim's span is its own
TWO_FAILURES = """\
========================= test session starts ==========================
collected 3 items

tests/test_app.py .FF                                            [100%]

=============================== FAILURES ===============================
______________________________ test_beta _______________________________
    def test_beta():
>       assert compute() == 2
E       AssertionError: BETA-MARKER

tests/test_app.py:7: AssertionError
______________________________ test_gamma ______________________________
    def test_gamma():
>       assert other() == 9
E       AssertionError: GAMMA-MARKER

tests/test_app.py:12: AssertionError
======================= short test summary info ========================
FAILED tests/test_app.py::test_beta - AssertionError: BETA-MARKER
FAILED tests/test_app.py::test_gamma - AssertionError: GAMMA-MARKER
===================== 2 failed, 1 passed in 0.02s ======================
"""


def test_span_stops_at_the_next_failure(state_home, workspace_dir, tmp_path):
    """A span minted for nodeid X must not carry nodeid Y's traceback.

    The window was a flat `start + 12` lines, so on adjacent failures the
    evidence resolved for test_beta ran straight through test_gamma's block
    and attributed its assertion to the wrong test.
    """
    from ctx.retrieval import Selector, get
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    a = _capture_text(tmp_path, ws, store, "a.txt", PASSING, code=0)
    b = _capture_text(tmp_path, ws, store, "b.txt", TWO_FAILURES, code=1)

    out = run_diff(store, ws, f"run:{a.manifest_id[:12]}", f"run:{b.manifest_id[:12]}")
    assert "new failures: 2" in out
    m = re.search(r"test_beta · B stdout:L\d+-L\d+ · span ([0-9a-f]{10})", out)
    assert m, out
    resolved = get(store, ws, f"run:{b.manifest_id[:12]}#stdout", Selector(span=m.group(1)))
    assert "BETA-MARKER" in resolved, "own evidence still resolves"
    assert "GAMMA-MARKER" not in resolved, "span bled into the next failure's block"


def test_span_end_never_precedes_its_start():
    """Two anchors on consecutive lines collapse the window to one line --
    never to an inverted range, which is what a bare `next - 1` would give."""
    from ctx.rundiff import _span_end

    assert _span_end(10, [10, 11], 100) == 10
    assert _span_end(10, [10], 100) == 22          # no next anchor: full window
    assert _span_end(10, [10, 40], 100) == 22      # next anchor is further out
    assert _span_end(95, [95], 100) == 100         # clamped to the stream


# --------------------------------------- "skipped" has to mean skipped
def test_binary_side_skips_the_delta_rather_than_faking_one(
    state_home, workspace_dir, tmp_path
):
    """A binary side is unknown, not empty.

    Substituting "" for it and continuing made every signature and template on
    the readable side report as `only in B` -- a delta manufactured out of the
    absence of a comparison, printed directly under a line saying the analysis
    had been skipped.
    """
    import sys as _sys

    from ctx.execution import run_capture
    from ctx.rundiff import run_diff

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = "import sys;sys.stdout.buffer.write(bytes(range(256))*40)"
    a = run_capture(ws, [_sys.executable, "-c", script], store=store)
    b = _capture_text(tmp_path, ws, store, "b.txt", FAILING, code=1)

    out = run_diff(store, ws, f"run:{a.manifest_id[:12]}", f"run:{b.manifest_id[:12]}")
    assert "binary stdout in A" in out
    assert "skipped" in out
    assert "new failures" not in out, "a delta against an unknown side is not a delta"
    assert "only in B" not in out
    assert "streams:" in out, "size accounting is still honest and still reported"
