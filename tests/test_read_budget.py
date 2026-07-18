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
PRESSURE_LIMIT = 240 // 4  # max_inline_lines default // 4


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
    assert updated["limit"] == PRESSURE_LIMIT
    reason = d["rewrite"]["reason"]
    assert "session native-read budget exceeded" in reason
    assert "KiB raw reads" in reason
    assert "ctx search repo:" in reason
    assert "ctx get repo:" in reason

    # Emitters turn the pressured decision into allow+updatedInput.
    from ctx.hook import _to_antigravity_schema, _to_claude_code_schema

    wire = _to_antigravity_schema(dict(d))
    assert wire["decision"] == "allow"
    assert wire["updatedInput"]["limit"] == PRESSURE_LIMIT
    hso = _to_claude_code_schema(dict(d))["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["limit"] == PRESSURE_LIMIT


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
    assert d2["rewrite"]["updatedInput"]["limit"] == PRESSURE_LIMIT


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
    assert d["rewrite"]["updatedInput"]["limit"] == PRESSURE_LIMIT
    assert "session native-read budget exceeded" in d["rewrite"]["reason"]


def test_budgets_dataclass_field_default():
    from ctx.config import Budgets

    assert Budgets().session_read_budget_bytes == 262144
