"""Wire contract, resource bounds and provider-accounting invariants."""
import json
from pathlib import Path
import signal
import sys

import pytest
import jsonschema

from conftest import make_store, make_ws
from ctx.cli import main
from ctx.tooltelemetry import usage_record

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "spec/schemas/command-result-v1.schema.json").read_text())


@pytest.mark.parametrize("code,rc,status", [("print('hi')",0,0),("raise SystemExit(4)",3,4),("import os,signal; os.kill(os.getpid(),signal.SIGTERM)",3,None)])
def test_json_command_contract(state_home, workspace_dir, capsys, code, rc, status):
    assert main(["--workspace",str(workspace_dir),"run","--json","--",sys.executable,"-c",code]) == rc
    output = capsys.readouterr()
    assert not output.err
    result = json.loads(output.out)
    jsonschema.validate(result,SCHEMA)
    assert result["command_exit_code"] == status
    if status is None:
        assert result["signal"] == "SIGTERM"


def test_json_launch_error_never_claims_command_exit(state_home, workspace_dir, capsys):
    assert main(["--workspace",str(workspace_dir),"run","--json","--","sj-does-not-exist"]) == 127
    result = json.loads(capsys.readouterr().out)
    jsonschema.validate(result,SCHEMA)
    assert result["ref"] is None and result["command_exit_code"] is None
    assert result["error"] == "launch_failed"


@pytest.mark.parametrize("flag", ["--bg","--passthrough","--bg-after"])
def test_json_rejects_ambiguous_execution_before_launch(state_home,workspace_dir,capsys,flag):
    args = [flag,"1"] if flag=="--bg-after" else [flag]
    assert main(["--workspace",str(workspace_dir),"run","--json",*args,"--","touch","not-created"]) == 2
    assert not (workspace_dir / "not-created").exists()


def test_native_signal_mapping_does_not_change_manifest_contract(state_home,workspace_dir,capsys):
    assert main(["--workspace",str(workspace_dir),"run","--passthrough","--",sys.executable,"-c","import os,signal; os.kill(os.getpid(),signal.SIGTERM)"]) == 143


def test_digest_parse_cap_bounds_the_blob_read(state_home,workspace_dir,monkeypatch):
    import ctx.digest.base as base
    from ctx.execution import run_capture
    ws=make_ws(workspace_dir); store=make_store(ws)
    try:
        capture=run_capture(ws,[sys.executable,"-c","print('x' * 10000)"],store=store)
        monkeypatch.setattr(base,"MAX_PARSE_BYTES",128)
        # If the implementation ever regresses to get_blob() then slice,
        # this fails without allocating a multi-GiB synthetic artifact.
        monkeypatch.setattr(store,"get_blob",lambda _id: pytest.fail("unbounded blob read"))
        ctx=base.DigestContext.load(store,ws,capture.manifest,focus=None)
        assert len(ctx.stdout.text.encode()) == 128
        assert ctx.stdout.parsed_fully is False and ctx.stdout.bytes == 10001
        assert ctx.stderr.parsed_fully is True
    finally:
        store.close()


def test_usage_semantics_are_explicit_and_missing_is_unknown():
    raw={"prompt_token_count":100,"cached_content_token_count":80,"candidates_token_count":10,"thoughts_token_count":20,"total_token_count":130}
    unknown=usage_record(raw)
    assert unknown["derived"]["gross_input"] is None and unknown["raw"] == raw
    assert usage_record(raw,input_semantics="includes_cache")["derived"]["uncached_input"] == 20
    assert usage_record(raw,input_semantics="excludes_cache")["derived"]["gross_input"] == 180
    assert usage_record({})["derived"]["candidate_output"] is None
    invalid=usage_record({**raw,"cached_content_token_count":200},input_semantics="includes_cache")
    assert invalid["accounting_error"] == "cached_exceeds_inclusive_prompt"
    assert invalid["derived"]["gross_input"] is None
    assert usage_record({"prompt_token_count":True})["accounting_error"] == "invalid_counter"


def test_model_free_replay_checks_status_and_quiet_retrieval(tmp_path):
    from evals.tool_boundary_replay import evaluate
    root=tmp_path/"workspace";root.mkdir();(root/"ctx.toml").write_text("version=1\n")
    report=evaluate(root,tmp_path/"state")
    assert report["passed"] and report["provider_calls"]==0
    assert {r["auto_delivery"] for r in report["rows"]} == {"inline","digest"}


def test_json_escaping_cannot_exceed_the_wire_budget(state_home, workspace_dir):
    import asyncio
    from ctx.sdk import ToolSession
    result = asyncio.run(ToolSession(workspace_dir, max_tokens=64).run([sys.executable, "-c", "print(chr(0x4e00) * 4000)"], delivery="digest"))
    assert len(result.to_json().encode()) <= 512
    assert "omitted digest tail" in result.digest and result.ref in result.digest
    jsonschema.validate(result.as_dict(), SCHEMA)
