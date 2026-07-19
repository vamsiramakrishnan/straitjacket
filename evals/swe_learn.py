"""SWE-bench as teacher, never referee (evals/BENCHMARK.md).

We do not run SWE-bench for resolve rates. We mine its tasks for the three
things they contain that money can't easily synthesize:

1. **Real hostile outputs** — each instance reproduces an actual failing
   test run in a real repository; the flood is genuine, not a fixture.
2. **Gold labels for evidence** — the gold patch names the files and line
   regions where the fix landed. A digest of the failing output either
   surfaces facts pointing into those regions, or it starved the agent.
3. **A stratification frame** — repo/language/build-family diversity for
   the pathology oracle, without inventing corpora.

This tool does the end-to-end extraction for one or more instances:

    python evals/swe_learn.py --repo pytest-dev/pytest --limit 1 \
        --workdir /path/to/scratch

Per instance: fetch metadata from the HF datasets-server API → shallow
clone at base_commit → editable install → apply test_patch → run the
FAIL_TO_PASS tests (they fail by construction pre-patch; that output is
the flood) → digest via the real pipeline → score **gold-anchored
evidence density**: the fraction of gold-patch files (and hunk line
regions) that the digest names inline, vs one `ctx get` hop away, vs
absent. No agent, no model, no leaderboard — a conformance measurement of
the evidence channel against ground truth.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

API = "https://datasets-server.huggingface.co/filter"
DATASETS = {
    "verified": "SWE-bench/SWE-bench_Verified",
    "multilingual": "SWE-bench/SWE-bench_Multilingual",
}
DATASET = DATASETS["verified"]

# Language families for the multilingual mine, keyed by repo. Breadth is
# data: a new repo is a table row naming its family. Families runnable in
# this container: cargo (rust), gotest (go), maven (jvm). JS (npm-family
# runners), ruby (rspec/minitest), and C/C++ (cmake/gtest, make) are the
# next rows — each needs its runner bootstrapped, none needs new scoring.
LANG_FAMILY = {
    "burntsushi/ripgrep": "cargo",
    "sharkdp/bat": "cargo",
    "tokio-rs/axum": "cargo",
    "tokio-rs/tokio": "cargo",
    "uutils/coreutils": "cargo",
    "nushell/nushell": "cargo",
    "astral-sh/ruff": "cargo",
    "gin-gonic/gin": "gotest",
    "caddyserver/caddy": "gotest",
    "gohugoio/hugo": "gotest",
    "prometheus/prometheus": "gotest",
    "hashicorp/terraform": "gotest",
    "google/gson": "maven",
    "javaparser/javaparser": "maven",
    "apache/druid": "maven",
}


def _family_argv(family: str, tests: list[str]) -> list[str]:
    if family == "cargo":
        # Rust test ids are full paths (mod::test); libtest takes multiple
        # --exact filters after `--`.
        return ["cargo", "test", "--no-fail-fast", "--", "--exact", *tests]
    if family == "gotest":
        # Subtests ('TestX/case') run via their root test name.
        roots = sorted({t.split("/")[0] for t in tests})
        return ["go", "test", "./...", "-run", "^(" + "|".join(roots) + ")$"]
    if family == "maven":
        return [
            "mvn", "-B", "-q", "test",
            "-Dtest=" + ",".join(tests),
            "-DfailIfNoTests=false",
            "-Dsurefire.failIfNoSpecifiedTests=false",
        ]
    raise ValueError(family)


def _django_spec(test: str) -> str:
    """'test_x (migrations.test_ops.OpTests)' -> 'migrations.test_ops.OpTests.test_x'."""
    m = re.match(r"^(\S+) \(([\w.]+)\)$", test.strip())
    return f"{m.group(2)}.{m.group(1)}" if m else test


# Run families: how each repo executes its FAIL_TO_PASS tests. The default
# family is pytest; breadth is data — adding a repo is a table row.
RUN_FAMILIES = {
    "django/django": lambda vpy, tests: [
        str(vpy), "tests/runtests.py", "--verbosity", "1",
        *(_django_spec(t) for t in tests),
    ],
    # sympy names bare test functions run through its own harness.
    "sympy/sympy": lambda vpy, tests: [str(vpy), "bin/test", *tests],
}


def _run_argv(repo: str, vpy: Path, tests: list[str]) -> list[str]:
    fam = RUN_FAMILIES.get(repo)
    if fam:
        return fam(vpy, tests)
    return [str(vpy), "-m", "pytest", "-v", *tests]


def fetch_instances(repo: str, limit: int, dataset: str = DATASET) -> list[dict]:
    """Newest-first: this container has one interpreter (3.11), and only
    the recent era of each repo imports on it. Pre-2022 instances need the
    official per-instance Docker images — deferred to a docker-capable
    environment, recorded honestly rather than scored as noise."""
    where = urllib.parse.quote(f'"repo"=\'{repo}\'')
    url = (
        f"{API}?dataset={urllib.parse.quote(dataset)}&config=default&split=test"
        f"&where={where}&offset=0&length=100"
    )
    with urllib.request.urlopen(url, timeout=60) as r:
        rows = [row["row"] for row in json.load(r)["rows"]]
    rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return rows[:limit]


def gold_regions(patch: str) -> dict[str, list[tuple[int, int]]]:
    """Files and post-image line regions touched by the gold patch."""
    regions: dict[str, list[tuple[int, int]]] = {}
    current = None
    for line in patch.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            current = m.group(1)
            regions.setdefault(current, [])
            continue
        h = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if h and current:
            start = int(h.group(1))
            n = int(h.group(2) or 1)
            regions[current].append((start, start + max(n - 1, 0)))
    return regions


def reproduce_failure(inst: dict, work: Path) -> tuple[str, int] | None:
    """Clone at base_commit, apply test_patch, run FAIL_TO_PASS tests
    inside a PER-INSTANCE venv — never the invoking interpreter (first run
    of this tool clobbered the host's pytest; isolation is not optional).
    Returns (combined output, exit code) or None if setup failed."""
    repo_dir = work / inst["instance_id"]
    if not repo_dir.exists():
        url = f"https://github.com/{inst['repo']}.git"
        subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
        subprocess.run(
            ["git", "fetch", "-q", "--depth", "1", url, inst["base_commit"]],
            cwd=repo_dir, check=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", inst["base_commit"]], cwd=repo_dir, check=True
        )
    p = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=repo_dir, input=inst["test_patch"], text=True, capture_output=True,
    )
    if p.returncode != 0 and "already exists" not in p.stderr:
        print(f"  test_patch failed to apply: {p.stderr[:200]}", file=sys.stderr)
        return None
    tests_raw = inst["FAIL_TO_PASS"]
    tests = json.loads(tests_raw) if isinstance(tests_raw, str) else tests_raw

    family = LANG_FAMILY.get(inst["repo"])
    if family:  # non-Python: toolchain runs in place, no venv
        r = subprocess.run(
            _family_argv(family, tests),
            cwd=repo_dir, capture_output=True, text=True, timeout=900,
        )
        return r.stdout + r.stderr, r.returncode

    venv = repo_dir / ".sj-venv"
    vpy = venv / "bin" / "python"
    if not vpy.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        # Shallow clones carry no tags; setuptools-scm would mint 0.1.dev1
        # and self-versioned projects (pytest's own minversion gate) refuse
        # to run. The dataset row names the real version — pretend it.
        env = dict(os.environ)
        env["SETUPTOOLS_SCM_PRETEND_VERSION"] = f"{inst.get('version', '1.0')}.0"
        subprocess.run(
            [str(vpy), "-m", "pip", "install", "-q", "-e", "."],
            cwd=repo_dir, capture_output=True, timeout=600, env=env,
        )
        if inst["repo"] not in RUN_FAMILIES:  # pytest-family repos need the runner
            subprocess.run(
                [str(vpy), "-m", "pip", "install", "-q", "pytest"],
                capture_output=True, timeout=300,
            )
    r = subprocess.run(
        _run_argv(inst["repo"], vpy, tests),
        cwd=repo_dir, capture_output=True, text=True, timeout=600,
    )
    combined = r.stdout + r.stderr
    # A run that never reached the tests is not a reproduction — scoring it
    # would count tooling noise as evidence. pytest reserves exit 1 for
    # real test failures; 2-5 are interruption/internal/usage/collection.
    not_repro = (
        "No module named" in combined[:400]
        or (inst["repo"] not in RUN_FAMILIES and r.returncode not in (0, 1))
    )
    if not_repro:
        tail = combined.strip().splitlines()[-1][:100] if combined.strip() else "?"
        print(f"  not a reproduction (exit {r.returncode}): {tail}")
        return None
    return combined, r.returncode


def score_digest(digest: str, raw: str, gold: dict[str, list[tuple[int, int]]]) -> dict:
    """Gold-anchored evidence per gold file. Inline requires real evidence:
    the full repo-relative path, or a basename:LINE / `File "...", line N`
    coordinate — a bare basename anywhere in prose is not credit. When a
    coordinate lands within ±20 lines of a gold hunk, that's a region hit
    (the digest didn't just name the file, it pointed at the fix site).
    One-hop keeps the loose containment test: any mention in raw means one
    ``ctx get`` recovers it."""
    _REGION_SLOP = 20
    inline = hop = absent = region_hits = 0
    detail = {}
    for f, regions in gold.items():
        base = re.escape(f.rsplit("/", 1)[-1])
        coords = [
            int(n)
            for n in re.findall(rf"{base}:(\d+)", digest)
            + re.findall(rf'{base}", line (\d+)', digest)
        ]
        if f in digest or coords:
            inline += 1
            verdict = "inline"
            if any(
                a - _REGION_SLOP <= n <= b + _REGION_SLOP
                for n in coords
                for a, b in regions
            ):
                region_hits += 1
                verdict = "inline+region"
            detail[f] = verdict
        elif f in raw or f.rsplit("/", 1)[-1] in raw:
            hop += 1
            detail[f] = "one-hop"
        else:
            absent += 1
            detail[f] = "absent"
    return {
        "inline": inline,
        "one_hop": hop,
        "absent": absent,
        "region_hits": region_hits,
        "files": detail,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="pytest-dev/pytest", help="comma-separated repo list")
    ap.add_argument("--limit", type=int, default=1, help="instances per repo")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--dataset", choices=sorted(DATASETS), default="verified")
    ap.add_argument(
        "--keep", action="store_true",
        help="keep clones+venvs (default: delete after scoring — disk hygiene)",
    )
    args = ap.parse_args()
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)

    from ctx.digest import digest_output
    from ctx.store import Store
    from ctx.textutil import estimate_tokens
    from ctx.workspace import resolve_workspace

    ws_dir = work / "_ws"
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(ws_dir))
    store = Store(ws_dir / "store")

    import shutil

    totals = {"inline": 0, "one_hop": 0, "absent": 0, "instances": 0, "skipped": 0}
    defects: list[tuple[str, str]] = []
    for repo in [r.strip() for r in args.repo.split(",") if r.strip()]:
        for inst in fetch_instances(repo, args.limit, DATASETS[args.dataset]):
            print(f"== {inst['instance_id']} ==", flush=True)
            gold = gold_regions(inst["patch"])
            try:
                out = reproduce_failure(inst, work)
            except Exception as e:  # noqa: BLE001 — a family that won't build is data
                print(f"  skipped: {type(e).__name__}: {str(e)[:120]}")
                totals["skipped"] += 1
                continue
            finally:
                if not args.keep:
                    shutil.rmtree(work / inst["instance_id"], ignore_errors=True)
            if out is None:
                totals["skipped"] += 1
                continue
            raw, code = out
            digest, _ = digest_output(store, ws, "bash", raw, is_error=code != 0)
            r_tok, d_tok = estimate_tokens(len(raw.encode())), estimate_tokens(
                len(digest.encode())
            )
            score = score_digest(digest, raw, gold)
            prof = digest.split("profile=", 1)[1].split("]", 1)[0] if "profile=" in digest else "?"
            print(f"  {r_tok:,} tok -> {d_tok:,} tok · exit {code} · {prof}")
            # The trichotomy is the lesson: inline = containment delivered
            # the fix location; one-hop = present in raw but dropped by the
            # digest (THE actionable defect class — the profile-improvement
            # queue); absent = not in the output at all — that evidence
            # belongs to the search lane (code verbs / repo map).
            region = f" ({score['region_hits']} at fix region)" if score["region_hits"] else ""
            print(
                f"  gold: {score['inline']} digest-inline{region} · "
                f"{score['one_hop']} digest-dropped (DEFECT) · "
                f"{score['absent']} not-in-output (search-lane)"
            )
            for f, verdict in score["files"].items():
                if verdict != "inline":
                    print(f"    {verdict:>7}: {f}")
                if verdict == "one-hop":
                    defects.append((inst["instance_id"], f))
            totals["inline"] += score["inline"]
            totals["one_hop"] += score["one_hop"]
            totals["absent"] += score["absent"]
            totals["instances"] += 1

    print("\n== aggregate ==")
    n = totals["inline"] + totals["one_hop"] + totals["absent"]
    print(
        f"  {totals['instances']} instances scored ({totals['skipped']} skipped) · "
        f"{n} gold files: {totals['inline']} inline · "
        f"{totals['one_hop']} digest-dropped · {totals['absent']} search-lane"
    )
    if defects:
        print("  DEFECT QUEUE (raw held it, digest dropped it):")
        for iid, f in defects:
            print(f"    {iid}: {f}")


if __name__ == "__main__":
    main()
