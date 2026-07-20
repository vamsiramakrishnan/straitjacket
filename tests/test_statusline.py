"""Acceptance: host-neutral status line rendering.

Each host's payload normalises to one line; cost prefers a host-reported
dollar value (Claude Code) and otherwise prices tokens (Antigravity, Codex);
malformed input is a short/empty line, never a crash."""

import json
import subprocess
import sys
from pathlib import Path

from ctx import statusline

SRC = Path(__file__).resolve().parent.parent / "src"


def _run(host, payload, extra=None):
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    argv = [sys.executable, "-m", "ctx", "statusline", host] + (extra or [])
    proc = subprocess.run(
        argv, input=json.dumps(payload).encode() if payload is not None else b"",
        capture_output=True, env=env, timeout=30,
    )
    return proc.stdout.decode().strip(), proc.returncode


def test_claude_uses_host_reported_cost():
    line = statusline.render(
        "claude-code",
        {"model": {"display_name": "Sonnet 5"},
         "cost": {"total_cost_usd": 0.4231, "total_lines_added": 12,
                  "total_lines_removed": 3}},
    )
    assert "Sonnet 5" in line
    assert "$0.42" in line          # host-reported, no ~ prefix
    assert "~$" not in line
    assert "+12/-3" in line


def test_antigravity_prices_tokens_when_no_cost():
    line = statusline.render(
        "antigravity",
        {"model": {"display_name": "gemini-3.5-flash"},
         "context_window": {"used_percentage": 42.0},
         "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
         "vcs": {"branch": "main", "dirty": True}},
    )
    assert "gemini-3.5-flash" in line
    assert "ctx 42%" in line
    assert "~$1.50" in line          # 1M input @ $1.50/Mtok, estimate marker
    assert "⎇ main*" in line


def test_no_cost_when_no_tokens_and_no_reported():
    line = statusline.render("antigravity", {"model": {"display_name": "gemini-3-pro"}})
    assert "gemini-3-pro" in line
    assert "$" not in line            # nothing to price, nothing claimed


def test_render_never_raises_on_garbage():
    assert statusline.render("claude-code", None) == ""
    assert statusline.render("x", {"model": 12345}) is not None


def test_codex_rollout_summary(tmp_path):
    roll = tmp_path / "rollout.jsonl"
    roll.write_text("\n".join([
        json.dumps({"payload": {"model": "gpt-5.3-codex"}}),
        json.dumps({"payload": {"info": {"total_token_usage": {
            "input_tokens": 200_000, "cached_input_tokens": 50_000,
            "output_tokens": 30_000, "reasoning_output_tokens": 10_000,
            "total_tokens": 290_000}}}}),
    ]), encoding="utf-8")
    line = statusline.codex_rollout_summary(roll)
    assert "gpt-5.3-codex" in line
    assert "290K tok" in line
    assert line.count("$") == 1      # one priced figure


def test_codex_rollout_missing_file_is_empty():
    assert statusline.codex_rollout_summary("/no/such/rollout.jsonl") == ""


def test_cli_fail_open_on_garbage_stdin():
    out, rc = _run("claude-code", None, extra=[])
    assert rc == 0                   # never errors into the host prompt
    # garbage handled: empty or minimal, but exit 0
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "statusline", "claude-code"],
        input=b"not json{{", capture_output=True,
        env={"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", "")},
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""


def test_cli_end_to_end_antigravity():
    out, rc = _run(
        "antigravity",
        {"model": {"display_name": "gemini-3.5-flash"},
         "context_window": {"used_percentage": 10.0}},
    )
    assert rc == 0
    assert "gemini-3.5-flash" in out
