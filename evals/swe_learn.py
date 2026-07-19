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
DATASET = "SWE-bench/SWE-bench_Verified"


def fetch_instances(repo: str, limit: int) -> list[dict]:
    where = urllib.parse.quote(f'"repo"=\'{repo}\'')
    url = (
        f"{API}?dataset={urllib.parse.quote(DATASET)}&config=default&split=test"
        f"&where={where}&offset=0&length={limit}"
    )
    with urllib.request.urlopen(url, timeout=60) as r:
        rows = json.load(r)["rows"]
    return [row["row"] for row in rows]


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
    tests = json.loads(inst["FAIL_TO_PASS"]) if isinstance(
        inst["FAIL_TO_PASS"], str
    ) else inst["FAIL_TO_PASS"]
    r = subprocess.run(
        [str(vpy), "-m", "pytest", "-v", *tests],
        cwd=repo_dir, capture_output=True, text=True, timeout=600,
    )
    return r.stdout + r.stderr, r.returncode


def score_digest(digest: str, raw: str, gold: dict[str, list[tuple[int, int]]]) -> dict:
    """Gold-anchored evidence: for each gold file, is it named inline in
    the digest, present in raw (one ctx-get hop), or absent entirely?"""
    inline = hop = absent = 0
    detail = {}
    for f in gold:
        base = f.rsplit("/", 1)[-1]
        if f in digest or base in digest:
            inline += 1
            detail[f] = "inline"
        elif f in raw or base in raw:
            hop += 1
            detail[f] = "one-hop"
        else:
            absent += 1
            detail[f] = "absent"
    return {"inline": inline, "one_hop": hop, "absent": absent, "files": detail}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="pytest-dev/pytest")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--workdir", required=True)
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

    for inst in fetch_instances(args.repo, args.limit):
        print(f"== {inst['instance_id']} ==")
        gold = gold_regions(inst["patch"])
        print(f"  gold files: {list(gold)}")
        out = reproduce_failure(inst, work)
        if out is None:
            continue
        raw, code = out
        digest, _ = digest_output(store, ws, "bash", raw, is_error=code != 0)
        r_tok, d_tok = estimate_tokens(len(raw.encode())), estimate_tokens(
            len(digest.encode())
        )
        score = score_digest(digest, raw, gold)
        print(f"  failure output: {r_tok:,} tok -> digest {d_tok:,} tok (exit {code})")
        prof = digest.split("profile=", 1)[1].split("]", 1)[0] if "profile=" in digest else "?"
        print(f"  profile: {prof}")
        # The trichotomy is the lesson: inline = containment delivered the
        # fix location; one-hop = present in raw but dropped by the digest
        # (THE actionable defect class — the profile-improvement queue);
        # absent = not in the output at all, so no output-side channel could
        # deliver it — that task's evidence belongs to the search lane
        # (code verbs / repo map), not to digesting.
        print(
            f"  gold-anchored evidence: {score['inline']} digest-inline · "
            f"{score['one_hop']} digest-dropped (DEFECT) · "
            f"{score['absent']} not-in-output (search-lane)"
        )
        for f, verdict in score["files"].items():
            print(f"    {verdict:>7}: {f}")


if __name__ == "__main__":
    main()
