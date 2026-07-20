"""Acceptance: graduated engagement (mechanism C) and the emission governor
(mechanism B) — measured signals scale the harness footprint and police
output volume; nothing here ever affects guard safety decisions."""

import io
import json

import pytest


@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return d


def _write_window(ws, **kw):
    doc = {
        "model": "claude-sonnet-5",
        "window_pct": 1.0,
        "requests": 1,
        "cum_output": 0,
        **kw,
    }
    p = ws / ".ctx-session-reads" / "proxy"
    p.mkdir(parents=True, exist_ok=True)
    (p / "window.json").write_text(json.dumps(doc), encoding="utf-8")


# ------------------------------------------------------------- graduation
def test_sessions_start_passive_and_graduate_by_call_count(ws):
    from ctx.engagement import note_call, read_state

    for _ in range(2):
        assert note_call(ws, activate_after_calls=3) == "passive"
    assert note_call(ws, activate_after_calls=3) == "active"
    assert read_state(ws)["activated_by"] == "call_count"
    # Once active, never regresses.
    assert note_call(ws, activate_after_calls=3) == "active"


def test_window_pressure_graduates_immediately(ws):
    from ctx.engagement import note_call

    _write_window(ws, window_pct=40.0)  # >= 70/2
    assert note_call(ws, activate_after_calls=99, window_pressure_pct=70) == "active"


def test_truncation_graduates_immediately(ws):
    from ctx.engagement import note_call, note_truncation, read_state

    assert note_call(ws, activate_after_calls=99) == "passive"
    note_truncation(ws)
    assert read_state(ws)["level"] == "active"
    assert read_state(ws)["activated_by"] == "truncation"


def test_explicit_modes_bypass_graduation(ws):
    from ctx.engagement import note_call

    assert note_call(ws, mode="active", activate_after_calls=99) == "active"
    assert note_call(ws, mode="passive", activate_after_calls=0) == "passive"
    assert note_call(None) == "active"  # no workspace: full affordances


# --------------------------------------------------------- suggestion cap
def test_suggestion_cap_by_level_and_model(ws):
    from ctx.engagement import note_truncation, suggestion_cap

    assert suggestion_cap(ws) == 0  # auto + passive
    note_truncation(ws)
    assert suggestion_cap(ws) == 3  # auto + active, capable model
    _write_window(ws, model="claude-haiku-4-5-20251001")
    assert suggestion_cap(ws) == 1  # lean model keeps one affordance
    assert suggestion_cap(ws, mode="passive") == 0
    assert suggestion_cap(None) == 3


def test_lean_model_recognition_is_host_neutral():
    """The lean-model default must recognize the small/fast tier of every
    supported host by token, not by raw substring (regression: 'gemini'
    contains 'mini')."""
    from ctx.engagement import model_matches_lean

    lean = [
        "claude-haiku-4-5-20251001",  # Claude
        "gemini-3.5-flash",  # Antigravity default
        "gemini-3-flash-lite",
        "gpt-5-mini",  # Codex / OpenAI
        "gpt-5-nano",
        "o4-mini",
        "GEMINI-3.5-FLASH",  # case-insensitive
    ]
    capable = [
        "claude-sonnet-5",
        "claude-opus-4-8",
        "gemini-3-pro",  # 'ge-mini' must NOT match 'mini'
        "gpt-5",
        "",
    ]
    for m in lean:
        assert model_matches_lean(m), f"{m!r} should be lean"
    for m in capable:
        assert not model_matches_lean(m), f"{m!r} should be capable"


def test_lean_models_config_default_matches_engagement():
    """config.py must not re-hardcode the list — one source of truth."""
    from ctx.config import Engagement
    from ctx.engagement import DEFAULT_LEAN_MODELS

    assert Engagement().lean_models == DEFAULT_LEAN_MODELS


def test_lean_models_override_is_respected():
    from ctx.engagement import model_matches_lean

    # A repo can name its own model as lean, or clear the list entirely.
    assert model_matches_lean("acme-tiny-1", ["tiny"])
    assert not model_matches_lean("gemini-3.5-flash", ("pro",))
    assert not model_matches_lean("gpt-5-mini", ())


def test_passive_digest_carries_no_suggestions():
    from ctx.digest.base import DigestContext, Profile

    ctx = DigestContext.__new__(DigestContext)
    ctx.suggestion_cap = 0
    assert Profile().next_lines(ctx, ["ctx get run:x"]) == []
    ctx.suggestion_cap = 1
    assert Profile().next_lines(ctx, ["a", "b", "c"]) == ["next:", "  a"]
    ctx.suggestion_cap = 3
    assert len(Profile().next_lines(ctx, ["a", "b", "c"])) == 4


def test_filter_digest_at_emission_boundary():
    """The stored digest is canonical; filtering happens only on the emitted
    copy — this is what keeps re-digests byte-identical (SPEC §8)."""
    from ctx.engagement import filter_digest

    digest = "header\nsummary:\n  tests: 3\nnext:\n  ctx search run:x 'a'\n  ctx get run:x --lines 1:9\ntail"
    assert filter_digest(digest, 3) == digest  # full engagement: untouched
    filtered0 = filter_digest(digest, 0)
    assert "next:" not in filtered0
    assert "ctx search" not in filtered0
    assert "summary:" in filtered0 and "tail" in filtered0  # only the block goes
    filtered1 = filter_digest(digest, 1)
    assert "ctx search run:x 'a'" in filtered1
    assert "ctx get" not in filtered1


def test_hook_classify_counts_calls(ws):
    from ctx.engagement import read_state
    from ctx.hook import classify

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "cwd": str(ws),
    }
    classify(payload)
    classify(payload)
    assert read_state(ws)["calls"] == 2


# -------------------------------------------------------- emission governor
def test_emission_tier_claimed_once(ws):
    from ctx.engagement import claim_emission_tier

    assert claim_emission_tier(ws, 1) is True
    assert claim_emission_tier(ws, 1) is False  # same tier: silence
    assert claim_emission_tier(ws, 3) is True  # higher tier: one more nudge
    assert claim_emission_tier(ws, 2) is False  # never re-nudge below max
    assert claim_emission_tier(None, 5) is False
    assert claim_emission_tier(ws, 0) is False


def test_emission_nudge_fires_on_verbose_sessions_only(ws):
    from ctx.hook import _emission_nudge

    payload = {"cwd": str(ws)}
    assert _emission_nudge(payload) is None  # no window.json: silence
    _write_window(ws, cum_output=25_000, requests=10)  # 2.5k/turn: verbose
    nudge = _emission_nudge(payload)
    assert nudge is not None and "CTX_EMISSION_GOVERNOR" in nudge
    assert "25,000" in nudge
    assert _emission_nudge(payload) is None  # tier already claimed

    # A terse session never gets nudged, no matter the volume.
    ws2 = ws.parent / "proj2"
    ws2.mkdir()
    (ws2 / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    _write_window(ws2, cum_output=60_000, requests=200)  # 300/turn: terse
    assert _emission_nudge({"cwd": str(ws2)}) is None


def test_post_tool_use_stage_emits_dialects(ws, monkeypatch, capsys):
    from ctx.cli import main

    _write_window(ws, cum_output=25_000, requests=10)
    payload = json.dumps({"cwd": str(ws)})

    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["hook", "claude-code", "post-tool-use"]) == 0
    out = json.loads(capsys.readouterr().out)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "CTX_EMISSION_GOVERNOR" in hso["additionalContext"]

    # Same tier again → exactly one no-op JSON object, silence.
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["hook", "claude-code", "post-tool-use"]) == 0
    assert json.loads(capsys.readouterr().out) == {}

    # Antigravity dialect on a fresh tier crossing.
    _write_window(ws, cum_output=45_000, requests=11)
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["hook", "antigravity", "post-tool-use"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "allow"
    assert "CTX_EMISSION_GOVERNOR" in out["reason"]


def test_post_tool_use_fails_open_on_garbage(monkeypatch, capsys):
    from ctx.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO("not json{{{"))
    assert main(["hook", "claude-code", "post-tool-use"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


# ------------------------------------------------- navigation governor
def test_symbol_grep_detection():
    from ctx.hook import _grep_symbol

    assert _grep_symbol("grep -rn register_span src/") == "register_span"
    assert _grep_symbol("rg mint_span") == "mint_span"
    assert _grep_symbol("grep -rn 'def foo' src/") is None  # not a bare ident
    assert _grep_symbol("cat foo.py") is None
    assert _grep_symbol("grep -rn foo.bar src/") is None  # dotted, not bare


def test_navigation_governor_fires_once_at_threshold(ws):
    from ctx.hook import _navigation_nudge

    def call(sym):
        return _navigation_nudge(
            {"tool_name": "Bash",
             "tool_input": {"command": f"grep -rn {sym} src/"}, "cwd": str(ws)}
        )
    assert call("alpha") is None       # 1 distinct
    assert call("beta") is None        # 2 distinct
    n = call("gamma")                  # 3rd → fire
    assert n is not None and "CTX_NAV_GOVERNOR" in n and "ctx impact gamma" in n
    assert call("delta") is None       # already fired: silent
    # a repeat of an already-seen symbol does not re-count
    assert call("alpha") is None


def test_navigation_governor_ignores_non_symbol_greps(ws):
    from ctx.hook import _navigation_nudge

    for _ in range(5):
        assert _navigation_nudge(
            {"tool_name": "Bash",
             "tool_input": {"command": "grep -rn 'TODO fix' src/"}, "cwd": str(ws)}
        ) is None  # multi-word pattern is not symbol-tracing
