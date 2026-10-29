import json
import os
import subprocess
import sys

import pytest

from ctx import anchors, astgrep
from ctx.acp import DEFAULT_COMMANDS
from ctx.mcp import _tool_call
from ctx.mcp_edits import dispatch


@pytest.mark.parametrize("host", DEFAULT_COMMANDS)
def test_real_mcp_anchored_patch_for_every_acp_host(host, workspace_dir):
    path = workspace_dir / "m.py"
    path.write_text("x = 1\n")
    span = anchors.format_span(1, 1, anchors.anchor(["x = 1"]))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": host}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "ctx_edit", "arguments": {"op": "replace", "ref": "repo:m.py",
            "span": span, "replacement": "x = 2\n", "apply": True}}},
    ]
    proc = subprocess.run([sys.executable, "-m", "ctx", "mcp", "--bounded-only", "--with-edits",
                           "--workspace", str(workspace_dir)], input="\n".join(map(json.dumps, requests)) + "\n",
                          text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(l) for l in proc.stdout.splitlines()]
    assert [t["name"] for t in responses[1]["result"]["tools"]] == ["ctx", "ctx_edit"]
    assert not responses[2]["result"]["isError"], responses[2]
    receipt = json.loads(responses[2]["result"]["content"][0]["text"])
    assert receipt["outcome"] == "applied" and receipt["receiptRef"].startswith("blob:")
    assert path.read_text() == "x = 2\n"


def test_edits_are_opt_in_and_workspace_bound(workspace_dir, tmp_path):
    params = {"name": "ctx_edit", "arguments": {"op": "replace"}}
    assert _tool_call(params)["isError"]
    params["arguments"]["workspace"] = str(tmp_path / "elsewhere")
    result = _tool_call(params, edit_workspace=str(workspace_dir))
    assert result["isError"] and "cannot change" in result["content"][0]["text"]


def test_patch_preview_then_stale_apply_refused(workspace_dir):
    path = workspace_dir / "m.py"
    path.write_text("x = 1\n")
    span = anchors.format_span(1, 1, anchors.anchor(["x = 1"]))
    preview = json.loads(dispatch({"op": "replace", "ref": "repo:m.py", "span": span,
                                   "replacement": "x = 2\n"}, str(workspace_dir)))
    assert path.read_text() == "x = 1\n"
    path.write_text("x = 99\n")
    result = _tool_call({"name": "ctx_edit", "arguments": {"op": "apply", "planRef": preview["planRef"]}},
                        edit_workspace=str(workspace_dir))
    assert result["isError"]
    assert path.read_text() == "x = 99\n"


def test_patch_rejects_path_escape(workspace_dir):
    with pytest.raises(Exception):
        dispatch({"op": "replace", "ref": "repo:../outside.py", "span": "1:1@bad",
                  "replacement": "bad"}, str(workspace_dir))


def test_rewrite_uses_preview_and_generation_guard(workspace_dir, monkeypatch):
    # The engine output is deterministic here; real generation/patch application
    # and stale-state refusal run unchanged through the MCP dispatch.
    from test_edit_expansion import fake_engine
    monkeypatch.setattr(astgrep, "binary", lambda: "test-ast-grep")
    monkeypatch.setattr(astgrep, "_run_astgrep", fake_engine)
    path = workspace_dir / "m.py"
    path.write_text("x = 1\n")
    request = {"op": "rewrite", "pattern": "x = 1", "replacement": "x = 2", "language": "python", "glob": "*.py"}
    preview = json.loads(dispatch(request, str(workspace_dir)))
    assert path.read_text() == "x = 1\n"
    applied = json.loads(dispatch({"op": "rewrite_apply", "receiptRef": preview["receiptRef"]}, str(workspace_dir)))
    assert applied["outcome"] == "applied" and path.read_text() == "x = 2\n"
    preview = json.loads(dispatch({**request, "pattern": "x = 2", "replacement": "x = 3"}, str(workspace_dir)))
    path.write_text("x = 99\n")
    with pytest.raises(astgrep.RewriteError, match="generation changed"):
        dispatch({"op": "rewrite_apply", "receiptRef": preview["receiptRef"]}, str(workspace_dir))
    assert path.read_text() == "x = 99\n"
