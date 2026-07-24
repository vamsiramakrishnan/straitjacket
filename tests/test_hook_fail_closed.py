"""A broken guard must not become permission (findings C2, C5).

* C2 ``guard.internal_error = "deny"`` -- the ``allow`` fallback was assigned
  *before* the block that loaded the configured value, so an error while
  loading the policy silently discarded the user's explicit ``deny``: the knob
  inverted under exactly the condition it exists for.
* C5 the PreToolUse entry point ran read-stdin, parse, policy-load and
  classification inside one broad ``try`` whose ``except`` was a blanket allow
  with no signal at all.

Every test here fails against the pre-fix module.
"""


from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest


# --------------------------------------------------------------- fixtures
@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    return d


def _run_post(payload: dict, flavor: str = "claude-code") -> dict:
    from ctx import hook

    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = io.StringIO()
    try:
        hook.main_post_tool_use(flavor)
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return json.loads(out)


def _run_pre(payload: dict, flavor: str = "antigravity") -> dict:
    from ctx import hook

    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    sys.stdout = io.StringIO()
    try:
        hook.main_pre_tool_use(flavor)
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return json.loads(out)


def _big(n: int = 60) -> str:
    return json.dumps([{"sha": f"c{i:04d}", "msg": "x" * 400, "n": i} for i in range(n)])


def _mcp_payload(ws, text, tool="mcp__github__list_commits") -> dict:
    return {
        "tool_name": tool,
        "cwd": str(ws),
        "tool_response": [{"type": "text", "text": text}],
    }


def _failures(ws) -> list[dict]:
    path = ws / ".ctx-session-reads" / "guard-failures.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ================================================= C2: internal_error=deny
def _deny_ws(tmp_path):
    d = tmp_path / "deny"
    d.mkdir()
    (d / "ctx.toml").write_text(
        'version = 1\n[guard]\ninternal_error = "deny"\n', encoding="utf-8"
    )
    return d


def _payload(root):
    return {
        "tool_name": "run_command",
        "tool_input": {"CommandLine": "echo hi", "Cwd": str(root)},
        "workspacePaths": [str(root)],
    }


def test_configured_deny_is_not_downgraded_when_policy_load_fails(tmp_path, monkeypatch):
    """The knob must not invert under exactly the condition it exists for."""
    root = _deny_ws(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("policy load exploded")

    monkeypatch.setattr("ctx.hook._load_guard_policy", boom)
    d = _run_pre(_payload(root))
    assert d["decision"] != "allow", d


def test_configured_deny_survives_a_transient_policy_load_failure(tmp_path, monkeypatch):
    """One bad read must not discard an explicit `deny`."""
    from ctx import hook

    root = _deny_ws(tmp_path)
    real = hook._load_guard_policy
    state = {"n": 0}

    def flaky(root_arg):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient")
        return real(root_arg)

    monkeypatch.setattr(hook, "_load_guard_policy", flaky)
    d = _run_pre(_payload(root))
    assert d["decision"] == "deny", d


def test_unreadable_config_is_not_treated_as_allow(tmp_path, monkeypatch):
    """'config says allow' and 'we failed to find out' are different answers."""
    from ctx import hook

    root = tmp_path / "broken"
    root.mkdir()
    # Present but unparseable: we cannot know what the user asked for.
    (root / "ctx.toml").write_bytes(b"\xff\xfe not toml at all [[[\n")

    monkeypatch.setattr(
        hook, "classify",
        lambda p, pol=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    d = _run_pre(_payload(root))
    assert d["decision"] != "allow", d


def test_absent_config_keeps_the_availability_safe_default(tmp_path, monkeypatch):
    """No ctx.toml means the user never asked for deny: stay available."""
    from ctx import hook

    root = tmp_path / "plain"
    root.mkdir()
    monkeypatch.setattr(
        hook, "classify",
        lambda p, pol=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    d = _run_pre(_payload(root))
    assert d["decision"] == "allow", d


# ==================================== C5: narrowed try + internal telemetry
def test_classification_error_is_recorded_with_its_stage(tmp_path, monkeypatch):
    from ctx import hook

    root = _deny_ws(tmp_path)
    monkeypatch.setattr(
        hook, "classify",
        lambda p, pol=None: (_ for _ in ()).throw(ValueError("boom")),
    )
    d = _run_pre(_payload(root))
    assert d["decision"] == "deny"
    rows = _failures(root)
    assert rows, "an internal guard error must leave a signal"
    assert rows[-1]["stage"] == "classify"
    assert rows[-1]["error"] == "ValueError"
    assert rows[-1]["op"] == "pre_tool_use"


def test_input_errors_are_distinguished_from_classification_errors(tmp_path, monkeypatch):
    """The broad try conflated 'bad host payload' with 'broken classifier';
    the narrowed try must label them differently."""
    from ctx import hook

    root = _deny_ws(tmp_path)
    seen = []
    monkeypatch.setattr(
        hook, "_note_guard_failure",
        lambda ws_root, *, op, stage, exc: seen.append(stage),
    )
    _run_pre("{ this is not json ")
    monkeypatch.setattr(
        hook, "classify",
        lambda p, pol=None: (_ for _ in ()).throw(ValueError("boom")),
    )
    _run_pre(_payload(root))
    assert "input" in seen and "classify" in seen, seen


def test_policy_load_failure_has_its_own_stage(tmp_path, monkeypatch):
    from ctx import hook

    root = _deny_ws(tmp_path)
    seen = []
    monkeypatch.setattr(
        hook, "_note_guard_failure",
        lambda ws_root, *, op, stage, exc: seen.append(stage),
    )
    monkeypatch.setattr(
        hook, "_load_guard_policy", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    _run_pre(_payload(root))
    assert "policy" in seen, seen
