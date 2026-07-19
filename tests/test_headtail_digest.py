"""Acceptance: HEAD/TAIL evidence window in the text/v1 digest profile.

Motivation (evals/eval-collapse-2026-07-18.md, S-C): CLIs put their
conclusions at the END of output (test summaries, exit reports). A flood
digest that shows only "head stdout:L1" omits the very evidence the run
existed to produce. The window shows the first H and last T lines with real
line numbers; the middle is declared-omitted with a span + --lines address.
"""

import re
import subprocess
import sys

import pytest

# 2,000 flood lines plus the conclusion on the final line (L2001) — the
# S-C shape: too large to inline, no error signals, lands in text/v1.
FLOOD_SCRIPT = (
    "for i in range(2000):\n"
    "    print(f'frame {i:05d} ok')\n"
    "print('SUMMARY frames=2000 anomalies=0')\n"
)


@pytest.fixture()
def make_ws_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    counter = iter(range(100))

    def make(toml: str = "version = 1\n"):
        from ctx.store import Store
        from ctx.workspace import resolve_workspace

        d = tmp_path / f"proj{next(counter)}"
        d.mkdir()
        (d / "ctx.toml").write_text(toml, encoding="utf-8")
        subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
        ws = resolve_workspace(str(d))
        return ws, Store(ws.workspace_id)

    return make


def _flood_digest(ws, store):
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    (ws.root / "gen.py").write_text(FLOOD_SCRIPT, encoding="utf-8")
    cap = run_capture(ws, [sys.executable, "gen.py"], store=store)
    return render_run_digest(store, ws, cap.manifest)


def _coverage_numbers(digest: str) -> tuple[int, int, int]:
    """(parsed, shown, omitted) from the coverage section."""
    parsed = re.search(r"parsed: ([\d,]+)/[\d,]+ lines", digest)
    shown = re.search(r"shown: (\d+) spans · omitted: ([\d,]+) lines", digest)
    assert parsed and shown, digest
    return (
        int(parsed.group(1).replace(",", "")),
        int(shown.group(1)),
        int(shown.group(2).replace(",", "")),
    )


# ------------------------------------------------------------ (a) window
def test_flood_shows_head_and_tail_with_real_line_numbers(make_ws_store):
    ws, store = make_ws_store()
    digest, m = _flood_digest(ws, store)
    assert m["digest"]["profile"] == "text/v1"
    # Head: first 5 lines, labeled with their real coordinates.
    assert "head stdout:L1: frame 00000 ok" in digest
    assert "head stdout:L5: frame 00004 ok" in digest
    # Tail: last 5 lines — the conclusion line survives with its coordinate.
    assert "tail stdout:L1997: frame 01996 ok" in digest
    assert "tail stdout:L2001: SUMMARY frames=2000 anomalies=0" in digest
    # Middle omission is declared, span-addressed, and retrievable.
    assert "omitted stdout:L6-L1996 (1,991 lines)" in digest
    assert "--lines 6:1996" in digest
    sid = re.search(r"· span ([0-9a-f]{10})", digest)
    assert sid, digest
    span = store.get_span(sid.group(1))
    assert (span["kind"], span["a"], span["b"]) == ("region", 6, 1996)


# -------------------------------------------------------- (b) arithmetic
def test_omitted_count_arithmetic_exact(make_ws_store):
    ws, store = make_ws_store()
    digest, _ = _flood_digest(ws, store)
    parsed, shown, omitted = _coverage_numbers(digest)
    assert shown == 10  # 5 head + 5 tail
    assert parsed == 2001
    assert parsed == shown + omitted


# ---------------------------------------------------- (c) config override
def test_config_override_head_and_tail_lines(make_ws_store):
    ws, store = make_ws_store(
        "version = 1\n[budgets]\ndigest_head_lines = 2\ndigest_tail_lines = 3\n"
    )
    digest, _ = _flood_digest(ws, store)
    assert "head stdout:L2: frame 00001 ok" in digest
    assert "head stdout:L3:" not in digest
    assert "tail stdout:L1999: frame 01998 ok" in digest
    assert "tail stdout:L1998:" not in digest
    assert "tail stdout:L2001: SUMMARY frames=2000 anomalies=0" in digest
    assert "omitted stdout:L3-L1998 (1,996 lines)" in digest
    parsed, shown, omitted = _coverage_numbers(digest)
    assert shown == 5 and parsed == shown + omitted


# ------------------------------------------------------- (d) determinism
def test_flood_digest_byte_identical_across_replays(make_ws_store):
    ws, store = make_ws_store()
    # Warm-up: the first flood flips the (workspace-local, idempotent)
    # engagement state to active, which participates in the next capture's
    # worktree hash. From then on identical bytes → byte-identical digests.
    _flood_digest(ws, store)
    d1, m1 = _flood_digest(ws, store)
    d2, m2 = _flood_digest(ws, store)
    assert d1 == d2
    assert m1["digest"]["bytesHash"] == m2["digest"]["bytesHash"]


# ------------------------------------------- (e) small output unchanged
def test_small_output_inline_path_has_no_window_scaffolding(make_ws_store):
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    ws, store = make_ws_store()
    cap = run_capture(ws, [sys.executable, "-c", "print('tiny ok')"], store=store)
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "text/v1"
    body = digest.splitlines()
    # Slim complete-inline shape, byte-compatible with the pre-window layout.
    assert body[1] == f"command: {sys.executable} -c print('tiny ok')"
    assert body[2] == "exit 0 · output (complete):"
    assert body[3] == "tiny ok"
    for marker in ("head stdout:", "tail stdout:", "omitted", "summary:", "coverage:"):
        assert marker not in digest, marker


# ------------------------------------------------- budget: tail shrinks first
def test_window_respects_digest_budget_shrinking_tail_first(make_ws_store):
    ws, store = make_ws_store(
        "version = 1\n[budgets]\n"
        "digest_tokens = 150\ndigest_head_lines = 40\ndigest_tail_lines = 40\n"
    )
    digest, _ = _flood_digest(ws, store)
    heads = re.findall(r"^  head stdout:", digest, re.MULTILINE)
    tails = re.findall(r"^  tail stdout:", digest, re.MULTILINE)
    assert len(heads) >= 1
    assert len(tails) < 40 and len(heads) < 40  # the window bent to the budget
    assert len(heads) >= len(tails)  # tail shrinks before head
    # Body honors the budget; small slack covers the [ctx run:...] header
    # line and the run:PENDING -> run:<short> id expansion added afterward.
    assert len(digest.encode("utf-8")) <= 150 * 4 + 120
    # The omission stays declared even under pressure.
    assert "omitted stdout:L" in digest
    parsed, shown, omitted = _coverage_numbers(digest)
    assert parsed == shown + omitted
