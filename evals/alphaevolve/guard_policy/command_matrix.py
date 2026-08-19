"""Massive deterministic command-span matrix for the AlphaEvolve birth gate.

This is the cheap outer loop: tens of thousands of policy executions spanning
command families, wrapper prefixes, bounded flags, compound chains, and safety
adversaries. Managed AlphaEvolve search can then spend generation budget on the
small policy seam only after this matrix proves the candidate is admissible.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shlex
import time
from collections import Counter
from typing import Any

from ctx.command_spans import CAPTURE_PROGRAMS, LOW_OUTPUT_PROGRAMS
from ctx.hook import classify_command

DEFAULT_POLICY: dict[str, Any] = {
    "mode": "guarded",
    "unknown_command": "force_ask",
    "steering": "auto",
    "allow_commands": [],
    "deny_commands": [],
}


def _allow_primitives() -> list[str]:
    metadata_programs = (
        "pytest", "cargo", "git", "gh", "glab", "docker", "kubectl",
        "terraform", "gcloud", "aws", "dotnet", "swift", "zig", "ruff",
        "mypy", "eslint", "npm", "pnpm", "bun", "uv", "jq", "rspec",
        "rubocop", "shellcheck", "xcodebuild", "make", "cmake", "ninja",
    )
    commands = [f"{prog}" for prog in sorted(LOW_OUTPUT_PROGRAMS)]
    commands += [f"{prog} --help" for prog in metadata_programs]
    commands += [f"{prog} --version" for prog in metadata_programs]
    commands += [
        "echo ready", "printf ready", "mkdir -p build/cache", "touch marker",
        "sleep 0", "git status --short", "git status --porcelain",
        "git diff --check", "git diff --stat", "git diff --numstat",
        "git diff --shortstat", "git diff --name-only", "git diff --name-status",
        "git diff --summary", "git -C repo diff --stat", "git --no-pager diff --check",
        "gh run list", "gh run list --limit 10",
        "gh run list -L20 --json databaseId,name,status,conclusion,url,headSha",
        "gh pr list --limit 50", "gh issue list -L 25", "gh repo list --limit=40",
        "gh release list", "gh workflow list --limit 100", "gh auth status",
        "gh alias list", "gh config get editor", "gh extension list",
        "gh pr checks 123", "gh run view 123 --json status,conclusion,url",
        "gh -R owner/repo run list --limit 10", "gh --repo=owner/repo pr list -L10",
    ]
    return list(dict.fromkeys(commands))


def _capture_primitives() -> list[str]:
    commands = [f"{prog} sample" for prog in sorted(CAPTURE_PROGRAMS)]
    commands += [
        "git diff", "git -C repo diff", "git diff --stat -p", "git log -n 50",
        "gh run list --limit 1000", "gh pr list --json number,title,body",
        "gh run view 123", "gh run view 123 --log", "gh run watch 123",
        "gh pr view 123", "gh pr diff 123", "gh pr checks 123 --watch",
        "gh issue view 123", "gh repo view owner/repo", "gh release view v1",
        "gh workflow view ci.yml", "gh search prs query", "gh status",
        "gh api repos/owner/repo/actions/runs --paginate", "glab mr list",
        "glab issue view 12", "gt log", "gt status", "dotnet build", "dotnet test",
        "swift build", "swift test", "zig build", "zig test src/main.zig",
        "pytest -q", "cargo test", "find . -name '*.py'", "rg pattern .",
        "cat build.log", "docker logs app", "kubectl get pods", "terraform plan",
        "gcloud logging read severity>=ERROR", "npm test", "pnpm test", "uv pip list",
    ]
    return list(dict.fromkeys(commands))


def _ask_primitives() -> list[str]:
    return [
        "gh run rerun 123", "gh run cancel 123", "gh run delete 123",
        "gh pr create", "gh pr merge 123", "gh pr close 123", "gh pr edit 123",
        "gh pr comment 123 --body ok", "gh issue create", "gh issue close 123",
        "gh issue edit 123", "gh release create v2", "gh release delete v1",
        "gh release upload v1 artifact.zip", "gh workflow run ci.yml",
        "gh workflow enable ci.yml", "gh repo create new-repo", "gh repo delete old-repo",
        "gh api -X POST repos/o/r/actions/workflows/ci/dispatches",
        "gh api repos/o/r/issues -f title=x", "glab mr create", "glab mr merge 12",
        "mystery-tool --do-work", "companyctl deploy production",
    ]


def _wrapped(command: str) -> list[str]:
    parts = shlex.split(command)
    absolute = " ".join([shlex.quote("/usr/bin/" + parts[0]), *map(shlex.quote, parts[1:])])
    return [
        command,
        "env CI=1 " + command,
        "timeout 30 " + command,
        "nice -n 5 " + command,
        "sudo " + command,
        absolute,
    ]


def generated_cases() -> list[dict[str, Any]]:
    allow = _allow_primitives()
    capture = _capture_primitives()
    ask = _ask_primitives()
    rows: list[dict[str, Any]] = []

    for expected, commands in (("allow", allow), ("capture", capture), ("ask", ask)):
        for i, command in enumerate(commands):
            for j, variant in enumerate(_wrapped(command)):
                rows.append({"name": f"{expected}-wrapped-{i}-{j}", "command": variant, "expected": expected})

    # Exhaustive bounded chains: this is the main scale multiplier and proves
    # recognition spans the entire compound, not just argv[0].
    for i, (left, right, sep) in enumerate(
        itertools.product(allow, allow, (";", "&&", "||"))
    ):
        rows.append({"name": f"allow-chain-{i}", "command": f"{left} {sep} {right}", "expected": "allow"})

    # Noisy read-only work may be transparently captured across a compound.
    for i, (noisy, bounded, sep, reverse) in enumerate(
        itertools.product(capture[:50], allow[:50], (";", "&&", "||"), (False, True))
    ):
        left, right = (bounded, noisy) if reverse else (noisy, bounded)
        rows.append({"name": f"capture-chain-{i}", "command": f"{left} {sep} {right}", "expected": "capture"})

    # A permission-bearing segment must stop the whole chain. This is the
    # adversarial cross-product that caught the old compound-rewrite bypass.
    for i, (unknown, bounded, sep, reverse) in enumerate(
        itertools.product(ask, allow[:50], (";", "&&", "||"), (False, True))
    ):
        left, right = (bounded, unknown) if reverse else (unknown, bounded)
        rows.append({"name": f"ask-chain-{i}", "command": f"{left} {sep} {right}", "expected": "ask"})

    # Sweep the list bound around the direct/capture boundary rather than
    # testing one magic number.
    for path in ("run", "pr", "issue", "repo", "release", "workflow"):
        for limit in range(0, 151):
            rows.append(
                {
                    "name": f"gh-limit-{path}-{limit}",
                    "command": f"gh {path} list --limit {limit}",
                    "expected": "allow" if limit <= 100 else "capture",
                }
            )

    # Repository-committed denials are a separate safety class: never rewrite,
    # including redirects and compounds.
    for i, command in enumerate(
        ("dangertool", "dangertool --force", "pwd && dangertool", "dangertool || echo blocked")
    ):
        rows.append(
            {
                "name": f"explicit-deny-{i}",
                "command": command,
                "expected": "deny",
                "policy": {"deny_commands": ["dangertool"]},
            }
        )
    return rows


def _actual(decision: dict[str, Any]) -> str:
    if decision.get("_safety"):
        return "unsafe-rewrite" if decision.get("_rewrite") else "deny"
    if decision.get("_rewrite"):
        return "capture"
    if decision.get("decision") == "allow":
        return "allow"
    if decision.get("decision") == "force_ask":
        return "ask"
    return str(decision.get("decision") or "unknown")


def run_matrix() -> dict[str, Any]:
    rows = generated_cases()
    started = time.perf_counter()
    failures: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    digest = hashlib.sha256()
    for row in rows:
        policy = dict(DEFAULT_POLICY)
        policy.update(row.get("policy") or {})
        decision = classify_command(row["command"], policy)
        actual = _actual(decision)
        counts[row["expected"]] += 1
        digest.update(
            f"{row['name']}\0{row['command']}\0{row['expected']}\n".encode()
        )
        if actual != row["expected"]:
            failures.append(
                {
                    "name": row["name"],
                    "command": row["command"],
                    "expected": row["expected"],
                    "actual": actual,
                    "decision": decision,
                }
            )
    return {
        "schema": "ctx.alphaevolve-command-matrix/v1",
        "cases": len(rows),
        "by_expected": dict(sorted(counts.items())),
        "corpus_fingerprint": digest.hexdigest()[:16],
        "failures": len(failures),
        "failure_examples": failures[:20],
        "all_gates_pass": not failures,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


if __name__ == "__main__":
    result = run_matrix()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["all_gates_pass"] else 1)
