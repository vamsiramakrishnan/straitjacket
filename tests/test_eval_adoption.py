"""Acceptance: ctx py teaching surface (measured gap: eval-collapse A/B,
finding 2 — 0/3 live adoption of `ctx py`, agents write raw python
heredocs / -c chains).

Three contracts:
* detection — `python3 << EOF` heredocs and `python -c` chains are eval
  opportunities; `python3 -m pytest` and `python3 script.py` are not;
* teaching — a denied/force_ask'd opportunity's remediation additionally
  teaches the collapse move (append-only, never an auto-rewrite);
* adoption ledger — every detected opportunity appends one fail-open JSON
  line to `.ctx-session-reads/eval-adoption.jsonl` (the denominator; actual
  eval use is store telemetry op="eval").
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

HEREDOC_CMD = "python3 << 'EOF'\nprint(1)\nEOF"


def _invoke_hook(payload: str, flavor: str = "antigravity") -> dict:
    """Run the real hook entry end-to-end (stdin JSON → stdout JSON)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", flavor, "pre-tool-use"],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    assert len(lines) == 1, f"hook must emit exactly one JSON object, got: {proc.stdout!r}"
    return json.loads(lines[0])


def _classify(tool_name, tool_input, workspace):
    from ctx.hook import classify

    return classify(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "workspacePaths": [str(workspace)],
        }
    )


def _payload(tool_input, workspace, tool_name="run_command"):
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "workspacePaths": [str(workspace)],
        }
    )


def _steering_deny(workspace):
    (workspace / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )


def _ledger_lines(workspace):
    path = workspace / ".ctx-session-reads" / "eval-adoption.jsonl"
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------- detection
def test_detection_positive():
    from ctx.hook import _eval_opportunity

    for cmd in (
        HEREDOC_CMD,
        "python3 <<'PY'\nimport sys\nPY",
        'python -c "print(1)"',
        "python3.12 -c 'x=1'",
        "PYTHONPATH=. timeout 5 python3 -c 'import mod'",  # unwrapped
        # Ephemeral-script pattern (the measured evasion: cat > /tmp/x.py
        # then run it — eval-collapse doc layer 2b, 0 ledger entries).
        "python3 /tmp/scratch/analyze_p95.py",
        "python /tmp/claude-x/scratchpad/job.py --fast",
        "python3 /home/user/.cache/scratchpad/tmp_script.py",
    ):
        assert _eval_opportunity(cmd), cmd


def test_detection_negative():
    from ctx.hook import _eval_opportunity

    for cmd in (
        "python3 -m pytest",
        "python3 -m pytest -c pytest.ini",  # -c belongs to pytest, not python
        "python3 script.py",
        "python3 script.py -c whatever",  # -c after the script path
        "python3 tools/gen.py",  # workspace-resident: addressable code
        "python3 /usr/share/doc/python3/examples/x.py",  # system path
        "python3",
        "python3 --version",
        "pytest -q",
        "cat big.log",
        "ctx py 'print(1)'",  # already the collapsed form
        "",
    ):
        assert not _eval_opportunity(cmd), cmd


# ------------------------------------------------------------ remediation
def test_denied_python_dash_c_teaches_ctx_eval(tmp_path):
    _steering_deny(tmp_path)
    d = _classify(
        "run_command",
        {"CommandLine": "python3 -c 'import_this'", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] == "deny"
    assert "ctx run -- " in d["reason"]  # existing remediation intact
    assert "ctx py" in d["reason"]  # teach appended


def test_force_ask_python_heredoc_teaches_ctx_eval(tmp_path):
    _steering_deny(tmp_path)
    d = _classify(
        "run_command", {"CommandLine": HEREDOC_CMD, "Cwd": str(tmp_path)}, tmp_path
    )
    assert d["decision"] == "force_ask"
    assert "ctx py" in d["reason"]


def test_teach_rides_rewrite_reason_under_auto_steering(tmp_path):
    # Default steering: the heredoc is rewritten to ctx run --shell (never
    # auto-rewritten into ctx py), and the teach line rides the reason.
    d = _classify(
        "run_command", {"CommandLine": HEREDOC_CMD, "Cwd": str(tmp_path)}, tmp_path
    )
    assert d["decision"] == "force_ask"  # canonical layer unchanged
    rewritten = d["rewrite"]["updatedInput"]["CommandLine"]
    assert rewritten.startswith("ctx run --shell -- ")
    assert "ctx py" not in rewritten  # teaching-only: no eval auto-rewrite
    assert "ctx py" in d["rewrite"]["reason"]


def test_non_python_deny_does_not_teach(tmp_path):
    _steering_deny(tmp_path)
    d = _classify(
        "run_command", {"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path
    )
    assert d["decision"] == "deny"
    assert "ctx py" not in d["reason"]


# --------------------------------------------------------- adoption ledger
def test_ledger_line_written_and_valid(tmp_path):
    _classify(
        "run_command",
        {"CommandLine": "python -c 'print(1)'", "Cwd": str(tmp_path)},
        tmp_path,
    )
    lines = _ledger_lines(tmp_path)
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["op"] == "eval_opportunity"
    assert event["taught"] is True


def test_ledger_taught_false_when_opportunity_is_allowed(tmp_path):
    # Full redirection proves console output small → allow; the opportunity
    # is still counted (denominator), with taught=false.
    d = _classify(
        "run_command",
        {"CommandLine": "python3 -c 'x' > out.log 2>&1", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] == "allow"
    lines = _ledger_lines(tmp_path)
    assert len(lines) == 1
    assert json.loads(lines[0])["taught"] is False


def test_no_ledger_line_for_non_opportunity(tmp_path):
    _classify(
        "run_command", {"CommandLine": "python3 -m pytest", "Cwd": str(tmp_path)}, tmp_path
    )
    _classify("run_command", {"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
    assert _ledger_lines(tmp_path) == []


def test_ledger_io_failure_does_not_break_decision(tmp_path):
    # Make the ledger dir uncreatable (a file sits where the dir must go);
    # works even when tests run as root, unlike permission bits.
    (tmp_path / ".ctx-session-reads").write_text("not a dir", encoding="utf-8")
    d = _classify(
        "run_command",
        {"CommandLine": "python -c 'print(1)'", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] in ("deny", "force_ask")
    assert "ctx py" in d["reason"]  # teaching still fires


# ------------------------------------------------------ decision JSON contract
def test_hook_end_to_end_antigravity_heredoc(tmp_path):
    out = _invoke_hook(
        _payload({"CommandLine": HEREDOC_CMD, "Cwd": str(tmp_path)}, tmp_path)
    )
    # Antigravity applies the rewrite through its `overwrite` field; teaching
    # still rides along in the reason.
    assert out["decision"] == "allow"
    assert out["overwrite"]["CommandLine"].startswith("ctx run --shell -- ")
    assert "ctx py" in out["reason"]


def test_hook_end_to_end_claude_code_denied_dash_c(tmp_path):
    _steering_deny(tmp_path)
    out = _invoke_hook(
        _payload(
            {"command": "python3 -c 'import_this'", "Cwd": str(tmp_path)},
            tmp_path,
            tool_name="Bash",
        ),
        flavor="claude-code",
    )
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "ctx py" in hso["permissionDecisionReason"]


def test_plain_ctx_eval_command_stays_allowed(tmp_path):
    d = _classify(
        "run_command", {"CommandLine": "ctx py 'print(1)'", "Cwd": str(tmp_path)}, tmp_path
    )
    assert d == {"decision": "allow"}
    assert _ledger_lines(tmp_path) == []
