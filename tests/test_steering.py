"""Acceptance: transparent input substitution ("rewrite, don't reject").

Layer 1: ``classify()`` keeps the canonical decision field stable and
attaches a ``rewrite`` field when steering allows substitution.
Layer 2: each dialect emitter converts a rewrite-bearing decision into its
allow+updatedInput wire form; steering="deny" is byte-identical to the old
deny-with-remediation contract.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


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


def _steering(workspace, value):
    (workspace / "ctx.toml").write_text(
        f'version = 1\n[guard]\nsteering = "{value}"\n', encoding="utf-8"
    )


# ------------------------------------------------------- command rewrites
def test_pytest_rewrite_antigravity_dialect(tmp_path):
    # No ctx.toml → default steering "auto".
    out = _invoke_hook(
        _payload({"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
    )
    # Antigravity calls its shallow argument rewrite `overwrite`.
    assert out["decision"] == "allow"
    assert out["overwrite"]["CommandLine"] == "ctx run -- pytest -q"
    assert "updatedInput" not in out
    assert set(out) <= {"decision", "reason", "permissionOverrides", "overwrite"}


def test_pytest_rewrite_claude_code_dialect(tmp_path):
    out = _invoke_hook(
        _payload({"command": "pytest -q", "Cwd": str(tmp_path)}, tmp_path, tool_name="Bash"),
        flavor="claude-code",
    )
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["command"] == "ctx run -- pytest -q"
    assert "bounded capture" in hso["permissionDecisionReason"]


def test_single_named_pytest_uses_guarded_native_fast_path(tmp_path):
    """The measured naive-friendly case avoids ctx's fixed capture tax.

    Claude/Codex can still replace an unexpected PostToolUse flood, so one
    explicit node runs unchanged while a broad suite keeps the capture path.
    """
    out = _invoke_hook(
        _payload(
            {"command": "pytest tests/test_api.py::test_health -q", "Cwd": str(tmp_path)},
            tmp_path,
            tool_name="Bash",
        ),
        flavor="claude-code",
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "updatedInput" not in hso

    rows = [
        json.loads(line)
        for line in (tmp_path / ".ctx-session-reads" / "steering-decisions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[-1]["action"] == "native"
    assert rows[-1]["signature"] == "pytest tests/test_api.py::test_health"


def test_single_named_pytest_stays_captured_without_output_substitution(tmp_path):
    out = _invoke_hook(
        _payload(
            {"CommandLine": "pytest tests/test_api.py::test_health -q", "Cwd": str(tmp_path)},
            tmp_path,
        ),
        flavor="antigravity",
    )
    assert out["decision"] == "allow"
    assert out["overwrite"]["CommandLine"].startswith("ctx run -- pytest")


def test_speculative_native_can_be_disabled(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\nspeculative_native = false\n', encoding="utf-8"
    )
    out = _invoke_hook(
        _payload(
            {"command": "pytest tests/test_api.py::test_health -q", "Cwd": str(tmp_path)},
            tmp_path,
            tool_name="Bash",
        ),
        flavor="claude-code",
    )
    assert "ctx run -- pytest" in out["hookSpecificOutput"]["updatedInput"]["command"]


def test_original_command_key_name_preserved(tmp_path):
    for key in ("CommandLine", "command"):
        d = _classify("run_command", {key: "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
        assert d["decision"] == "deny"  # canonical layer stays deny (doctor contract)
        updated = d["rewrite"]["updatedInput"]
        assert updated[key] == "ctx run -- pytest -q"
        assert set(updated) == {key, "Cwd"}


def test_extra_input_fields_survive_in_updated_input(tmp_path):
    d = _classify(
        "run_command",
        {
            "command": "pytest -q",
            "description": "run the test suite",
            "timeout": 120000,
            "Cwd": str(tmp_path),
        },
        tmp_path,
    )
    updated = d["rewrite"]["updatedInput"]
    assert updated["command"] == "ctx run -- pytest -q"
    assert updated["description"] == "run the test suite"
    assert updated["timeout"] == 120000


def test_bounded_chain_is_plain_allow_not_rewrite(tmp_path):
    d = _classify(
        "run_command",
        {"CommandLine": 'which ctx; echo hi; ls -la', "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d == {"decision": "allow"}


def test_chain_with_denied_segment_is_not_allowed(tmp_path):
    d = _classify(
        "run_command",
        {"CommandLine": "which ctx; pytest -q", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] != "allow"


def test_metachar_pipeline_rewrites_to_ctx_run_shell(tmp_path):
    cmd = "cat x | head -n 5"
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(tmp_path)}, tmp_path)
    assert d["decision"] == "force_ask"  # canonical layer unchanged
    assert d["rewrite"]["updatedInput"]["CommandLine"] == (
        "ctx run --shell -- " + shlex.quote(cmd)
    )
    out = _invoke_hook(_payload({"CommandLine": cmd, "Cwd": str(tmp_path)}, tmp_path))
    assert out["decision"] == "allow"
    assert out["overwrite"]["CommandLine"] == "ctx run --shell -- 'cat x | head -n 5'"


def test_grep_single_file_gets_match_cap_injected(tmp_path):
    (tmp_path / "notes.txt").write_text("TODO one\nTODO two\n", encoding="utf-8")
    d = _classify(
        "run_command",
        {"CommandLine": "grep TODO notes.txt", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] == "deny"  # canonical layer
    assert d["rewrite"]["updatedInput"]["CommandLine"] == "grep -m 25 TODO notes.txt"
    # Recursive grep never takes the cap path.
    d2 = _classify(
        "run_command",
        {"CommandLine": "grep -r TODO .", "Cwd": str(tmp_path)},
        tmp_path,
    )
    rw = d2.get("rewrite")
    assert rw is None or "-m 25" not in rw["updatedInput"]["CommandLine"]


# ------------------------------------------------- text tools (M-K5.3)
def test_sed_range_read_collapses_to_an_addressed_get(tmp_path):
    """`sed -n 'A,Bp' <file>` now has a rung on the replacement surface.

    It used to land on the generic steer — deny, plus `ctx run -- sed …`,
    which captures the output but keeps the operator's own unaddressed range.
    The collapse rung is strictly better: `ctx get repo:<f> --lines A:B` is
    the same bytes with a handle and a continuation that advances, so the
    replacement surface overrides the deny the same way it does for the
    flagship `grep -rn` case.
    """
    (tmp_path / "notes.txt").write_text("a\nb\n", encoding="utf-8")
    d = _classify(
        "run_command",
        {"CommandLine": "sed -n 1,5p notes.txt", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] == "allow"
    assert d["rewrite"]["updatedInput"]["CommandLine"] == (
        "ctx get repo:notes.txt --lines 1:5"
    )


def test_sed_outside_the_collapsible_range_shape_still_steers_to_ctx_run(tmp_path):
    """The generic steer must still cover every sed the rung declines.

    A script, an edit or a stdin read is not a range read, so it falls back to
    the canonical layer — the rung narrowing what reaches the steer must not
    narrow the steer itself.
    """
    (tmp_path / "notes.txt").write_text("a\nb\n", encoding="utf-8")
    d = _classify(
        "run_command",
        {"CommandLine": "sed -n /pat/p notes.txt", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] == "deny"  # canonical layer: unbounded output
    assert d["rewrite"]["updatedInput"]["CommandLine"].startswith("ctx run -- sed")


def test_sed_inplace_force_asks_with_preview_remediation(tmp_path):
    for cmd in (
        "sed -i s/a/b/ notes.txt",
        "sed -i.bak s/a/b/ notes.txt",
        "sed --in-place=.bak s/a/b/ notes.txt",
        "sed -ni s/a/b/p notes.txt",
    ):
        d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(tmp_path)}, tmp_path)
        assert d["decision"] == "force_ask", cmd
        assert "ctx rewrite" in d["reason"], cmd
        assert "rewrite" not in d, cmd  # mutation is never silently rerouted


def test_awk_inplace_force_asks_readonly_steers(tmp_path):
    d = _classify(
        "run_command",
        {"CommandLine": "gawk -i inplace '{print}' notes.txt", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] == "force_ask" and "ctx rewrite" in d["reason"]
    # Read-only awk with a program text carries `{}` → the compound path:
    # force_ask, steered into a bounded shell capture (mutation-free).
    d2 = _classify(
        "run_command",
        {"CommandLine": "awk '{print $1}' notes.txt", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d2["decision"] == "force_ask"
    assert d2["rewrite"]["updatedInput"]["CommandLine"].startswith("ctx run --shell -- ")
    # Braceless read-only awk (-f progfile) takes the plain-argv rung.
    d3 = _classify(
        "run_command",
        {"CommandLine": "awk -f prog.awk notes.txt", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d3["decision"] == "deny"
    assert d3["rewrite"]["updatedInput"]["CommandLine"].startswith("ctx run -- awk")


# ----------------------------------------------------------- read rewrites
def test_oversized_read_bounded_with_limit_under_auto(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 20000, encoding="utf-8")
    d = _classify("Read", {"file_path": str(big)}, tmp_path)
    assert d["decision"] == "deny"  # canonical layer
    updated = d["rewrite"]["updatedInput"]
    assert updated["file_path"] == str(big)  # original path field preserved
    assert updated["limit"] == 240  # max_inline_lines default
    assert "20000 bytes" in d["rewrite"]["reason"]
    assert "ctx get repo:" in d["rewrite"]["reason"]
    out = _invoke_hook(_payload({"file_path": str(big)}, tmp_path, tool_name="Read"))
    assert out["decision"] == "allow"
    assert "ctx get repo:" in out["reason"]
    assert out["overwrite"]["limit"] == 240
    assert "updatedInput" not in out


def test_oversized_read_denied_under_steering_deny(tmp_path):
    _steering(tmp_path, "deny")
    big = tmp_path / "big.txt"
    big.write_text("x" * 20000, encoding="utf-8")
    d = _classify("Read", {"file_path": str(big)}, tmp_path)
    assert d["decision"] == "deny"
    assert "rewrite" not in d


def test_secret_path_read_stays_force_ask_never_rewritten(tmp_path):
    d = _classify("Read", {"file_path": str(tmp_path / ".env")}, tmp_path)
    assert d["decision"] == "force_ask"
    assert "rewrite" not in d
    out = _invoke_hook(
        _payload({"file_path": str(tmp_path / ".env")}, tmp_path, tool_name="Read")
    )
    assert out["decision"] == "force_ask"
    assert "updatedInput" not in out


# ------------------------------------------------------- deny contract
def test_steering_deny_reproduces_old_deny_json_exactly(tmp_path):
    _steering(tmp_path, "deny")
    out = _invoke_hook(
        _payload({"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
    )
    assert out == {
        "decision": "deny",
        "reason": (
            "CTX_CONTEXT_GUARD: this command may emit unbounded output.\n"
            "Run it as: ctx run -- pytest -q\n"
            "Then use ctx search/get/stats on the returned handle."
        ),
    }


def test_steering_deny_keeps_grep_deny(tmp_path):
    _steering(tmp_path, "deny")
    (tmp_path / "notes.txt").write_text("TODO\n", encoding="utf-8")
    d = _classify(
        "run_command",
        {"CommandLine": "grep TODO notes.txt", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] == "deny"
    assert "rewrite" not in d


def test_canonical_classify_keeps_deny_for_doctor(tmp_path):
    # ctx doctor's self-test asserts classify() → "deny" for pytest with no
    # steering config present; the rewrite rides along as an extra field.
    d = _classify("run_command", {"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
    assert d["decision"] == "deny"
    assert "rewrite" in d


# ------------------------------------------------------------ fail-open
def test_fail_open_on_garbage_stdin_still_allows():
    assert _invoke_hook("this is not json {{{") == {"decision": "allow"}
