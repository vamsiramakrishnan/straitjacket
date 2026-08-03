"""Acceptance: window-pressure loop (proxy ground truth tightens the guard).

The Tier-0 proxy writes ``<workspace>/.ctx-session-reads/proxy/window.json``
with ``{"window_pct": float, ...}``. At or above ``[budgets]
window_pressure_pct`` (default 70) the guard scales its effective
``max_inline_bytes``, ``session_read_budget_bytes``, and head/tail ``-n``
cap by ``max(0.25, 1 - (window_pct - threshold)/100*2)`` and marks affected
reasons with a window-fullness suffix. Absent/corrupt/unreadable telemetry
and sub-threshold fullness leave every decision byte-identical.
"""

import json

DEFAULT_INLINE = 16384
DEFAULT_THRESHOLD = 70


def _classify(tool_name, tool_input, workspace, session_id="sess-w"):
    from ctx.hook import classify

    return classify(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "workspacePaths": [str(workspace)],
            "session_id": session_id,
        }
    )


def _read(path, workspace, session_id="sess-w"):
    return _classify("Read", {"file_path": str(path)}, workspace, session_id)


def _cmd(command, workspace):
    return _classify("run_command", {"CommandLine": command, "Cwd": str(workspace)}, workspace)


def _make_file(workspace, name, nbytes):
    p = workspace / name
    p.write_text("x" * nbytes, encoding="utf-8")
    return p


def _window(workspace, pct):
    d = workspace / ".ctx-session-reads" / "proxy"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "window.json"
    path.write_text(
        json.dumps(
            {
                "window_pct": pct,
                "model": "claude-sonnet-5",
                "last_input_tokens": int(2000 * pct),
                "context_limit": 200000,
                "requests": 3,
            }
        ),
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------- inline tightening
def test_12kb_read_flips_from_allow_to_deny_at_84_pct(tmp_path):
    """factor = max(0.25, 1 - (84-70)/100*2) = 0.72 → effective inline
    budget int(16384*0.72) = 11796 < 12288."""
    f = _make_file(tmp_path, "mid.txt", 12288)  # 12 KiB < 16 KiB default
    assert _read(f, tmp_path, session_id="s-before") == {"decision": "allow"}

    _window(tmp_path, 84.0)
    d = _read(f, tmp_path, session_id="s-after")
    assert d["decision"] == "deny"  # canonical layer
    assert "> 11796 inline budget" in d["reason"]
    assert " [window 84% full — budgets tightened]" in d["reason"]
    # Steering auto still bounds instead of blocking; note rides along.
    assert d["rewrite"]["updatedInput"]["limit"] == 240
    assert " [window 84% full — budgets tightened]" in d["rewrite"]["reason"]


def test_below_threshold_is_byte_identical(tmp_path):
    f = _make_file(tmp_path, "mid.txt", 12288)
    _window(tmp_path, 69.9)
    assert _read(f, tmp_path) == {"decision": "allow"}  # exact equality


def test_absent_and_corrupt_window_json_change_nothing(tmp_path):
    f = _make_file(tmp_path, "mid.txt", 12288)
    assert _read(f, tmp_path, session_id="s-a") == {"decision": "allow"}
    p = _window(tmp_path, 99.0)
    p.write_text("{not json", encoding="utf-8")  # corrupt → fail-open
    assert _read(f, tmp_path, session_id="s-b") == {"decision": "allow"}


def test_unreadable_window_json_fails_open(tmp_path):
    f = _make_file(tmp_path, "mid.txt", 12288)
    d = tmp_path / ".ctx-session-reads" / "proxy" / "window.json"
    d.mkdir(parents=True)  # a directory where the file should be
    assert _read(f, tmp_path) == {"decision": "allow"}


def test_factor_floors_at_a_quarter(tmp_path):
    """At 130% the linear ramp would go negative; the floor holds at 0.25 →
    effective inline budget 4096."""
    f = _make_file(tmp_path, "small.txt", 5000)
    assert _read(f, tmp_path, session_id="s-cold") == {"decision": "allow"}
    _window(tmp_path, 130.0)
    d = _read(f, tmp_path, session_id="s-hot")
    assert d["decision"] == "deny"
    assert "> 4096 inline budget" in d["reason"]
    assert "[window 130% full — budgets tightened]" in d["reason"]


# --------------------------------------------------- configurable threshold
def test_threshold_configurable_via_ctx_toml(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        "version = 1\n[budgets]\nwindow_pressure_pct = 90\n", encoding="utf-8"
    )
    f = _make_file(tmp_path, "mid.txt", 12288)
    _window(tmp_path, 84.0)  # would tighten under the default threshold
    assert _read(f, tmp_path, session_id="s-cfg-a") == {"decision": "allow"}

    _window(tmp_path, 95.0)  # factor 0.9 → 14745; 15000-byte file now over
    big = _make_file(tmp_path, "bigger.txt", 15000)
    d = _read(big, tmp_path, session_id="s-cfg-b")
    assert d["decision"] == "deny"
    assert "> 14745 inline budget" in d["reason"]
    assert "[window 95% full — budgets tightened]" in d["reason"]


def test_budgets_dataclass_default():
    from ctx.config import Budgets

    assert Budgets().window_pressure_pct == 70


# ---------------------------------------------------- session-budget scaling
def test_session_read_budget_tightens_under_pressure(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        "version = 1\n[budgets]\nsession_read_budget_bytes = 1000\n",
        encoding="utf-8",
    )
    f = _make_file(tmp_path, "chunk.txt", 800)
    # 800 ≤ 1000: clean allow while the window is calm.
    assert _read(f, tmp_path, session_id="s-calm") == {"decision": "allow"}
    # At 84% the effective budget is int(1000*0.72) = 720 < 800: the very
    # first read of a fresh session comes under graduated pressure.
    _window(tmp_path, 84.0)
    d = _read(f, tmp_path, session_id="s-tight")
    assert d["decision"] == "allow"
    assert 20 <= d["rewrite"]["updatedInput"]["limit"] <= 240 // 4
    assert "session native-read budget exceeded" in d["rewrite"]["reason"]
    assert " [window 84% full — budgets tightened]" in d["rewrite"]["reason"]


# --------------------------------------------------------- head/tail -n cap
def test_head_tail_cap_scales_with_window(tmp_path):
    """Exercised through `tail`, deliberately.

    `head` used to demonstrate this cap, but it has since gained a rung on the
    replacement surface, so a `head -n N` is collapsed to `ctx get --lines 1:N`
    before the cap can bite. That is an improvement rather than a regression —
    `ctx get` is bounded by `result_tokens`, which is itself scaled by this
    same window pressure, so the flood is prevented by a stricter mechanism.
    But it does mean `head` can no longer show that this cap works.

    `tail` has no collapse rung (`ctx get` has no from-the-end window, so any
    mapping would guess), so it still reaches this layer and is now what pins
    the behaviour. If `tail` ever gains a rung, this test needs a new subject —
    not deletion.
    """
    f = _make_file(tmp_path, "notes.txt", 100)
    cmd = f"tail -n 300 {f.name}"
    assert _cmd(cmd, tmp_path) == {"decision": "allow"}  # 300 ≤ 400 default

    _window(tmp_path, 84.0)  # cap int(400*0.72) = 288 < 300
    d = _cmd(cmd, tmp_path)
    assert d["decision"] == "deny"
    assert d["reason"].endswith(" [window 84% full — budgets tightened]")
    assert d["rewrite"]["updatedInput"]["CommandLine"] == f"ctx run -- tail -n 300 {f.name}"
    assert " [window 84% full — budgets tightened]" in d["rewrite"]["reason"]
    # Still inside the tightened cap: unchanged allow.
    assert _cmd(f"tail -n 200 {f.name}", tmp_path) == {"decision": "allow"}


def test_head_is_collapsed_before_the_cap_applies(tmp_path):
    """The interaction the test above documents, asserted rather than implied.

    A `head` under window pressure must still not flood: it is replaced by a
    bounded `ctx get`, whose own budget tightens with the same pressure.
    """
    f = _make_file(tmp_path, "notes.txt", 100)
    _window(tmp_path, 84.0)
    d = _cmd(f"head -n 300 {f.name}", tmp_path)
    assert d["decision"] == "allow"
    assert d["rewrite"]["updatedInput"]["CommandLine"] == (
        f"ctx get repo:{f.name} --lines 1:300"
    )
