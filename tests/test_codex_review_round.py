"""Four defects found by an automated reviewer on PR #21, each closed as a
mechanism.

The bug bash that produced this branch ran agents against the tree; this round
came from a static reviewer reading the diff, and it found four things sixteen
agent rounds did not. Three of them are instances of classes the bash had
already named -- which is the useful part: naming a class does not close it,
and only an invariant that fails on the NEXT instance does.

* `--records` was the third door onto the continuation contract (`--bytes`
  round 12, `--lines` round 16). Closed by one fitter for every item window.
* The rewrite guard hand-rolled a stat basis weaker than the shared one.
  Closed by making it the fourth caller of `stat_fingerprint`.
* `ctx seq` latched its exit code and its timeout flag in two statements with
  two different rules. Closed by latching them in one.
* The fact cache keyed a derived census on a manifest id that cannot move when
  the extractor set changes. Closed by deriving the epoch from the registry.
"""

from __future__ import annotations

import hashlib

import pytest

from ctx._retrieval.get import Selector, _fit_window, get


class _Budget:
    """Just the fields `_fit_window` reads."""

    def __init__(self, result_tokens: int) -> None:
        self.result_tokens = result_tokens
        self.max_inline_lines = 10_000
        self.max_inline_bytes = 1 << 20


# --------------------------------------------------------------- continuation
#: Every item-window selector `_fit_window` serves. A new one belongs here the
#: day it is added -- the test below asserts the property that makes the
#: continuation usable at all, so a selector missing from this list is a
#: selector nobody proved can advance.
ITEM_WINDOW_FLAGS = ["--lines", "--records"]


@pytest.mark.parametrize("flag", ITEM_WINDOW_FLAGS)
def test_fit_window_continuation_strictly_advances(flag):
    """A trimmed window must address something AFTER what it showed.

    The defect: `--records 1:100` on 100 wide records exceeded the result
    budget with no selector-level continuation, so the emitted `next:` was the
    verbatim handle -- the identical range, returning the identical truncated
    prefix, forever. A continuation that does not advance is not a
    continuation, it is a loop.
    """
    total = 100
    rendered = [f"{'x' * 400} item {i}" for i in range(1, total + 1)]
    new_b, nxt = _fit_window(flag, "run:abc#stdout", 1, total, total, rendered, _Budget(400))

    assert new_b < total, "an over-budget window must be trimmed by the selector"
    assert nxt is not None, "a trimmed window must carry a forward address"
    assert f"{flag} {new_b + 1}:" in nxt, (
        f"continuation must start after the last item shown ({new_b}); got {nxt!r}"
    )


@pytest.mark.parametrize("flag", ITEM_WINDOW_FLAGS)
def test_fit_window_leaves_a_fitting_window_alone(flag):
    """Small content passes through with no continuation and no trim."""
    rendered = [f"item {i}" for i in range(1, 6)]
    new_b, nxt = _fit_window(flag, "run:abc#stdout", 1, 5, 5, rendered, _Budget(100_000))
    assert (new_b, nxt) == (5, None)


def test_records_over_budget_advances_end_to_end(workspace_dir, state_home):
    """The whole `ctx get --records` path, not just the helper.

    Guards the wiring: the helper can be correct while the caller keeps its
    own pre-fit header and its own `b < len(lines)` continuation, which is
    exactly the state `--lines` was in before round 16.
    """
    from conftest import make_store, make_ws

    (workspace_dir / "ctx.toml").write_text(
        "version = 1\n[budgets]\nresult_tokens = 400\n", encoding="utf-8"
    )
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)

    blob = "\n".join(f"{'r' * 300} record {i}" for i in range(1, 101))
    ref = f"blob:{store.put_blob(blob.encode())}"

    out = get(store, ws, ref, Selector(records=(1, 100)))

    # The header must describe the body that shipped, and the address must
    # move past it.
    header = [ln for ln in out.splitlines() if ln.startswith("selector:")][0]
    shown_end = int(header.split("--records ")[1].split(" of ")[0].split(":")[1])
    assert shown_end < 100, f"body was not trimmed by the selector: {header}"
    nxt = [ln for ln in out.splitlines() if ln.startswith("next:")]
    if nxt:
        assert f"--records {shown_end + 1}:" in nxt[0], (
            f"continuation must advance past record {shown_end}; got {nxt[0]!r}"
        )


# ---------------------------------------------------------------- guard basis
def test_rewrite_guard_sees_a_restored_mtime(workspace_dir, monkeypatch):
    """A same-size edit with mtime put back must still invalidate the guard.

    `os.utime`, `rsync -t`, `tar -p` and editors that save-and-restore
    timestamps all produce this. The guard folded `(rel, size, mtime_ns)` and
    therefore could not see it, while the performance caches beside it were
    already folding ctime for exactly this reason -- a safety guard reading a
    weaker basis than a cache is the wrong way round.
    """
    import os

    from conftest import make_ws

    from ctx.astgrep import _guard_state

    src = workspace_dir / "a.py"
    src.write_text("value = 1\n")
    st = src.stat()

    ws = make_ws(workspace_dir)
    # Force the non-git fallback: with a real git root the generation hash
    # answers first and this basis is never consulted.
    monkeypatch.setattr("ctx.execution.generation_hash", lambda _root: None)

    before = _guard_state(ws)
    src.write_text("value = 2\n")  # same length, different content
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))  # hide the write

    assert src.stat().st_size == st.st_size and src.stat().st_mtime_ns == st.st_mtime_ns
    assert _guard_state(ws) != before, (
        "guard accepted a worktree that changed under it: size+mtime alone "
        "cannot see a same-size edit whose mtime was restored"
    )


def test_stat_fingerprint_records_missing_paths():
    """A vanished file must not hash like a workspace that never had it."""
    from ctx.workspace import stat_fingerprint

    a, b = hashlib.sha256(), hashlib.sha256()
    stat_fingerprint("/nonexistent-root", ["gone.py"], a)
    stat_fingerprint("/nonexistent-root", [], b)
    assert a.hexdigest() != b.hexdigest()


# ------------------------------------------------------------------ seq latch
def test_seq_keep_going_reports_the_first_failure_not_a_later_timeout():
    """Exit code and digest must name the same failure.

    Under `--keep-going` the exit code was first-failure-wins while the
    timeout flag was any-failure-wins, so an early exit-3 followed by a later
    timeout rendered step 1's digest and returned 124 -- the summary and the
    exit status disagreeing about which failure was primary. Two values
    derived from the same event, latched in two statements, under two rules.
    """
    from ctx.seq import run_seq

    seen = []

    class _FakeCapture:
        def __init__(self, manifest):
            self.manifest = manifest

    def _manifest(idx, exit_code, timed_out):
        return {
            "id": f"sha256:{idx:064x}",
            "result": {"exitCode": exit_code, "timedOut": timed_out, "signal": None},
            "streams": {},
        }

    plan = [(3, False), (0, False), (None, True)]  # fail, pass, timeout

    def fake_run_capture(ws, cmds, **kw):
        idx = len(seen)
        seen.append(cmds)
        code, to = plan[idx]
        return _FakeCapture(_manifest(idx + 1, code, to))

    def fake_render(store, ws, manifest, focus=None):
        return f"digest for {manifest['id'][:12]}", manifest

    import ctx.digest
    import ctx.execution

    orig_cap, orig_render = ctx.execution.run_capture, ctx.digest.render_run_digest
    ctx.execution.run_capture = fake_run_capture
    ctx.digest.render_run_digest = fake_render
    try:
        _text, code, timed_out = run_seq(
            None, None, ["a", "b", "c"], halt_on_fail=False
        )
    finally:
        ctx.execution.run_capture = orig_cap
        ctx.digest.render_run_digest = orig_render

    assert code == 3, "the first failure's exit code must win"
    assert timed_out is False, (
        "a LATER step's timeout must not claim to be the primary outcome: "
        "the digest rendered is step 1's, and `ctx seq` would return 124"
    )


# ---------------------------------------------------------------- fact epoch
def test_extractor_epoch_moves_when_a_profile_learns_to_extract():
    """The fact cache's epoch must be derived, not remembered.

    A store that had already derived a unittest / Go / Cargo / Jest run kept
    returning the old pytest-only census after those profiles learned to
    extract, because the cached fingerprint was the manifest id and a manifest
    id does not move when this harness gains an extractor.
    """
    import ctx.digest as D
    from ctx.digest.base import Profile

    base = D.extractor_epoch()
    assert base == D.extractor_epoch(), "epoch must be stable within a build"

    class _NewRunner(Profile):
        version = "brandnew/v1"

        def detect(self, ctx):  # pragma: no cover - never probed here
            return None

        def extract(self, ctx):  # pragma: no cover - never called here
            return None

    D.extractor_epoch.cache_clear()
    orig = D._PROFILES
    D._PROFILES = orig + (_NewRunner(),)
    try:
        assert D.extractor_epoch() != base, (
            "adding a profile that can extract must invalidate derived run "
            "facts; otherwise every already-derived run keeps its stale census"
        )
    finally:
        D._PROFILES = orig
        D.extractor_epoch.cache_clear()


def test_an_already_derived_run_is_re_derived_when_the_epoch_moves(
    state_home, workspace_dir
):
    """The contract itself: a new extractor must reach OLD runs.

    Asserted behaviourally rather than by reading the source. The reported
    defect is not "the key lacks a field", it is "a store that derived this
    run yesterday keeps answering from yesterday's extractor" -- so the test
    derives a run, moves the epoch the way shipping a new extractor would,
    and demands the second call actually re-derive instead of taking the
    cache shortcut.
    """
    import sys

    from conftest import make_store, make_ws

    import ctx.digest as D
    from ctx.digest.base import Profile
    from ctx.execution import run_capture
    from ctx.facts import derive_run

    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    cap = run_capture(
        ws,
        [sys.executable, "-c", "print('Ran 1 test in 0.0s'); print('OK')"],
        store=store,
    )

    first = derive_run(store, ws, cap.manifest)
    assert not first.get("skipped"), "the first derivation cannot be a cache hit"

    # Same store, same manifest: this is the shortcut working as intended.
    assert derive_run(store, ws, cap.manifest).get("skipped") is True

    # Now ship an extractor. The manifest is untouched -- that is the whole
    # point -- so only a derived epoch can invalidate the entry.
    class _NewRunner(Profile):
        version = "brandnew/v1"

        def detect(self, ctx):  # pragma: no cover - never probed here
            return None

        def extract(self, ctx):  # pragma: no cover - never called here
            return None

    orig = D._PROFILES
    D._PROFILES = orig + (_NewRunner(),)
    D.extractor_epoch.cache_clear()
    try:
        again = derive_run(store, ws, cap.manifest)
        assert not again.get("skipped"), (
            "a run derived before the extractor shipped kept serving its old "
            "census: the cache key could not see that extraction changed"
        )
    finally:
        D._PROFILES = orig
        D.extractor_epoch.cache_clear()
