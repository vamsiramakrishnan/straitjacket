"""Acceptance: per-session cumulative native-read ledger ("death by a
thousand small reads").

Single reads under max_inline_bytes pass raw, but cumulative bytes are
charged to <workspace>/.ctx-session-reads/<session_id>.count. Past
[budgets] session_read_budget_bytes (default 256 KiB) reads come under
graduated pressure: allow+updatedInput with a small ``limit`` under rewrite
steering, deny-with-remediation under steering="deny". Ledger IO is
fail-open and reads of the ledger dir itself are never counted.
"""

import io
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

DEFAULT_BUDGET = 262144  # 256 KiB
# The over-budget window escalates with the overrun ratio (hook._pressured_window),
# so it is no longer a single constant. These bracket the contract: a pressured
# window is never looser than the old fixed throttle and never below the floor.
PRESSURE_CEILING = 240 // 4  # max_inline_lines default // 4, at ~1x overrun
PRESSURE_FLOOR = 20          # hook._OVER_BUDGET_MIN_LINES


def _classify(tool_name, tool_input, workspace, session_id=None):
    from ctx.hook import classify

    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "workspacePaths": [str(workspace)],
    }
    if session_id is not None:
        payload["session_id"] = session_id
    return classify(payload)


def _read(path, workspace, session_id="sess-fixed"):
    return _classify("Read", {"file_path": str(path)}, workspace, session_id)


def _make_file(workspace, name, nbytes):
    p = workspace / name
    p.write_text("x" * nbytes, encoding="utf-8")
    return p


def _ledger_file(workspace, session_id):
    return workspace / ".ctx-session-reads" / f"{session_id}.count"


# ------------------------------------------------------------ accumulation
def test_small_reads_accumulate_and_flip_past_default_budget(tmp_path):
    """Each read is legal alone; the read that pushes the cumulative total
    past 256 KiB flips to allow+updatedInput with a bounded limit."""
    f = _make_file(tmp_path, "chunk.txt", 15000)  # < 16 KiB inline budget
    n_under = DEFAULT_BUDGET // 15000  # 17 reads = 255000 bytes, still under
    for i in range(n_under):
        d = _read(f, tmp_path)
        assert d == {"decision": "allow"}, f"read {i + 1} must pass raw"
    ledger = _ledger_file(tmp_path, "sess-fixed")
    assert ledger.is_file()
    assert int(ledger.read_text()) == n_under * 15000

    d = _read(f, tmp_path)  # (N+1)th read crosses the budget
    assert d["decision"] == "allow"
    updated = d["rewrite"]["updatedInput"]
    assert updated["file_path"] == str(f)
    assert PRESSURE_FLOOR <= updated["limit"] <= PRESSURE_CEILING
    reason = d["rewrite"]["reason"]
    assert "session native-read budget exceeded" in reason
    assert "KiB raw reads" in reason
    assert "ctx search repo:" in reason
    assert "ctx get repo:" in reason

    # Emitters use their native field names for the same transparent rewrite.
    from ctx.hook import _to_antigravity_schema, _to_claude_code_schema

    wire = _to_antigravity_schema(dict(d))
    assert wire["decision"] == "allow"
    assert "updatedInput" not in wire
    assert PRESSURE_FLOOR <= wire["overwrite"]["limit"] <= PRESSURE_CEILING
    hso = _to_claude_code_schema(dict(d))["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert PRESSURE_FLOOR <= hso["updatedInput"]["limit"] <= PRESSURE_CEILING


def test_over_budget_denies_under_steering_deny(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n'
        "[budgets]\nsession_read_budget_bytes = 1024\n",
        encoding="utf-8",
    )
    f = _make_file(tmp_path, "small.txt", 700)
    assert _read(f, tmp_path)["decision"] == "allow"  # 700 bytes: under budget
    d = _read(f, tmp_path)  # 1400 bytes: over budget
    assert d["decision"] == "deny"
    assert "rewrite" not in d
    assert "session native-read budget exceeded" in d["reason"]
    assert "ctx search repo:" in d["reason"]  # remediation present


# --------------------------------------------------------- session identity
def test_distinct_sessions_have_independent_ledgers(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        "version = 1\n[budgets]\nsession_read_budget_bytes = 1024\n",
        encoding="utf-8",
    )
    f = _make_file(tmp_path, "small.txt", 700)
    assert _read(f, tmp_path, session_id="sess-a") == {"decision": "allow"}
    d = _read(f, tmp_path, session_id="sess-a")
    assert d["decision"] == "allow" and "rewrite" in d  # A over budget
    # B is untouched by A's spending.
    assert _read(f, tmp_path, session_id="sess-b") == {"decision": "allow"}
    assert int(_ledger_file(tmp_path, "sess-a").read_text()) == 1400
    assert int(_ledger_file(tmp_path, "sess-b").read_text()) == 700


def test_conversation_id_fallback_charges_ledger(tmp_path):
    from ctx.hook import classify

    f = _make_file(tmp_path, "small.txt", 300)
    d = classify(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(f)},
            "workspacePaths": [str(tmp_path)],
            "conversation_id": "conv-7",
        }
    )
    assert d == {"decision": "allow"}
    assert int(_ledger_file(tmp_path, "conv-7").read_text()) == 300


# ---------------------------------------------------------------- fail-open
def test_ledger_io_failure_still_emits_one_valid_allow(tmp_path, monkeypatch):
    """A broken ledger must degrade to counting nothing: the hook entry still
    emits exactly one valid allow decision for a small read."""
    from ctx import hook

    f = _make_file(tmp_path, "small.txt", 500)

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    # Shadow builtins.open for code inside the hook module only.
    monkeypatch.setattr(hook, "open", boom, raising=False)

    payload = json.dumps(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(f)},
            "workspacePaths": [str(tmp_path)],
            "session_id": "sess-broken",
        }
    )
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(payload)
    sys.stdout = io.StringIO()
    try:
        hook.main_pre_tool_use()
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"hook must emit exactly one JSON object, got: {out!r}"
    assert json.loads(lines[0]) == {"decision": "allow"}


def test_ledger_charge_never_raises_without_workspace(tmp_path):
    from ctx.hook import _ledger_charge

    assert _ledger_charge(None, "s", 100) == 0
    # Unwritable ledger dir path (a file where the dir should be) → 0.
    blocker = tmp_path / ".ctx-session-reads"
    blocker.write_text("not a dir", encoding="utf-8")
    assert _ledger_charge(str(tmp_path), "s", 100) == 0


# ------------------------------------------------------- unchanged behavior
def test_below_budget_small_read_decision_is_byte_identical(tmp_path):
    small = _make_file(tmp_path, "small.txt", 5)
    d = _read(small, tmp_path)
    assert d == {"decision": "allow"}  # exact equality with prior contract


def test_ledger_dir_reads_are_never_counted(tmp_path):
    ledger_dir = tmp_path / ".ctx-session-reads"
    ledger_dir.mkdir()
    count = ledger_dir / "other-session.count"
    count.write_text("12345", encoding="utf-8")
    d = _read(count, tmp_path, session_id="sess-x")
    assert d == {"decision": "allow"}
    assert not _ledger_file(tmp_path, "sess-x").exists()  # nothing charged


def test_oversized_read_rewrite_charges_max_inline_bytes(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        "version = 1\n[budgets]\nsession_read_budget_bytes = 20000\n",
        encoding="utf-8",
    )
    big = _make_file(tmp_path, "big.txt", 20000)  # > 16384 inline budget
    d = _read(big, tmp_path)
    assert d["decision"] == "deny"  # canonical layer unchanged
    assert d["rewrite"]["updatedInput"]["limit"] == 240
    assert int(_ledger_file(tmp_path, "sess-fixed").read_text()) == 16384
    # A follow-up small read pushes 16384 + 5000 past the 20000 budget.
    small = _make_file(tmp_path, "small.txt", 5000)
    d2 = _read(small, tmp_path)
    assert d2["decision"] == "allow"
    assert PRESSURE_FLOOR <= d2["rewrite"]["updatedInput"]["limit"] <= PRESSURE_CEILING


# --------------------------------------------------------- configured budget
def test_configured_budget_flips_after_1kb(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        "version = 1\n[budgets]\nsession_read_budget_bytes = 1024\n",
        encoding="utf-8",
    )
    f = _make_file(tmp_path, "small.txt", 600)
    assert _read(f, tmp_path) == {"decision": "allow"}  # 600 ≤ 1024
    d = _read(f, tmp_path)  # 1200 > 1024
    assert d["decision"] == "allow"
    assert PRESSURE_FLOOR <= d["rewrite"]["updatedInput"]["limit"] <= PRESSURE_CEILING
    assert "session native-read budget exceeded" in d["rewrite"]["reason"]


def test_budgets_dataclass_field_default():
    from ctx.config import Budgets

    assert Budgets().session_read_budget_bytes == 262144


# ------------------------------------------- escalating over-budget pressure
# Both properties below were learned from a measured failure (evals/devex/,
# harnessed arm): 167 native reads landed 796 KiB against a 256 KiB budget.
def test_over_budget_window_escalates_with_overrun():
    """The throttle must tighten as the overrun grows, not latch at one step.

    The old behaviour was a single binary gate: the same window at 1.01x
    overrun and at 6.3x. A session already far over budget therefore kept
    paying the same toll, and 133 post-budget reads still averaged ~4 KiB."""
    from ctx.hook import _OVER_BUDGET_MIN_LINES, _pressured_window

    budget = 262144
    at_budget = _pressured_window(240, budget, budget)
    just_over = _pressured_window(240, budget + 1, budget)
    two_x = _pressured_window(240, 2 * budget, budget)
    six_x = _pressured_window(240, 1647077, budget)  # the measured session

    assert at_budget == 240, "at or under budget the window is untouched"
    assert just_over <= 240 // 4, "crossing the budget is at least as tight as before"
    assert two_x < just_over, "2x overrun must be tighter than 1x"
    assert six_x < two_x, "6.3x overrun must be tighter than 2x"
    assert six_x >= _OVER_BUDGET_MIN_LINES, "never below the useful-evidence floor"
    assert _pressured_window(240, 500 * budget, budget) == _OVER_BUDGET_MIN_LINES


def test_broken_ledger_applies_no_pressure():
    """Fail-open contract: a ledger that returns 0 must not throttle."""
    from ctx.hook import _pressured_window

    assert _pressured_window(240, 0, 262144) == 240
    assert _pressured_window(240, 100, 0) == 240


def test_large_file_read_feels_session_pressure(tmp_path):
    """A file over max_inline_bytes must be bounded by session pressure too.

    The large-file branch used to return before the ledger was consulted, so
    the biggest reads -- the ones that dominate the flood -- were the only
    ones exempt. Measured max read stayed ~15 KiB before and after the budget
    was crossed precisely because those reads never saw it."""
    (tmp_path / "ctx.toml").write_text(
        "version = 1\n[budgets]\nsession_read_budget_bytes = 20000\n",
        encoding="utf-8",
    )
    big = _make_file(tmp_path, "big.txt", 20000)  # > 16384 inline budget
    first = _read(big, tmp_path, session_id="s-pressure")
    assert first["rewrite"]["updatedInput"]["limit"] == 240, "fresh session: unpressured"

    # Drive the ledger well past its 20000-byte budget, then read big again.
    for i in range(6):
        _read(_make_file(tmp_path, f"f{i}.txt", 15000), tmp_path, session_id="s-pressure")
    later = _read(big, tmp_path, session_id="s-pressure")
    assert later["rewrite"]["updatedInput"]["limit"] < 240, (
        "a large file read by an over-budget session must be bounded tighter "
        "than the unpressured window"
    )
