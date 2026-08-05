"""Acceptance: ctx py (programmable capture, the Maki absorption).

The contract under test: a Python script chains N operations in one round;
only its bounded digest returns; the script itself is a content-addressed
blob; streams stay span-addressable; failures ride with path-free traceback
evidence; identical script + identical worktree → byte-identical digest.
"""

import json
import subprocess

import pytest


@pytest.fixture()
def ws_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


def test_eval_collapses_computation_to_one_digest(ws_store):
    from ctx.pyeval import run_eval

    ws, store = ws_store
    # Maki's demo shape: heavy internal iteration, tiny emitted result.
    script = (
        "total = sum(len(str(i)) for i in range(100000))\n"
        "print('checksum:', total)\n"
    )
    text, code, _timed_out = run_eval(ws, store, script)
    assert code == 0
    assert text.startswith("[ctx py · script blob:")
    assert "· 2 lines]" in text
    assert "checksum: 488890" in text  # only the emitted result rides


def test_eval_flood_is_bounded_with_addresses(ws_store):
    from ctx.pyeval import run_eval

    ws, store = ws_store
    text, code, _timed_out = run_eval(ws, store, "for i in range(50000): print('line', i)")
    assert code == 0
    assert len(text.encode("utf-8")) < 2000  # ~500 KiB stdout stays out
    assert "run:" in text  # stream evidence stays addressable
    assert "omitted" in text  # declared omission, never silent


def test_eval_script_is_an_addressable_blob(ws_store):
    from ctx.pyeval import run_eval
    from ctx.retrieval import Selector, get

    ws, store = ws_store
    script = "x = 41\nprint('answer:', x + 1)\n"
    text, _, _timed_out = run_eval(ws, store, script)
    blob_short = text.split("script blob:")[1].split(" ")[0]
    fetched = get(store, ws, f"blob:{blob_short}", Selector(lines=(1, 2)))
    assert "x = 41" in fetched and "print('answer:', x + 1)" in fetched


def test_eval_manifest_carries_script_provenance(ws_store):
    from ctx.pyeval import run_eval

    ws, store = ws_store
    text, _, _timed_out = run_eval(ws, store, "print('prov')")
    rid = text.split("[ctx run:")[1].split(" ")[0]
    manifest = store.get_manifest(rid)
    assert manifest["eval"]["script"].startswith("sha256:")
    assert manifest["eval"]["lines"] == 1
    assert manifest["argv"] == ["python3", "-I", "-"]  # normalized, path-free


def test_eval_failure_traceback_is_pathfree_evidence(ws_store):
    from ctx.pyeval import run_eval

    ws, store = ws_store
    text, code, _timed_out = run_eval(ws, store, "raise ValueError('boom-evidence')")
    assert code == 1
    assert "ValueError: boom-evidence" in text  # failure evidence rides
    assert 'File "<stdin>"' in text  # deterministic, path-free frames
    assert "/home/" not in text and "/tmp/" not in text and "/usr/" not in text


def test_eval_deterministic_byte_identical(ws_store):
    from ctx.pyeval import run_eval

    ws, store = ws_store
    script = "print('stable output')"
    first, _, _timed_out = run_eval(ws, store, script)
    second, _, _timed_out = run_eval(ws, store, script)
    assert first == second


def test_eval_timeout_kills_and_reports(ws_store):
    from ctx.pyeval import run_eval

    ws, store = ws_store
    text, code, _timed_out = run_eval(
        ws, store, "import time\ntime.sleep(30)\nprint('never')", timeout=0.5
    )
    assert code == 124
    assert "never" not in text


def test_eval_empty_script_rejected(ws_store):
    from ctx.execution import ExecutionError
    from ctx.pyeval import run_eval

    ws, store = ws_store
    with pytest.raises(ExecutionError):
        run_eval(ws, store, "   \n")


def test_eval_isolated_mode_blocks_cwd_injection(ws_store):
    """`python -I` must not auto-import from the workspace: a sitecustomize
    or same-named module in cwd cannot poison the interpreter."""
    from ctx.pyeval import run_eval

    ws, store = ws_store
    (ws.root / "json.py").write_text("raise SystemExit('poisoned')\n", encoding="utf-8")
    text, code, _timed_out = run_eval(ws, store, "import json\nprint('stdlib json ok')")
    assert code == 0
    assert "stdlib json ok" in text


def test_eval_telemetry_attributed_to_own_verb(ws_store):
    from ctx.pyeval import run_eval

    ws, store = ws_store
    run_eval(ws, store, "print('telemetry')")
    events = [
        json.loads(line)
        for line in (store.audit_dir / "telemetry.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(ev["op"] == "eval" for ev in events)


# ------------------------------------------------------------------ CLI
def test_cli_eval_exit_codes_and_stdin(ws_store, capsys, monkeypatch):
    import io

    from ctx.cli import main

    ws, _ = ws_store
    root = str(ws.root)
    assert main(["--workspace", root, "py", "print('ok')"]) == 0
    assert "ok" in capsys.readouterr().out
    assert main(["--workspace", root, "py", "raise SystemExit(7)"]) == 3
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("print('from-stdin')"))
    assert main(["--workspace", root, "py"]) == 0
    assert "from-stdin" in capsys.readouterr().out


def test_cli_eval_file_mode_confined(ws_store, capsys, tmp_path):
    from ctx.cli import main

    ws, _ = ws_store
    (ws.root / "job.py").write_text("print('from-file')\n", encoding="utf-8")
    assert main(["--workspace", str(ws.root), "py", "--file", "job.py"]) == 0
    assert "from-file" in capsys.readouterr().out
    # Escapes are rejected before any read.
    (tmp_path / "outside.py").write_text("print('escape')\n", encoding="utf-8")
    assert main(["--workspace", str(ws.root), "py", "--file", "../outside.py"]) != 0
    assert "escape" not in capsys.readouterr().out
