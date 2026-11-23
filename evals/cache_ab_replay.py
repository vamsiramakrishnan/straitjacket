"""Fixed tool traces: local delivery measurements, never model task success.

Every arm gets the same initial diagnostic and the same source definitions.
Navigation is scripted with known paths: these traces cannot estimate how a
model chooses searches, repairs a task, or changes the number of turns.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from evals.cache_ab_offline import count_tokens, sha256

ARMS = ("native", "explore", "contain", "full")
SCRIPTS = {
    "quiet_flood": {"diagnostic": "python3 diagnose.py", "sources": {"app/router.py": ["route"]}},
    "keyword_flood": {"diagnostic": "python3 diagnose.py", "sources": {"app/router.py": ["route"]}},
    "traceback": {"diagnostic": "python3 run.py", "sources": {"pipeline.py": ["normalize", "load"]}},
    "bigtest": {"diagnostic": "python3 tests/verify.py", "sources": {"mathlib.py": ["clamp", "is_even", "last", "merge", "sign"]}},
    "multifile": {"diagnostic": "python3 tests/verify.py", "sources": {"api.py": ["make_slug"], "textutil.py": ["slugify"]}},
}


def commands_for(task: str, arm: str) -> list[str]:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    script = SCRIPTS[task]
    if arm in ("explore", "full"):
        navigation = ["ctx map --budget 800", *(
            f"ctx def repo:{path}:{symbol}"
            for path, symbols in script["sources"].items() for symbol in symbols
        )]
    else:
        navigation = ["ls", *(f"cat {path}" for path in script["sources"])]
    return [script["diagnostic"], *navigation]


def normalize_visible(text: str, root: Path) -> str:
    """Remove only opaque identities; retain real arm-dependent map metadata."""
    text = text.replace(str(root), "<fixture>")
    text = re.sub(r"run:[0-9a-f]{12,64}\b", "run:8956c843e556", text)
    return re.sub(r"snapshot:[0-9a-f]{12,64}\b", "snapshot:8956c843e556", text)


def run_replay(out: Path) -> dict:
    from evals.cache_ab_tasks import build_fixture
    from evals.cache_ab_tools import ToolExecutor

    out = Path(out)
    if (out / "script-manifest.json").exists():
        raise FileExistsError(f"scripted replay already exists: {out}")
    transcript_dir = out / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    # Artifact path length must not change traceback clipping. The temp parent
    # is independent of out, and all task/arm directory names have equal width.
    with tempfile.TemporaryDirectory(prefix="cab-") as td:
        for task_index, task in enumerate(SCRIPTS):
            for arm_index, arm in enumerate(ARMS):
                root = Path(td) / f"{task_index:02d}" / f"{arm_index:02d}"
                fixture = build_fixture(task, root)
                executor = ToolExecutor(fixture, arm, Path(td) / f"s{task_index:02d}{arm_index:02d}")
                trace = []
                for command in commands_for(task, arm):
                    result = executor.execute("shell", {"command": command})
                    if result.get("error"):
                        raise RuntimeError(f"scripted {task}/{arm} command failed: {command}: {result['error']}")
                    text = result["text"]
                    normalized = normalize_visible(text, root)
                    trace.append({"tool": "shell", "arguments": {"command": command}, "result": result,
                                  "visible_tokens_actual": count_tokens(text),
                                  "visible_tokens_normalized": count_tokens(normalized),
                                  "normalized_sha256": sha256(normalized.encode())})
                relative = f"transcripts/{task}-{arm}.jsonl"
                payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in trace)
                (out / relative).write_text(payload, encoding="utf-8")
                rows.append({"task": task, "arm": arm, "fixture": fixture.manifest(),
                             "transcript": relative, "transcript_sha256": sha256(payload.encode()),
                             "tool_calls": len(trace),
                             "visible_tokens_actual": sum(row["visible_tokens_actual"] for row in trace),
                             "visible_tokens_normalized": sum(row["visible_tokens_normalized"] for row in trace),
                             "tool_wall_s": sum(row["result"]["elapsed_s"] for row in trace),
                             "provider_tokens": None, "cache_reads": None, "cache_writes": None,
                             "model_task_success": None, "provider_cost_usd": None})
    result = {
        "schema": "straitjacket.cache-ab.replay/v1", "provider_calls": 0,
        "scripts": SCRIPTS, "arms": list(ARMS), "cells": rows,
        "normalization": "replace candidate absolute path and opaque run/snapshot IDs only; sum each tool output separately",
        "scope": "Fixed traces over unchanged buggy source. No repairs or adaptive model exploration. "
                 "Tool output tokens are not provider input tokens or conversation residency. "
                 "Containment may legitimately add recent-run evidence to ctx map.",
    }
    (out / "script-manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
