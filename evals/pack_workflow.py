"""Compare bounded discovery workflows without making model calls.

Run from a checkout containing the baseline revision:
python -m evals.pack_workflow --output evals/pack-workflow-local.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch


def run(baseline: str) -> dict:
    from ctx import pack
    from ctx.callgraph import cmd_callers
    from ctx.retrieval import Selector, get
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    repo = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", baseline + "^{commit}"], cwd=repo, text=True).strip()
    source = subprocess.check_output(["git", "show", f"{revision}:src/ctx/pack.py"], cwd=repo)
    old = types.ModuleType("ctx_pack_baseline")
    sys.modules[old.__name__] = old
    try:
        exec(compile(source, f"{revision}:src/ctx/pack.py", "exec"), old.__dict__)
        with tempfile.TemporaryDirectory(prefix="ctx-pack-workflow-") as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / "ctx.toml").write_text("version = 1\n")
            files = {
                "events.py": "def normalize_event(event):\n    return event.strip()\n\n"
                             "def transform_event(event):\n    return normalize_event(event)\n",
                "consumer.py": "from events import transform_event\n\n"
                               "def consume(event):\n    return transform_event(event)\n",
                "alias_consumer.py": "from events import transform_event as convert\n\n"
                                     "def consume_alias(event):\n    return convert(event)\n",
                "unrelated.py": "def transform_event(event):\n    return event\n\n"
                                "def unrelated(event):\n    return transform_event(event)\n",
            }
            for name, text in files.items():
                (root / name).write_text(text)
            task = "Update transform_event in events.py"
            with patch.dict(os.environ, {"CTX_STATE_HOME": str(Path(tmp) / "state")}):
                ws = resolve_workspace(str(root))
                store = Store(ws.workspace_id)
                old_pack = old.render_pack(old.build_pack(store, ws, task, history=False))
                old_source = get(store, ws, "repo:events.py", Selector(lines=(4, 11)))
                old_callers = cmd_callers(store, ws, "transform_event")
                new_pack = pack.build_pack(store, ws, task, history=False)
                rendered = pack.render_pack(new_pack)
                row = next(r for r in new_pack.rows if r.rel == "events.py")
                checks = {
                    "source_in_bundle": "return normalize_event(event)" in rendered,
                    "caller_in_bundle": "consume repo:consumer.py:4" in rendered,
                    "callee_in_bundle": "normalize_event repo:events.py:1" in rendered,
                    "snapshot_address_in_bundle": "snapshot:" in rendered,
                    "unrelated_caller_excluded": all(r["symbol"] != "unrelated" for r in row.relationships),
                    "baseline_source_and_callers_available": "return normalize_event(event)" in old_source
                    and "consume" in old_callers,
                    "within_estimated_budget": len(rendered) <= new_pack.budget_tokens * 4,
                }
                def account(outputs):
                    return {"scripted_operations": len(outputs),
                            "result_bytes": sum(len(s.encode()) for s in outputs),
                            "estimated_result_tokens": sum((len(s) + 3) // 4 for s in outputs),
                            "outputs_sha256": [hashlib.sha256(s.encode()).hexdigest() for s in outputs]}
                return {"schema": "ctx.pack-workflow/v1", "baseline_commit": revision,
                        "baseline_pack_sha256": hashlib.sha256(source).hexdigest(),
                        "source_sha256": {name: hashlib.sha256((repo / "src/ctx" / name).read_bytes()).hexdigest()
                                          for name in ("pack.py", "callgraph.py", "codeindex.py")},
                        "fixture_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
                        "task": task, "checks": checks, "passed": all(checks.values()),
                        "coverage_observations": {
                            "aliased_import_caller_discovered": any(r["symbol"] == "consume_alias" for r in row.relationships),
                            "relationship_backend": "static callgraph, not language-server resolution"},
                        "baseline_pack_only": account([old_pack]),
                        "baseline_pack_plus_source_and_callers": account([old_pack, old_source, old_callers]),
                        "bundled_pack": account([rendered]), "model_calls": 0,
                        "agent_turns": None, "provider_cost": None,
                        "accounting": "characters/4 estimate; scripted operations are not model turns; baseline uses current shared index and graph backends"}
    finally:
        sys.modules.pop(old.__name__, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="ba61748")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "output": str(args.output), "checks": result["checks"]}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
