"""Paired edit-format trials with an evaluator-owned behavioral oracle.

No API keys are needed for --fixture. For live trials, --adapter FORMAT=COMMAND
runs a caller-configured host driver in each fresh workspace. The driver reads
CTX_EVAL_REQUEST and optionally writes usage JSON to CTX_EVAL_METRICS. The
oracle stays outside the editable workspace and is checked for tampering.
This is benchmark hygiene, not an OS sandbox or a network-contamination guard.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ctx.anchors import anchor
from ctx.edit_transactions import replace_span
from ctx.execution import run_capture
from ctx.store import Store, canonical_json
from ctx.sessiondir import LEDGER_DIR_NAME
from ctx.workspace import resolve_workspace


def _snapshot(ws):
    return {p: hashlib.sha256(ws.confine(p, must_exist=True).read_bytes()).hexdigest()
            for p in ws.list_files() if p != "ctx.toml" and LEDGER_DIR_NAME not in Path(p).parts
            and not ws.is_ignored(p)}


def _check(ws, store, oracle):
    captured = run_capture(ws, [sys.executable, "-I", "-B", str(oracle)], store=store, timeout=30,
                           record_argv=["python3", "-I", "-B", "<acceptance>"])
    result = captured.manifest["result"]
    return result["exitCode"] == 0 and not result["timedOut"], "run:" + captured.manifest_id


def run_matrix(cases, adapters, *, model, repeats=1, measurement="live"):
    """adapters[name](ws, store, request_path, metrics_path) -> usage dict.

    Costs and token counts remain null when the host does not report them.
    Pass/fail and changed-file scope come from the evaluator, never the driver.
    """
    if measurement not in {"live", "fixture"} or not 1 <= repeats <= 10:
        raise ValueError("invalid measurement or repetition count")
    if not cases or len(cases) > 1000 or not adapters or len(adapters) > 8:
        raise ValueError("matrix needs bounded cases and adapters")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("case ids must be unique")
    for case in cases:
        case_hash = hashlib.sha256(canonical_json(case)).hexdigest()
        for repeat in range(repeats):
            # Alternate order; every arm still starts in a fresh workspace.
            order = sorted(adapters, reverse=bool(repeat % 2))
            for fmt in order:
                with tempfile.TemporaryDirectory(prefix="ctx-edit-eval-") as directory:
                    parent = Path(directory)
                    root = parent / "workspace"
                    root.mkdir()
                    (root / "ctx.toml").write_text("version = 1\n")
                    ws = resolve_workspace(str(root))
                    for path, contents in case["files"].items():
                        if path == "ctx.toml":
                            raise ValueError("case cannot override evaluator configuration")
                        target = ws.confine(path)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(contents.encode("utf-8"))
                    oracle = parent / "acceptance.py"
                    oracle.write_text("import os, sys\nsys.path.insert(0, os.getcwd())\n" + case["oracle"])
                    oracle_hash = hashlib.sha256(oracle.read_bytes()).hexdigest()
                    request = parent / "request.json"
                    request.write_bytes(canonical_json({"case": case["id"], "task": case["task"],
                                                        "format": fmt, "model": model,
                                                        "targets": case.get("targets", list(case["files"]))}))
                    metrics = parent / "metrics.json"
                    store = Store(ws.workspace_id)
                    try:
                        baseline_passed, baseline_ref = _check(ws, store, oracle)
                        before = _snapshot(ws)
                        started = time.monotonic()
                        failure = None
                        try:
                            usage = adapters[fmt](ws, store, request, metrics) or {}
                        except Exception as exc:
                            usage, failure = {}, type(exc).__name__
                        elapsed = (time.monotonic() - started) * 1000
                        tampered = not oracle.exists() or hashlib.sha256(oracle.read_bytes()).hexdigest() != oracle_hash
                        try:
                            after = _snapshot(ws)
                            changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
                            wrong = bool(changed - set(case.get("targets", case["files"])))
                        except Exception:
                            wrong = True
                        passed, acceptance_ref = (False, None) if tampered else _check(ws, store, oracle)
                        # Do not train policy on cases whose oracle already passed.
                        valid = not baseline_passed and not tampered
                        row = {"schema": "ctx.edit-trial/v1", "case": case["id"], "caseHash": case_hash,
                               "repeat": repeat, "format": fmt, "model": model, "shape": case["shape"],
                               "measurement": measurement if valid else "invalid_case",
                               "task_success": valid and passed and not wrong and failure is None,
                               "wrong_target": wrong or tampered, "duration_ms": round(elapsed, 3),
                               "baselineRef": baseline_ref, "acceptanceRef": acceptance_ref,
                               "driverFailure": failure}
                        for key in ("cost_usd", "input_tokens", "cached_input_tokens", "retrieval_calls", "edit_retries"):
                            row[key] = usage.get(key)
                        yield row
                    finally:
                        store.close()


def command_adapter(command):
    """Use a host driver without shell interpolation or global env mutation."""
    def run(ws, store, request, metrics):
        # run_capture has no env override. A tiny explicit exec wrapper passes
        # only the two per-run paths; it is captured and bounded like any command.
        wrapper = ("import os,sys; os.environ['CTX_EVAL_REQUEST']=sys.argv[1]; "
                   "os.environ['CTX_EVAL_METRICS']=sys.argv[2]; os.execvp(sys.argv[3],sys.argv[3:])")
        result = run_capture(ws, [sys.executable, "-I", "-c", wrapper, str(request), str(metrics), *command],
                             timeout=600, store=store, record_argv=["<edit-eval-driver>", *command])
        if result.manifest["result"]["exitCode"] != 0 or result.manifest["result"]["timedOut"]:
            raise RuntimeError("adapter did not complete")
        if not metrics.exists():
            return {}
        if metrics.stat().st_size > 8192:
            raise ValueError("adapter metrics exceed 8 KiB")
        value = json.loads(metrics.read_text())
        if not isinstance(value, dict):
            raise ValueError("adapter metrics must be an object")
        return value
    return run


def fixture():
    cases = [{"id": f"assignment-{i}", "shape": "mechanical", "task": f"Set value to {i+1}.",
              "files": {"m.py": f"value = {i}\n"}, "targets": ["m.py"],
              "oracle": f"from m import value\nassert value == {i+1}\n"} for i in range(6)]

    def adapter(ws, store, request, metrics):
        fmt = json.loads(request.read_text())["format"]
        p = ws.root / "m.py"
        before = p.read_text()
        value = int(before.split("=")[1])
        after = f"value = {value+1}\n"
        if fmt == "anchored":
            replace_span(ws, store, "m.py", "1:1@" + anchor(before.splitlines()), after, apply=True)
        else:
            p.write_text(before.replace(before, after))
        return {}  # No invented model cost or usage.
    return cases, {"native": adapter, "anchored": adapter}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--cases", type=Path, help="JSON list: id, shape, task, files, targets, oracle")
    parser.add_argument("--adapter", action="append", default=[], help="FORMAT=COMMAND, repeat per arm")
    parser.add_argument("--model")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.fixture:
        cases, adapters = fixture()
        model, measurement = "fixture-no-model", "fixture"
    else:
        if not args.cases or not args.model or len(args.adapter) < 2:
            parser.error("live trials require --cases, --model, and at least two --adapter entries")
        cases = json.loads(args.cases.read_text())
        adapters = {}
        for item in args.adapter:
            name, sep, cmd = item.partition("=")
            if not sep or not cmd or name in adapters:
                parser.error("adapters must have distinct FORMAT=COMMAND entries")
            adapters[name] = command_adapter(shlex.split(cmd))
        model, measurement = args.model, "live"
    with args.out.open("w") as output:
        for row in run_matrix(cases, adapters, model=model, repeats=args.repeats, measurement=measurement):
            output.write(json.dumps(row, sort_keys=True) + "\n")
            output.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
