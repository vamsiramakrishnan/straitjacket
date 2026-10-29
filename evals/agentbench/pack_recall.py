"""Does `ctx pack` point at the files the fix actually touches?

For each DeepSWE task the reference solution (`solution/solution.patch`) is
the ground truth: the source files it changes are the files a good context
pack must surface. This measures, per task and in aggregate, recall@k of
those files and the rank of the first one, for three rankers:

  grep-count   the Cody/BM25-shaped baseline — files ranked by how many of
               the task's identifiers they contain (no rarity, no symbols,
               no history)
  pack-nohist  ctx pack without the history signal
  pack         ctx pack, all signals

No model runs; a checkout is materialized per task (no venv), the pack is
built against it, and the checkout is deleted. Usage:

  python3 evals/agentbench/pack_recall.py [--ids a,b] [--top 8] [--out results/pack_recall.json]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "src"))

VALIDATED = [
    "adaptix-name-mapping-aliases", "bandit-incremental-cache-control",
    "bandit-interprocedural-taint-checks", "cattrs-partial-structuring-recovery",
    "ipython-session-bundle-replay", "kombu-single-active-consumer-priority",
    "mashumaro-flattened-dataclass-fields", "mobly-grouped-test-barriers",
    "returns-validated-error-accumulation", "sqlite-utils-safe-import-checkpoints",
    "textual-kitty-key-phases", "textual-richlog-follow-state",
    "tomlkit-toml-table-converters", "vulture-persistent-analysis-cache",
]

_TEST_PATH_RE = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*$|_test\.py$|(^|/)conftest\.py$")


def _gold_files(task: dict) -> tuple[list[str], list[str]]:
    from adapters.deepswe import _patch_paths

    text = (pathlib.Path(task["dir"]) / "solution" / "solution.patch").read_text(encoding="utf-8",
                                                                                   errors="replace")
    paths = _patch_paths(text)
    src = [p for p in paths if not _TEST_PATH_RE.search(p)]
    tests = [p for p in paths if _TEST_PATH_RE.search(p)]
    return src, tests


def _grep_count_rank(root: pathlib.Path, task_text: str, rels: list[str]) -> list[str]:
    """Files ranked by the number of distinct task identifiers they contain
    (case-insensitive substring), ties by path. The keyword baseline."""
    from ctx.pack import extract_terms

    terms, _paths = extract_terms(task_text)
    keys = [t.key for t in terms]
    scored = []
    for rel in rels:
        try:
            text = (root / rel).read_bytes().lower()
        except OSError:
            continue
        if b"\x00" in text[:8192]:
            continue
        n = sum(1 for k in keys if k.encode() in text)
        if n:
            scored.append((-n, rel))
    scored.sort()
    return [rel for _n, rel in scored]


def _pack_rank(root: pathlib.Path, state: pathlib.Path, task_file: pathlib.Path, *,
               history: bool, top: int) -> tuple[list[str], float, dict]:
    argv = [sys.executable, "-m", "ctx", "--workspace", str(root), "pack", f"@{task_file.name}",
            "--json", "--files", str(top)]
    if not history:
        argv.append("--no-history")
    t0 = time.monotonic()
    proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, timeout=600,
                          env={**os.environ, "CTX_STATE_HOME": str(state)})
    wall = time.monotonic() - t0
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:])
    doc = json.loads(proc.stdout)
    return [f["file"] for f in doc["files"]], wall, doc


def _metrics(ranked: list[str], gold: list[str], top: int) -> dict:
    gold_set = set(gold)
    first = next((i + 1 for i, r in enumerate(ranked) if r in gold_set), None)
    at = lambda k: (len([r for r in ranked[:k] if r in gold_set]) / len(gold)) if gold else None  # noqa: E731
    return {"recall@5": at(5), f"recall@{top}": at(top), "first_hit": first,
            "rr": (1.0 / first) if first else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=",".join(VALIDATED))
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--out", default=str(HERE / "results" / "pack_recall.json"))
    ap.add_argument("--work-root", default=None)
    ap.add_argument("--keep", action="store_true")
    ns = ap.parse_args()

    from adapters import deepswe

    work_root = pathlib.Path(ns.work_root or (pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "pack-recall")).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    tasks = deepswe.load(0, ids=ns.ids)
    rows = []
    for task in tasks:
        tid = task["id"]
        workdir = work_root / tid
        state = work_root / f"{tid}.state"
        try:
            deepswe._materialize(task, workdir)
            (workdir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
            task_text = (pathlib.Path(task["dir"]) / "instruction.md").read_text(encoding="utf-8")
            task_file = workdir / ".ctx-task.md"
            task_file.write_text(task_text, encoding="utf-8")
            with (workdir / ".git" / "info" / "exclude").open("a", encoding="utf-8") as fh:
                fh.write(".ctx-task.md\nctx.toml\n")
            gold_all, gold_tests = _gold_files(task)
            rels = subprocess.run(["git", "ls-files"], cwd=workdir, capture_output=True,
                                  text=True).stdout.split()
            present = set(rels)
            # A file the fix CREATES cannot be surfaced by any ranker; recall
            # is over the gold files that exist at the base commit, and the
            # new ones are counted so the ceiling is visible.
            gold_src = [p for p in gold_all if p in present]
            gold_new = [p for p in gold_all if p not in present]
            rec = {"task_id": tid, "gold_src": gold_src, "gold_new": gold_new,
                   "gold_tests": gold_tests, "files": len(rels)}
            grep = _grep_count_rank(workdir, task_text, rels)
            rec["grep-count"] = {**_metrics(grep, gold_src, ns.top), "top": grep[:ns.top]}
            for label, hist in (("pack-nohist", False), ("pack", True)):
                ranked, wall, doc = _pack_rank(workdir, state, task_file, history=hist, top=ns.top)
                rec[label] = {**_metrics(ranked, gold_src, ns.top), "top": ranked, "wall_s": round(wall, 2),
                              "index_files": doc.get("corpus_files")}
            rows.append(rec)
            print(f"{tid:44s} gold={len(gold_src)} grep@{ns.top}={rec['grep-count'][f'recall@{ns.top}']}"
                  f" nohist@{ns.top}={rec['pack-nohist'][f'recall@{ns.top}']}"
                  f" pack@{ns.top}={rec['pack'][f'recall@{ns.top}']} first={rec['pack']['first_hit']}",
                  flush=True)
        except Exception as e:  # keep going; the receipt names the failure
            rows.append({"task_id": tid, "error": f"{type(e).__name__}: {e}"})
            print(f"{tid:44s} ERROR {type(e).__name__}: {e}", flush=True)
        finally:
            if not ns.keep:
                shutil.rmtree(workdir, ignore_errors=True)
                shutil.rmtree(state, ignore_errors=True)

    ok = [r for r in rows if "error" not in r]
    summary = {}
    for label in ("grep-count", "pack-nohist", "pack"):
        vals = [r[label] for r in ok]
        n = len(vals) or 1
        summary[label] = {
            "recall@5": round(sum(v["recall@5"] or 0 for v in vals) / n, 3),
            f"recall@{ns.top}": round(sum(v[f"recall@{ns.top}"] or 0 for v in vals) / n, 3),
            "mrr": round(sum(v["rr"] for v in vals) / n, 3),
            "first_hit_within_top": sum(1 for v in vals if v["first_hit"] and v["first_hit"] <= ns.top),
            "tasks": len(vals),
            "gold_existing": sum(len(r["gold_src"]) for r in ok),
            "gold_new": sum(len(r["gold_new"]) for r in ok),
        }
    out = {"schema": "agentbench.pack_recall/v1", "top": ns.top, "tasks": rows, "summary": summary,
           "corpus_commit": tasks[0]["corpus_commit"] if tasks else None}
    pathlib.Path(ns.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(ns.out).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
