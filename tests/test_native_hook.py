"""Acceptance: the Rust post-tool-use shim is byte-identical to Python.

The native binary is an accelerator, never a requirement: these tests skip
when it hasn't been built (cargo build --release in native/ctx-hook-native).
Golden cases cover silence paths, both dialect nudges, tier dedup, and the
shared engagement.json state file both implementations must agree on."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NATIVE = REPO / "native" / "ctx-hook-native" / "target" / "release" / "ctx-hook-native"

# CI sets CTX_REQUIRE_NATIVE=1 after building the binary, so a missing shim is a
# hard failure instead of a silent skip: the whole point of this suite is to
# catch Rust<->Python drift, and a suite that quietly skips catches nothing.
REQUIRE_NATIVE = os.environ.get("CTX_REQUIRE_NATIVE") == "1"

pytestmark = pytest.mark.skipif(
    not NATIVE.is_file() and not REQUIRE_NATIVE,
    reason="native shim not built (cargo build --release)",
)


def test_native_binary_present_when_required():
    """Guard against a green-but-vacuous CI run: if CTX_REQUIRE_NATIVE is set,
    the binary must actually exist so the parity assertions below really run."""
    if REQUIRE_NATIVE:
        assert NATIVE.is_file(), (
            f"CTX_REQUIRE_NATIVE=1 but {NATIVE} is missing — the CI build step "
            "must run `cargo build --release` before this suite."
        )


def _python(flavor: str, payload: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", flavor, "post-tool-use"],
        input=payload, capture_output=True, text=True,
        env={"PYTHONPATH": str(REPO / "src"), **__import__("os").environ},
    )
    return proc.stdout


def _native(flavor: str, payload: str) -> str:
    proc = subprocess.run(
        [str(NATIVE), "hook", flavor, "post-tool-use"],
        input=payload, capture_output=True, text=True,
    )
    return proc.stdout


def _ws(tmp_path, cum_output=0, requests=0, nudge_tokens=None):
    d = tmp_path / "proj"
    d.mkdir(exist_ok=True)
    toml = "version = 1\n"
    if nudge_tokens is not None:
        toml += f"[engagement]\nemission_nudge_tokens = {nudge_tokens}\n"
    (d / "ctx.toml").write_text(toml)
    if requests:
        p = d / ".ctx-session-reads" / "proxy"
        p.mkdir(parents=True, exist_ok=True)
        (p / "window.json").write_text(
            json.dumps({"cum_output": cum_output, "requests": requests})
        )
    return d


def _reset(ws):
    eng = ws / ".ctx-session-reads" / "engagement.json"
    eng.unlink(missing_ok=True)


CASES = [
    ("empty payload", "{}"),
    ("garbage", "not json {{{"),
    ("no window", None),  # payload built per-ws below
]


def test_silence_paths_identical(tmp_path):
    ws = _ws(tmp_path)
    payload = json.dumps({"cwd": str(ws)})
    for flavor in ("claude-code", "antigravity"):
        for raw in ("{}", "not json {{{", payload):
            assert _python(flavor, raw) == _native(flavor, raw) == "{}\n"


def test_nudge_byte_identical_both_dialects(tmp_path):
    """Parity is byte-for-byte per dialect — including where the dialect's
    published contract forbids the nudge. Claude Code carries the governor line
    in additionalContext; Antigravity's PostToolUse permits only `{}`, so both
    implementations must withhold it there rather than one inventing a field
    (https://antigravity.google/docs/hooks)."""
    ws = _ws(tmp_path, cum_output=25_000, requests=10)
    payload = json.dumps({"cwd": str(ws)})
    for flavor, carries_nudge in (("claude-code", True), ("antigravity", False)):
        _reset(ws)
        py = _python(flavor, payload)
        _reset(ws)
        rs = _native(flavor, payload)
        if carries_nudge:
            assert "CTX_EMISSION_GOVERNOR" in py
        else:
            assert py == "{}\n"
        assert py == rs, f"dialect {flavor} diverged:\npy: {py}\nrs: {rs}"


def test_tier_dedup_shared_state(tmp_path):
    """Python claims the tier; the native shim must observe the claim (and
    vice versa) through the same flock'd state file."""
    ws = _ws(tmp_path, cum_output=25_000, requests=10)
    payload = json.dumps({"cwd": str(ws)})
    assert "CTX_EMISSION_GOVERNOR" in _python("claude-code", payload)
    assert _native("claude-code", payload) == "{}\n"  # tier already claimed
    # Higher tier: native claims, python then stays silent.
    (ws / ".ctx-session-reads" / "proxy" / "window.json").write_text(
        json.dumps({"cum_output": 45_000, "requests": 11})
    )
    assert "CTX_EMISSION_GOVERNOR" in _native("claude-code", payload)
    assert _python("claude-code", payload) == "{}\n"


def test_config_threshold_respected(tmp_path):
    ws = _ws(tmp_path, cum_output=25_000, requests=10, nudge_tokens=999_999_999)
    payload = json.dumps({"cwd": str(ws)})
    assert _python("claude-code", payload) == _native("claude-code", payload) == "{}\n"


def test_terse_sessions_never_nudged(tmp_path):
    ws = _ws(tmp_path, cum_output=60_000, requests=200)  # 300/turn
    payload = json.dumps({"cwd": str(ws)})
    assert _python("claude-code", payload) == _native("claude-code", payload) == "{}\n"
