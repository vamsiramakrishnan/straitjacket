#!/usr/bin/env python3
"""Live A/B bug bash: harnessed (`ctx wrap claude`) vs naive.

Both arms get evals/devex/TASK-BUGBASH.md verbatim and hunt defects in the
same repository at the same base commit. The only difference between arms is
whether `ctx wrap claude` has installed its harness into the arm's checkout.

A claimed bug counts ONLY if it reproduces. Each arm's `repro` command is
executed against a PRISTINE clone at the base commit, with only the arm's
`findings.json` and `bugbash/` copied across -- so a repro cannot pass by
having quietly fixed the tree, and cannot depend on unrelated edits. This is
the whole point: counting *claims* rewards hallucination, counting *verified
repros* does not.

Instrumentation is per-arm and total, taken from `--output-format
stream-json`: cost, turns, wall clock, token split, prompt-cache
creation/read, tool calls by name, and -- the mechanism under test -- the
total and peak bytes of tool RESULT content the model was made to read.

Usage:
    python3 evals/devex/bugbash.py --out /tmp/bugbash --arm both
    python3 evals/devex/bugbash.py --out /tmp/bugbash --grade-only
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
REPO = HERE.parent.parent
TASK = (HERE / "TASK-BUGBASH.md").read_text(encoding="utf-8")
TOOLS = "Bash Read Write Edit Grep Glob"

# Repros that cannot possibly be exercising a defect.
TRIVIAL = re.compile(
    r"^\s*(false|exit\s+[1-9]|/bin/false|test\s+1\s*(-eq|=)\s*2|\[\s*1\s*(-eq|=)\s*2\s*\])\s*;?\s*$"
)
REPRO_TIMEOUT = 120


def sh(argv, cwd=None, env=None, timeout=600, shell=False):
    return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, shell=shell,
                          capture_output=True, text=True)


# ----------------------------------------------------------------- arms
def clone_at(dest: pathlib.Path, base: str) -> pathlib.Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sh(["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(dest)])
    sh(["git", "checkout", "--quiet", base], cwd=dest)
    # The eval harness must not be visible to the arms it grades. Once this
    # directory is committed, an arm could read the verifier -- the trivial-
    # repro regex, the guards -- and tune its findings to them. Removing it
    # from every clone (both arms, and the pristine verification tree) keeps
    # the grader out of the graded tree without pinning base to a stale commit.
    shutil.rmtree(dest / "evals" / "devex", ignore_errors=True)
    return dest


def run_arm(arm: str, out: pathlib.Path, base: str, max_turns: int,
            model: str, timeout: int) -> dict:
    d = out / f"arm-{arm}"
    repo = clone_at(d / "repo", base)
    if arm == "harnessed":
        r = sh(["ctx", "wrap", "claude"], cwd=repo, timeout=180)
        (d / "wrap.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    cfg = d / "claude-cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
    argv = ["claude", "-p", TASK, "--max-turns", str(max_turns),
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", TOOLS,
            "--permission-mode", "acceptEdits", "--model", model]
    t0 = time.time()
    # Stream straight to disk. Buffering stdout and writing on exit loses the
    # entire wire record if the runner dies mid-arm -- which is exactly what
    # happened to the naive arm on the first run.
    with (d / "stream.jsonl").open("w", encoding="utf-8") as fout, \
            (d / "stderr.txt").open("w", encoding="utf-8") as ferr:
        proc = subprocess.Popen(argv, cwd=repo, env=env, stdout=fout,
                                stderr=ferr, text=True)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=60)
            rc = -1
    return {"_wall_s": round(time.time() - t0, 1), "_rc": rc}


# ------------------------------------------------------- instrumentation
def instrument(stream_path: pathlib.Path) -> dict:
    """Everything the wire will tell us, including the flood itself."""
    calls: dict[str, int] = {}
    result_bytes = 0
    peak = 0
    big = 0            # tool results over 10 KB -- flood events
    n_results = 0
    final: dict = {}
    for line in stream_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        if t == "assistant":
            for b in o.get("message", {}).get("content", []) or []:
                if b.get("type") == "tool_use":
                    calls[b.get("name", "?")] = calls.get(b.get("name", "?"), 0) + 1
        elif t == "user":
            for b in o.get("message", {}).get("content", []) or []:
                if b.get("type") != "tool_result":
                    continue
                c = b.get("content")
                if isinstance(c, list):
                    s = "".join(x.get("text", "") for x in c
                                if isinstance(x, dict))
                else:
                    s = str(c or "")
                n = len(s.encode("utf-8", "ignore"))
                n_results += 1
                result_bytes += n
                peak = max(peak, n)
                if n > 10_000:
                    big += 1
        elif t == "result":
            final = o
    u = final.get("usage") or {}
    cc = int(u.get("cache_creation_input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    return {
        "cost_usd": round(float(final.get("total_cost_usd") or 0.0), 4),
        "turns": final.get("num_turns"),
        "is_error": bool(final.get("is_error")),
        "stop_reason": final.get("stop_reason"),
        "permission_denials": len(final.get("permission_denials") or []),
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "cache_creation": cc,
        "cache_read": cr,
        "cache_hit_ratio": round(cr / (cr + cc), 3) if (cr + cc) else 0.0,
        "tool_calls_total": sum(calls.values()),
        "tool_calls_by_name": dict(sorted(calls.items())),
        "tool_results": n_results,
        "tool_result_bytes": result_bytes,
        "tool_result_peak_bytes": peak,
        "tool_results_over_10kb": big,
        "bytes_per_result": round(result_bytes / n_results) if n_results else 0,
    }


# -------------------------------------------------------------- verdicts
def verify(arm: str, out: pathlib.Path, base: str) -> dict:
    """Run each claimed repro on a PRISTINE tree. Only exit!=0 counts."""
    d = out / f"arm-{arm}"
    arm_repo = d / "repo"
    # findings.jsonl (append-only, one object per line) is preferred: an arm
    # cut off by the turn cap still leaves every finding it had already
    # proved. findings.json remains accepted for whole-file writers.
    claims: list = []
    jsonl = arm_repo / "findings.jsonl"
    fpath = arm_repo / "findings.json"
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                claims.append(json.loads(line))
            except Exception:
                continue  # a half-written last line costs one finding, not all
    elif fpath.exists():
        try:
            claims = json.loads(fpath.read_text(encoding="utf-8"))
            assert isinstance(claims, list)
        except Exception as e:
            return {"error": f"unparseable findings.json: {e}",
                    "claimed": 0, "confirmed": 0, "findings": []}
    if not claims:
        return {"error": "no findings", "claimed": 0, "confirmed": 0,
                "findings": []}

    clean = clone_at(d / "verify", base)
    src_bb = arm_repo / "bugbash"
    if src_bb.is_dir():
        shutil.copytree(src_bb, clean / "bugbash", dirs_exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(clean / "src")}

    rows = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        repro = str(c.get("repro") or "").strip()
        rec = {"id": c.get("id"), "file": c.get("file"),
               "severity": c.get("severity"), "claim": c.get("claim"),
               "repro": repro}
        if not repro:
            rec["verdict"] = "no-repro"
        elif TRIVIAL.match(repro):
            rec["verdict"] = "trivial-rejected"
        elif "ctx" not in repro and "bugbash" not in repro:
            rec["verdict"] = "unrelated-rejected"
        else:
            try:
                r = sh(repro, cwd=clean, env=env,
                       timeout=REPRO_TIMEOUT, shell=True)
                rec["exit"] = r.returncode
                rec["stderr_tail"] = (r.stderr or "")[-300:]
                rec["verdict"] = "CONFIRMED" if r.returncode != 0 else "did-not-reproduce"
            except subprocess.TimeoutExpired:
                rec["verdict"] = "timeout"
        rows.append(rec)

    conf = [r for r in rows if r["verdict"] == "CONFIRMED"]
    return {
        "claimed": len(rows),
        "confirmed": len(conf),
        "did_not_reproduce": sum(1 for r in rows if r["verdict"] == "did-not-reproduce"),
        "rejected": sum(1 for r in rows if "rejected" in str(r["verdict"])),
        "precision": round(len(conf) / len(rows), 3) if rows else 0.0,
        "confirmed_files": sorted({r.get("file") for r in conf if r.get("file")}),
        "findings": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--arm", default="both",
                    choices=["both", "harnessed", "naive"])
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=2700)
    ap.add_argument("--grade-only", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    base = sh(["git", "rev-parse", a.base], cwd=REPO).stdout.strip()

    # Harnessed first: the provider prefix cache is shared across arms, so the
    # second arm inherits a warm prefix. Running harnessed first makes that
    # bias run against the arm under test. See evals/devex/ab_devex.py.
    arms = ["harnessed", "naive"] if a.arm == "both" else [a.arm]
    report: dict = {"base": base, "model": a.model,
                    "max_turns": a.max_turns, "arms": {}}

    for arm in arms:
        print(f"\n=== arm: {arm} ===", flush=True)
        d = a.out / f"arm-{arm}"
        if not a.grade_only:
            w = run_arm(arm, a.out, base, a.max_turns, a.model, a.timeout)
        else:
            w = {"_wall_s": None}
        m = instrument(d / "stream.jsonl")
        m["wall_s"] = w["_wall_s"]
        v = verify(arm, a.out, base)
        print(f"  cost=${m['cost_usd']} turns={m['turns']} "
              f"tools={m['tool_calls_total']} "
              f"result_bytes={m['tool_result_bytes']:,} "
              f"floods={m['tool_results_over_10kb']}", flush=True)
        print(f"  claimed={v.get('claimed')} CONFIRMED={v.get('confirmed')} "
              f"precision={v.get('precision')}", flush=True)
        report["arms"][arm] = {"metrics": m, "verdicts": v}

    if len(report["arms"]) == 2:
        h = report["arms"]["harnessed"]
        n = report["arms"]["naive"]
        hf = {(f.get("file"), f.get("id")) for f in h["verdicts"]["findings"]
              if f["verdict"] == "CONFIRMED"}
        nf = {(f.get("file"), f.get("id")) for f in n["verdicts"]["findings"]
              if f["verdict"] == "CONFIRMED"}
        hfiles = set(h["verdicts"].get("confirmed_files") or [])
        nfiles = set(n["verdicts"].get("confirmed_files") or [])
        report["comparison"] = {
            "confirmed_harnessed": len(hf),
            "confirmed_naive": len(nf),
            "files_only_harnessed": sorted(hfiles - nfiles),
            "files_only_naive": sorted(nfiles - hfiles),
            "files_both": sorted(hfiles & nfiles),
            "cost_ratio_h_over_n": round(
                (h["metrics"]["cost_usd"] or 0) / n["metrics"]["cost_usd"], 3)
            if n["metrics"]["cost_usd"] else None,
            "result_bytes_ratio_h_over_n": round(
                (h["metrics"]["tool_result_bytes"] or 0)
                / n["metrics"]["tool_result_bytes"], 3)
            if n["metrics"]["tool_result_bytes"] else None,
        }

    (a.out / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("\n" + json.dumps(report.get("comparison", {}), indent=2))
    print(f"report: {a.out / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
