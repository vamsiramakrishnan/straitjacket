#!/usr/bin/env python3
"""Live A/B: harnessed (`ctx wrap claude`) vs naive, on a real devex task.

The task (evals/devex/TASK.md) is frozen before any run and handed to both
arms verbatim. The ONLY difference between arms is whether `ctx wrap claude`
has installed its hooks into the arm's checkout — same model, same tool
allowlist, same turn cap, same task, same base commit. The delta therefore
isolates the harness, not the prompt.

Grading is mechanical: nine binary acceptance checks recomputed here against
each arm's resulting tree. Score is the fraction passed, the same shape as
the vibecode harness's "% of substeps".

Cost accounting reports gross and net-of-prefix. A cold run pays a fixed
system-prefix cache-creation tax (~35k tokens) that is identical for both
arms and swamps the mechanism delta; net-of-prefix is the honest comparison.

Usage:
    python3 evals/devex/ab_devex.py --out /tmp/devex --arm both
    python3 evals/devex/ab_devex.py --out /tmp/devex --grade-only
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
TASK = (HERE / "TASK.md").read_text(encoding="utf-8")
TOOLS = "Bash Read Write Edit Grep Glob"
ENGINES = ("ast-grep", "ctags", "fd", "semgrep", "scip")


def sh(argv, cwd=None, env=None, timeout=600):
    return subprocess.run(
        argv, cwd=cwd, env=env, timeout=timeout,
        capture_output=True, text=True,
    )


# ----------------------------------------------------------------- arms
def prepare(arm: str, out: pathlib.Path, base: str) -> pathlib.Path:
    """Fresh clone at the frozen base commit; wrap only the harnessed arm."""
    d = out / f"arm-{arm}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    repo = d / "repo"
    sh(["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(repo)])
    sh(["git", "checkout", "--quiet", base], cwd=repo)
    if arm == "harnessed":
        r = sh(["ctx", "wrap", "claude"], cwd=repo, timeout=180)
        (d / "wrap.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    return repo


def run_arm(arm: str, out: pathlib.Path, base: str, max_turns: int,
            model: str, timeout: int) -> dict:
    repo = prepare(arm, out, base)
    cfg = out / f"arm-{arm}" / "claude-cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
    argv = ["claude", "-p", TASK,
            "--max-turns", str(max_turns),
            "--output-format", "json",
            "--allowedTools", TOOLS,
            "--permission-mode", "acceptEdits",
            "--model", model]
    t0 = time.time()
    try:
        r = sh(argv, cwd=repo, env=env, timeout=timeout)
        raw, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        raw, err, rc = "", f"TIMEOUT after {timeout}s", -1
    wall = time.time() - t0
    (out / f"arm-{arm}" / "raw.json").write_text(raw or "", encoding="utf-8")
    if err:
        (out / f"arm-{arm}" / "stderr.txt").write_text(err, encoding="utf-8")
    try:
        res = json.loads(raw)
    except Exception:
        res = {"is_error": True, "parse_error": True, "stderr": err[:2000]}
    res["_wall_s"] = round(wall, 1)
    res["_rc"] = rc
    return res


# -------------------------------------------------------------- grading
def grade(repo: pathlib.Path) -> dict:
    """Nine binary acceptance checks against the arm's resulting tree."""
    c: dict[str, bool] = {}
    py = [sys.executable, "-m", "ctx"]
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}

    def ctx(args, tmo=120):
        try:
            return sh(py + args, cwd=repo, env=env, timeout=tmo)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([], -1, "", "timeout")

    # metadata version
    pv = ""
    for line in (repo / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            pv = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

    d = ctx(["doctor"])
    human = d.stdout + d.stderr
    c["1_version_truth"] = bool(pv) and pv in human.splitlines()[0] if human else False
    named = sum(1 for e in ENGINES if e in human)
    c["2_engine_coverage"] = named >= 4
    bad = [ln for ln in human.splitlines() if "✗" in ln]
    c["3_remediation"] = bool(bad) and any(
        k in human.lower() for k in ("run:", "install", "pip ", "brew ", "remedy")
    )

    j = ctx(["doctor", "--json"])
    try:
        obj = json.loads(j.stdout)
        c["4_json_valid"] = isinstance(obj, dict)
        rows = obj.get("rows") if isinstance(obj, dict) else None
        c["5_json_shape"] = (
            isinstance(rows, list) and len(rows) > 0
            and all(isinstance(r, dict) for r in rows)
            and all({"id", "ok"} <= set(r) for r in rows)
        )
    except Exception:
        c["4_json_valid"] = False
        c["5_json_shape"] = False
    c["6_exit_codes"] = d.returncode in (0, 1) and j.returncode in (0, 1)

    t = sh([sys.executable, "-m", "pytest", "-q", "--no-header", "-p",
            "no:cacheprovider"], cwd=repo, env=env, timeout=1800)
    c["7_tests_pass"] = t.returncode == 0
    (repo.parent / "pytest.txt").write_text(
        (t.stdout + t.stderr)[-6000:], encoding="utf-8")

    gates = []
    for script, extra in (("fix_docs_svgs.py", ["--check"]),
                          ("check_docs_links.py", []),
                          ("check_docs_facts.py", [])):
        p = repo / "scripts" / script
        if not p.exists():
            gates.append(False)
            continue
        g = sh([sys.executable, str(p), *extra], cwd=repo, timeout=600)
        gates.append(g.returncode == 0)
    c["8_docs_gates"] = all(gates)

    diff = sh(["git", "diff", "--name-only", "HEAD"], cwd=repo).stdout
    unt = sh(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo).stdout
    touched = diff + unt
    c["9_tests_added"] = any(
        "test" in ln for ln in touched.splitlines()
    ) and "CHANGELOG.md" in touched

    return {
        "checks": c,
        "passed": sum(1 for v in c.values() if v),
        "total": len(c),
        "score": round(sum(1 for v in c.values() if v) / len(c), 3),
        "files_touched": [ln for ln in touched.splitlines() if ln.strip()],
    }


def metrics(res: dict) -> dict:
    u = res.get("usage") or {}
    cc = int(u.get("cache_creation_input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    return {
        "cost_usd": round(float(res.get("total_cost_usd") or 0.0), 4),
        "turns": res.get("num_turns"),
        "wall_s": res.get("_wall_s"),
        "is_error": bool(res.get("is_error")),
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "cache_creation": cc,
        "cache_read": cr,
        "cache_hit_ratio": round(cr / (cr + cc), 3) if (cr + cc) else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--arm", default="both",
                    choices=["both", "harnessed", "naive"])
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=2400)
    ap.add_argument("--grade-only", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    base = sh(["git", "rev-parse", a.base], cwd=REPO).stdout.strip()
    # Harnessed runs FIRST on purpose. The provider prefix cache is shared
    # across arms, so whichever arm runs second inherits a warm prefix (a
    # measured ~5.5x cost advantage on an identical request). Ordering the
    # harnessed arm first makes that residual bias run *against* the arm
    # under test rather than for it. Cache-warmth was the dominant confound
    # in evals/ab-claude-code-2026-07-17.md; do not reorder without saying so.
    arms = ["harnessed", "naive"] if a.arm == "both" else [a.arm]
    report: dict = {"base": base, "model": a.model,
                    "max_turns": a.max_turns, "arms": {}}

    for arm in arms:
        print(f"\n=== arm: {arm} ===", flush=True)
        if a.grade_only:
            res = json.loads(
                (a.out / f"arm-{arm}" / "raw.json").read_text(encoding="utf-8"))
            res.setdefault("_wall_s", None)
        else:
            res = run_arm(arm, a.out, base, a.max_turns, a.model, a.timeout)
        m = metrics(res)
        print(f"  cost=${m['cost_usd']}  turns={m['turns']}  "
              f"wall={m['wall_s']}s  cache_hit={m['cache_hit_ratio']}", flush=True)
        g = grade(a.out / f"arm-{arm}" / "repo")
        print(f"  score={g['passed']}/{g['total']}", flush=True)
        report["arms"][arm] = {"metrics": m, "grade": g}

    (a.out / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("\n" + json.dumps(report["arms"], indent=2, sort_keys=True)[:4000])
    print(f"\nreport: {a.out / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
