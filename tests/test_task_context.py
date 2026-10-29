import io
import json
from types import SimpleNamespace

from conftest import make_store, make_ws
from ctx import task_context
from ctx.digest import digest_output, render_run_digest


def test_prompt_replaces_and_clears_context(monkeypatch, tmp_path):
    path = tmp_path / "task.json"
    monkeypatch.setenv("CTX_TASK_CONTEXT_FILE", str(path))
    monkeypatch.setenv("CTX_TASK_QUERY", "stale parent")
    assert task_context.current_query() is None
    task_context.submit_prompt({"prompt": "cache latency"})
    assert task_context.current_query() == "cache latency"
    task_context.submit_prompt({"prompt": "permissions"})
    assert task_context.current_query() == "permissions"
    task_context.submit_prompt({})
    assert task_context.current_query() is None


def test_bad_prompt_event_clears_old_task(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CTX_TASK_CONTEXT_FILE", str(tmp_path / "task.json"))
    task_context.submit_prompt({"prompt": "old"})
    monkeypatch.setattr("sys.stdin", io.StringIO("malformed"))
    assert task_context.main_prompt() == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert task_context.current_query() is None


def test_automatic_query_override_and_turn_change(state_home, workspace_dir, monkeypatch):
    monkeypatch.delenv("CTX_TASK_CONTEXT_FILE", raising=False)
    monkeypatch.delenv("CTX_TASK_QUERY", raising=False)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = "noise\n" * 10000 + "cache latency p95=400ms\npermissions mode=0644\n"
    _, rid = digest_output(store, ws, "opaque", text)
    manifest = store.get_manifest(rid)
    baseline = render_run_digest(store, ws, manifest, focus="")[0]
    monkeypatch.setenv("CTX_TASK_QUERY", "cache latency")
    auto, meta = render_run_digest(store, ws, manifest)
    assert "text/focus-v2" in auto and "cache latency p95=400ms" in auto
    assert auto == render_run_digest(store, ws, manifest, focus="cache latency")[0]
    assert baseline == render_run_digest(store, ws, manifest, focus="")[0]
    monkeypatch.setenv("CTX_TASK_QUERY", "permissions")
    updated, updated_meta = render_run_digest(store, ws, manifest)
    assert "permissions mode=0644" in updated
    assert meta["digest"]["focusHash"] != updated_meta["digest"]["focusHash"]


def test_orchestrator_sets_subtask_without_parent_or_checkpoint_leak(monkeypatch, tmp_path):
    from ctx.hosts import detect_all
    from ctx.orchestrator import _launch_host

    host = next(h for h in detect_all(which=lambda b: "/bin/" + b) if h.name == "codex")
    monkeypatch.setenv("CTX_TASK_CONTEXT_FILE", "/parent/session.json")
    monkeypatch.setenv("CTX_TASK_QUERY", "parent task")
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("ctx.orchestrator.subprocess.run", run)
    for goal in ("cache latency", "permissions"):
        _launch_host(host, tmp_path, "prompt with private checkpoint", "ctx",
                     timeout=5, task_query=goal)
    assert [env["CTX_TASK_QUERY"] for env in seen] == ["cache latency", "permissions"]
    assert all("CTX_TASK_CONTEXT_FILE" not in env for env in seen)
    assert task_context.current_query() is None  # parent file is still absent


def test_prompt_hook_is_installed():
    from ctx.installer import claude_hook_settings

    hooks = claude_hook_settings("ctx")["hooks"]["UserPromptSubmit"]
    assert hooks[0]["hooks"][0]["command"] == "ctx hook claude-code user-prompt-submit"
