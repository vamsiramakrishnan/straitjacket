from __future__ import annotations

from ctx.hook import classify_command


def _policy(**over):
    policy = {
        "mode": "guarded",
        "unknown_command": "force_ask",
        "steering": "auto",
        "allow_commands": [],
        "deny_commands": [],
    }
    policy.update(over)
    return policy


def test_new_bounded_spans_pass_without_manual_ctx_wrapper():
    commands = (
        "mktemp -d /tmp/ctx-test.XXXXXX",
        "git diff --stat",
        "git -C repo diff --check",
        "gh run list --limit 10 --json databaseId,name,status,conclusion,url,headSha",
        "env CI=1 timeout 30 gh pr list --limit 20",
    )
    for command in commands:
        assert classify_command(command, _policy()) == {"decision": "allow"}, command


def test_readonly_noisy_spans_are_transparent_capture_candidates():
    for command in ("gh pr view 123", "gh run view 123 --log", "fd pattern .", "rspec"):
        decision = classify_command(command, _policy())
        assert decision["decision"] == "deny", command
        assert decision["_rewrite"]["command"].startswith("ctx run"), command


def test_transparent_capture_preserves_env_and_launcher_wrappers():
    command = (
        "env GE_APP_ID=alpha-evolve-straitjacket "
        "timeout 30 python -m evals.alphaevolve.portfolio wave-policy --run"
    )
    decision = classify_command(command, _policy())
    assert decision["decision"] == "deny"
    assert decision["_rewrite"]["command"] == f"ctx run -- {command}"


def test_github_mutations_keep_permission_boundary():
    for command in ("gh run rerun 123", "gh pr merge 123", "gh issue create"):
        decision = classify_command(command, _policy())
        assert decision["decision"] == "force_ask", command
        assert "_rewrite" not in decision, command


def test_permission_segment_stops_compound_capture():
    for command in ("pwd && gh run rerun 123", "gh pr merge 123 || echo blocked"):
        decision = classify_command(command, _policy())
        assert decision["decision"] == "force_ask", command
        assert "_rewrite" not in decision, command


def test_committed_deny_is_never_rewritten_even_with_auto_steering():
    policy = _policy(deny_commands=["dangertool"])
    for command in ("dangertool", "pwd && dangertool", "dangertool > out 2>&1"):
        decision = classify_command(command, policy)
        assert decision["decision"] == "deny", command
        assert decision["_safety"] == "1", command
        assert "_rewrite" not in decision, command


def test_alphaevolve_massive_command_matrix_is_a_promotion_gate():
    from evals.alphaevolve.guard_policy.command_matrix import run_matrix

    result = run_matrix()
    assert result["cases"] >= 50_000
    assert result["failures"] == 0
    assert result["all_gates_pass"] is True
