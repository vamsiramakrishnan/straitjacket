"""Run generated native plugins with each host's actual callback shape."""
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from ctx.native_hooks import render, decision_for


@pytest.mark.parametrize("host", ["hermes", "omp", "opencode", "dsh"])
def test_native_guard_and_emission_gate(host, workspace_dir):
    name = "terminal" if host == "hermes" else "bash"
    payload = {"cwd": str(workspace_dir), "tool_name": name, "tool_input": {"command": "find . -type f"}}
    pre = subprocess.run([sys.executable, "-m", "ctx", "hook", host, "pre-tool-use"],
                         input=json.dumps(payload), text=True, capture_output=True, timeout=20)
    decision = json.loads(pre.stdout)
    assert decision["action"] == ("block" if host == "dsh" else "rewrite"), decision
    payload["tool_response"] = "repeated output\n" * 3000
    post = subprocess.run([sys.executable, "-m", "ctx", "hook", host, "post-tool-use"],
                          input=json.dumps(payload), text=True, capture_output=True, timeout=20)
    result = json.loads(post.stdout)
    assert len(result["output"]) < len(payload["tool_response"])
    assert "ctx" in result["output"]


def test_hermes_plugin_translates_callbacks(workspace_dir, monkeypatch):
    monkeypatch.chdir(workspace_dir)
    path = workspace_dir / "plugin.py"
    path.write_text(render("hermes", shlex.join([sys.executable, "-m", "ctx"])))
    spec = importlib.util.spec_from_file_location("hermes_test", path)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    hooks = {}
    class Context:
        def register_hook(self, name, callback):
            hooks[name] = callback
    plugin.register(Context())
    result = hooks["pre_tool_call"]("terminal", {"command": "find . -type f"})
    assert result["action"] == "modify"
    assert "ctx" in result["args"]["command"]
    assert hooks["transform_tool_result"]("terminal", {"command": "echo hi"}, "hi") is None
    assert len(hooks["transform_tool_result"]("terminal", {}, "x\n" * 20000)) < 40000


@pytest.mark.parametrize("host", ["omp", "opencode", "dsh"])
def test_generated_javascript_callbacks(host, workspace_dir):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute native plugin contract")
    path = workspace_dir / f"{host}.mjs"
    path.write_text(render(host, shlex.join([sys.executable, "-m", "ctx"])))
    script = workspace_dir / "exercise.mjs"
    script.write_text('''
import assert from 'node:assert/strict';
const mod = await import(process.argv[2]);
const host = process.argv[3];
const cwd = process.cwd();
const args = {command: 'find . -type f'};
const large = 'raw output\\n'.repeat(5000);
if (host === 'omp') {
  const hooks = {};
  mod.default({on: (name, cb) => hooks[name] = cb});
  const event = {toolName: 'bash', input: args, toolCallId: 'one'};
  const pre = await hooks.tool_call(event, {cwd});
  assert.match(pre.input.command, /ctx/);
  const post = await hooks.tool_result({...event, content:[{type:'text',text:large},{type:'image',data:'keep'}]}, {cwd});
  assert.ok(post.content[0].text.length < large.length);
  assert.equal(post.content[1].data, 'keep');
} else if (host === 'opencode') {
  const hooks = await mod.StraitjacketPlugin({directory:cwd});
  const event = {tool:'bash', sessionID:'s', callID:'one', args};
  const original = {...args};
  const output = {args:original};
  await hooks['tool.execute.before'](event, output);
  assert.match(output.args.command, /ctx/);
  assert.match(original.command, /ctx/); // host executes the original object
  const result = {output:large, metadata:{keep:true}, title:'original'};
  await hooks['tool.execute.after'](event, result);
  assert.ok(result.output.length < large.length);
  assert.equal(result.metadata.keep, true);
  assert.equal(result.title, 'original');
} else {
  const hooks = {};
  mod.apply({on:(name, cb) => hooks[name] = cb});
  const exec = {name:'bash', arguments:args, callId:'one'};
  const pre = await hooks['tools/pre-execute'](exec, () => ({kind:'allow'}));
  assert.equal(pre.kind, 'deny');
  assert.match(pre.reason, /Retry with/);
  const result = {content:[{type:'text',text:large}], isError:false};
  const post = await hooks['tools/post-execute'](exec, result, () => ({kind:'accept', additionalContexts:['keep']}));
  assert.ok(post.content[0].text.length < large.length);
  assert.deepEqual(post.additionalContexts, ['keep']);
  const blocked = {kind:'block',feedback:[{type:'text',text:'downstream denied'}]};
  assert.equal(await hooks['tools/post-execute'](exec, result, () => blocked), blocked);
}
''')
    result = subprocess.run([node, str(script), path.as_uri(), host], cwd=workspace_dir,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_worker_plugin_is_removed_before_patch_capture(tmp_path):
    from ctx.native_hooks import PATHS, worker_files
    source, worker = tmp_path / "source", tmp_path / "worker"
    source.mkdir()
    worker.mkdir()
    (source / "ctx.toml").write_text("version = 1\n")
    with worker_files("opencode", worker, source):
        assert (worker / PATHS["opencode"]).is_file()
        assert (worker / "ctx.toml").read_text() == "version = 1\n"
    assert list(worker.iterdir()) == []


def test_worker_preserves_existing_plugin(tmp_path):
    from ctx.native_hooks import PATHS, worker_files
    path = tmp_path / PATHS["omp"]
    path.parent.mkdir(parents=True)
    path.write_text("user settings")
    with worker_files("omp", tmp_path):
        assert path.read_text() == "user settings"
    assert path.read_text() == "user settings"


def test_conflicting_native_plugin_refuses_before_mcp_write(workspace_dir):
    from conftest import make_ws
    from ctx import mcp_hosts
    from ctx.native_hooks import PATHS
    path = workspace_dir / PATHS["opencode"]
    path.parent.mkdir(parents=True)
    path.write_text("user-owned plugin")
    with pytest.raises(mcp_hosts.IntegrationError, match="unmanaged"):
        mcp_hosts.install("opencode", make_ws(workspace_dir))
    assert not (workspace_dir / "opencode.json").exists()
    assert path.read_text() == "user-owned plugin"
