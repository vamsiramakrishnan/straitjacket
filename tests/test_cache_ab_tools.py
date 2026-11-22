import os
import shutil
from pathlib import Path

import pytest

from evals.cache_ab_tasks import build_fixture
from evals.cache_ab_tools import ARMS, TOOLS, ToolExecutor, child_env


@pytest.fixture
def executor(tmp_path):
    return ToolExecutor(build_fixture("quiet_flood", tmp_path / "candidate"), "full", tmp_path / "state")


def test_all_arms_share_identical_schemas(tmp_path):
    for index, arm in enumerate(ARMS):
        worker = ToolExecutor(build_fixture("quiet_flood", tmp_path / str(index)), arm, tmp_path / f"state{index}")
        assert worker.tools == TOOLS


def test_child_env_excludes_provider_credentials_and_python_injection(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "PYTHONPATH", "PYTHONOPTIMIZE", "BASH_ENV"):
        monkeypatch.setenv(name, "must-not-leak")
    env = child_env(tmp_path / "state")
    assert "must-not-leak" not in env.values()
    assert env["CTX_PICKER_MODE"] == "local"
    assert env["CTX_MAP_ENGINE"] == "builtin"


@pytest.mark.parametrize("command", [
    "cat /etc/passwd", "cat ../source", "cat tests/verify.py ; cat /etc/passwd",
    "python3 -c 'print(1)'", "python3 diagnose.py | cat", "sed -i s/old/new/ app/router.py",
    "ctx run -- python3 diagnose.py", "ctx def repo:../x.py:a", "rg --pre sh foo",
])
def test_unsupported_commands_are_rejected(executor, command):
    assert executor.execute("shell", {"command": command})["error"]


def test_write_whitelist(executor):
    for path in ("tests/verify.py", "diagnose.py", "ctx.toml", "../escape.py"):
        assert executor.execute("write_file", {"path": path, "content": "pass\n"})["error"]
    assert not executor.execute("write_file", {"path": "app/router.py", "content": "def route(x): return x\n"})["error"]


def test_native_output_is_verbatim_and_containment_keeps_real_capture(tmp_path):
    native = ToolExecutor(build_fixture("quiet_flood", tmp_path / "00"), "native", tmp_path / "state0")
    contain = ToolExecutor(build_fixture("quiet_flood", tmp_path / "01"), "contain", tmp_path / "state1")
    raw = native.execute("shell", {"command": "python3 diagnose.py"})
    digest = contain.execute("shell", {"command": "python3 diagnose.py"})
    assert raw["exit_code"] == digest["exit_code"] == 0
    assert not raw["error"] and not digest["error"]
    assert len(raw["text"].splitlines()) == 3000
    assert "circuit OPEN" in digest["text"]
    assert len(digest["text"]) < len(raw["text"]) / 10
    assert digest["raw_bytes"] == len(raw["text"].encode())
    fetched = contain.execute("shell", {"command": f"ctx get {digest['captured_ref']} --lines 1636:1638"})
    assert not fetched["error"]
    assert "circuit OPEN" in fetched["text"]
    searched = contain.execute("shell", {"command": f"ctx search {digest['captured_ref']}#stdout 'circuit OPEN' --context 1"})
    assert not searched["error"]
    assert "circuit OPEN" in searched["text"]


def test_capture_reports_underlying_failure_exit(tmp_path):
    worker = ToolExecutor(build_fixture("traceback", tmp_path / "candidate"), "full", tmp_path / "state")
    result = worker.execute("shell", {"command": "python3 run.py"})
    assert not result["error"]
    assert result["exit_code"] == 1
    retrieved = worker.execute("shell", {"command": f"ctx get {result['captured_ref']}#stderr --lines 1:20"})
    assert not retrieved["error"]
    assert "KeyError" in retrieved["text"]


def test_containment_applies_to_native_reads_as_well_as_diagnostics(tmp_path):
    worker = ToolExecutor(build_fixture("quiet_flood", tmp_path / "candidate"), "contain", tmp_path / "state")
    result = worker.execute("shell", {"command": "cat app/router.py"})
    assert not result["error"]
    assert result["captured_ref"].startswith("run:")
    assert result["raw_bytes"] == len(worker.fixture.initial["app/router.py"])
    assert "def route" in result["text"]


def test_exploration_and_retrieval_are_assigned_by_arm(tmp_path):
    for index, arm in enumerate(ARMS):
        worker = ToolExecutor(build_fixture("quiet_flood", tmp_path / str(index)), arm, tmp_path / f"state{index}")
        result = worker.execute("shell", {"command": "ctx def repo:app/router.py:route"})
        assert bool(result["error"]) == (arm not in ("explore", "full"))
        if not result["error"]:
            assert "def route" in result["text"]


def test_output_overflow_is_a_failed_tool_not_truncation(tmp_path):
    worker = ToolExecutor(build_fixture("quiet_flood", tmp_path / "candidate"), "native", tmp_path / "state", output_limit_bytes=100)
    result = worker.execute("shell", {"command": "python3 diagnose.py"})
    assert result["error"] == "output_limit"
    assert result["text"].startswith("Tool failed:")


def test_timeout_kills_looping_candidate(tmp_path):
    worker = ToolExecutor(build_fixture("quiet_flood", tmp_path / "candidate"), "native", tmp_path / "state", timeout_s=0.2)
    (worker.fixture.root / "app/router.py").write_text("while True: pass\n")
    result = worker.execute("shell", {"command": "python3 tests/verify.py"})
    assert result["error"] == "timeout"
    assert result["elapsed_s"] < 2


def test_contained_timeout_reaps_separate_capture_process_group(tmp_path):
    worker = ToolExecutor(build_fixture("quiet_flood", tmp_path / "candidate"), "contain", tmp_path / "state", timeout_s=0.7)
    pidfile = worker.state_dir / "test-child-pid"
    (worker.fixture.root / "app/router.py").write_text(
        "import os, pathlib\n"
        "pathlib.Path(os.environ['CTX_STATE_HOME'], 'test-child-pid').write_text(str(os.getpid()))\n"
        "while True: pass\n")
    result = worker.execute("shell", {"command": "python3 tests/verify.py"})
    assert result["error"] == "timeout"
    assert result["capture_command_timeout_s"] < result["tool_timeout_s"]
    assert pidfile.exists(), "the test must actually exercise the captured child"
    pid = int(pidfile.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        # Do not leave a hanging process behind even when the regression fails.
        os.kill(pid, 9)
        pytest.fail("ctx's captured child survived the tool deadline")


def test_tool_rejects_symlink_even_after_fixture_build(executor):
    source = executor.fixture.root / "app/router.py"
    source.unlink()
    source.symlink_to(executor.fixture.root / "tests/verify.py")
    assert executor.execute("shell", {"command": "cat app/router.py"})["error"]
