"""The public command path and the existing independent patch evaluator."""
import json
import re
import sys

from conftest import make_store, make_ws
from ctx.cli import main
from ctx.semantic import prepare, run
from ctx.semantic.evidence import read_document
from ctx.semantic.worker import WorkerResult


def test_prepare_execute_inspect_and_resume_through_cli(workspace_dir, state_home, tmp_path, capsys):
    source = workspace_dir / "logic.py"
    source.write_text("def answer():\n    return 42\n")
    marker = tmp_path / "calls"
    driver = tmp_path / "worker.py"
    driver.write_text("""import json, pathlib, sys
request = json.load(sys.stdin)
with pathlib.Path(sys.argv[1]).open('a') as f: f.write('call\\n')
evidence = request['evidence']
print(json.dumps({'schema': 'ctx.semantic.response/v1', 'findings': [
    {'summary': 'Returns 42', 'support': [{'ref': evidence['ref'],
    'lines': [evidence['start'], evidence['end']]}], 'counterevidence': []}],
    'unresolved': [], 'usage': {'cost_usd': 0.01}}))
""")
    (workspace_dir / "request.json").write_text(json.dumps({
        "question": "What does answer return?", "sources": ["repo:logic.py"],
        "worker": {"identity": "fixture-1", "model": "fixture",
                   "command": [sys.executable, str(driver), str(marker)]}}))
    base = ["--workspace", str(workspace_dir), "semantic"]
    assert main(base + ["prepare", "request.json"]) == 0
    output = capsys.readouterr().out
    assert "no model invoked" in output
    handle = re.search(r"blob:[0-9a-f]{64}", output).group()
    assert not marker.exists()
    assert main(base + ["show", handle]) == 0
    assert not marker.exists()
    capsys.readouterr()
    assert main(base + ["run", handle]) == 0
    output = capsys.readouterr().out
    assert "model inference" in output and "Returns 42" in output
    assert "selection completeness: unknown" in output
    assert len(output.encode()) < 6000
    report_ref = re.search(r"report: (blob:[0-9a-f]{64})", output).group(1)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    try:
        report = read_document(store, report_ref)
        assert report["totals"]["input_tokens"] is None
        assert report["totals"]["calls"] == 1
    finally:
        store.close()
    assert main(base + ["resume", handle]) == 0
    assert marker.read_text() == "call\n"
    assert source.read_text() == "def answer():\n    return 42\n"


def test_semantic_findings_do_not_override_independent_patch_acceptance():
    from evals.edit_matrix import run_matrix
    case = {"id": "semantic-scope", "shape": "mechanical", "task": "Return 2", "targets": ["m.py"],
            "files": {"m.py": "def answer(): return 1\n"},
            "oracle": "from m import answer\nassert answer() == 2\n"}
    def adapter(ws, store, request_path, metrics_path):
        spec = {"question": "Does this function return 2?", "sources": ["repo:m.py"],
                "worker": {"identity": "lying-fixture", "model": "fixture", "command": ["unused"]}}
        def lie(data, **kwargs):
            e = json.loads(data)["evidence"]
            return WorkerResult(json.dumps({"schema": "ctx.semantic.response/v1", "findings": [{
                "summary": "The requested patch is correct", "support": [{"ref": e["ref"], "lines": [1, 1]}],
                "counterevidence": []}], "unresolved": []}).encode())
        _, report = run(ws, store, prepare(ws, store, spec), worker=lie)
        assert report["coverage"]["processing_complete"]
        if json.loads(request_path.read_text())["format"] == "correct":
            (ws.root / "m.py").write_text("def answer(): return 2\n")
        return {}
    rows = list(run_matrix([case], {"semantic": adapter, "correct": adapter},
                           model="fixture", measurement="fixture"))
    assert {r["format"]: r["task_success"] for r in rows} == {"semantic": False, "correct": True}
    assert all(r["measurement"] == "fixture" and r["cost_usd"] is None for r in rows)
