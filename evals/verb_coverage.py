"""Does `ctx map` advertise addresses that `ctx def` can actually resolve?

`ctx map` is the discovery surface. For every symbol it finds it prints the
exact address to use next::

    repo:api/client.go --symbol NewClientFromHTTP

That line is an affordance: it tells an agent what to type. This eval asks the
only question that matters about an affordance — **does it work?** It takes the
symbols the map advertises and asks `ctx def` to resolve each one, then reports
the resolve rate per language.

Why this is the right shape of measurement. `evals/contextbench.py` scores
retrieval end to end and, when it comes out low, cannot say whose fault that
is: the issue text, the probe extraction, the ranking, or a verb. This one has
no such ambiguity. The map produced the symbol and the address; if `ctx def`
refuses that address, the two halves of ctx disagree with each other and the
defect is ctx's by construction. No corpus, no gold labels, no model, no
network beyond fetching a repository to look at.

It is also cheap enough to be a gate rather than a study: one `ctx map` and N
`ctx def` calls per repository.

Usage
-----
    python evals/verb_coverage.py --repo /path/to/checkout [--repo ...]
    python evals/verb_coverage.py --from-contextbench --workdir /scratch/cb --limit 6

Environment matters and is reported, because two optional dependencies change
the answer completely: `universal-ctags` decides whether the map sees
non-Python files at all, and the `code` extra decides which engines `ctx def`
can reach. A run that does not say which of those were present is not a
measurement of ctx, it is a measurement of a laptop.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

#: Extension to language, for grouping. Only languages ContextBench covers.
LANG = {
    ".py": "python", ".pyi": "python",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
}

_MAP_SYMBOL = re.compile(r"repo:(\S+)\s+--symbol\s+(\S+)")
_MAP_HEADER = re.compile(r"\[ctx map ([\d,]+) files .*?engine (\S+)\]")
_DEF_OK = re.compile(r"definition:\s+repo:\S+\s+L(\d+):(\d+)")
_DEF_ENGINE = re.compile(r"engine (\w+)")

#: ctags emits import aliases and package names as tags. They are not
#: definitions and no definition verb should be expected to resolve them, so
#: they are excluded from the denominator rather than counted as failures.
NON_DEFINITION_HINTS = ("packageName", "package")


def _run(root: Path, *args: str, timeout: int = 180) -> str:
    try:
        r = subprocess.run(
            ["ctx", "--workspace", str(root), *args],
            capture_output=True, text=True, timeout=timeout, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        return ""
    return (r.stdout or "") + (r.stderr or "")


def _lang(path: str) -> str:
    return LANG.get(Path(path).suffix.lower(), "other")


def _ctags_kinds(root: Path, rel: str) -> dict[str, str]:
    """Symbol -> ctags kind, so import aliases can be excluded honestly."""
    if not shutil.which("ctags"):
        return {}
    try:
        r = subprocess.run(
            ["ctags", "--output-format=json", "--fields=+nek", "--sort=no", "-f", "-", rel],
            capture_output=True, text=True, timeout=60, cwd=str(root),
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    kinds: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("name"):
            kinds[str(d["name"])] = str(d.get("kind") or "")
    return kinds


def probe_repo(root: Path, *, budget: int, per_lang: int) -> dict:
    """Advertise-then-resolve, for one checkout."""
    mapped = _run(root, "map", "--budget", str(budget), timeout=300)
    header = _MAP_HEADER.search(mapped)
    advertised = [(f, s) for f, s in _MAP_SYMBOL.findall(mapped)]

    by_lang: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for rel, sym in advertised:
        by_lang[_lang(rel)].append((rel, sym))

    kinds_cache: dict[str, dict[str, str]] = {}
    results: dict[str, dict] = {}
    defects: list[dict] = []

    for lang, pairs in sorted(by_lang.items()):
        if lang == "other":
            continue
        checked = resolved = 0
        engines: Counter[str] = Counter()
        for rel, sym in pairs[:per_lang]:
            if rel not in kinds_cache:
                kinds_cache[rel] = _ctags_kinds(root, rel)
            if kinds_cache[rel].get(sym, "") in NON_DEFINITION_HINTS:
                continue  # an import alias is not a definition; not a failure
            checked += 1
            out = _run(root, "def", f"repo:{rel}:{sym}", timeout=90)
            eng = _DEF_ENGINE.search(out)
            engines[eng.group(1) if eng else "?"] += 1
            if _DEF_OK.search(out):
                resolved += 1
            else:
                reason = next(
                    (l for l in out.splitlines() if "ctx def:" in l), out.strip()[:120]
                )
                defects.append(
                    {"language": lang, "file": rel, "symbol": sym, "reason": reason[:160]}
                )
        if checked:
            results[lang] = {
                "advertised": len(pairs),
                "checked": checked,
                "resolved": resolved,
                "rate": round(resolved / checked, 4),
                "engines": dict(engines),
            }

    return {
        "root": str(root),
        "map_files": int((header.group(1) if header else "0").replace(",", "")),
        "map_engine": header.group(2) if header else "?",
        "advertised_symbols": len(advertised),
        "by_language": results,
        "defects": defects,
    }


def environment() -> dict:
    """The two optional dependencies that decide the answer."""
    def importable(name: str) -> bool:
        try:
            __import__(name)
            return True
        except Exception:
            return False

    return {
        "ctags": bool(shutil.which("ctags")),
        "jedi": importable("jedi"),
        "tree_sitter": importable("tree_sitter"),
        "tree_sitter_go": importable("tree_sitter_go"),
        "tree_sitter_typescript": importable("tree_sitter_typescript"),
        "ast_grep_py": importable("ast_grep_py"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", action="append", default=[], help="a checkout to probe")
    ap.add_argument("--from-contextbench", action="store_true",
                    help="materialize one repo per language from the corpus")
    ap.add_argument("--workdir", default="", help="corpus/repo cache for --from-contextbench")
    ap.add_argument("--limit", type=int, default=6, help="languages to sample")
    ap.add_argument("--budget", type=int, default=4000, help="ctx map token budget")
    ap.add_argument("--per-lang", type=int, default=20, help="symbols probed per language")
    ap.add_argument("--json", default="", help="write the full record here")
    args = ap.parse_args()

    roots = [Path(r).resolve() for r in args.repo]
    if args.from_contextbench:
        if not args.workdir:
            raise SystemExit("--from-contextbench needs --workdir")
        import contextbench as cb

        work = Path(args.workdir).expanduser().resolve()
        work.mkdir(parents=True, exist_ok=True)
        rows = cb.load_corpus(work, "verified")
        seen: dict[str, dict] = {}
        for r in sorted(rows, key=lambda r: r["instance_id"]):
            if r["language"] not in seen and len(seen) < args.limit:
                seen[r["language"]] = r
        for inst in seen.values():
            got = cb.materialize(inst, work / "repos")
            if got:
                roots.append(got)

    if not roots:
        raise SystemExit("no repositories to probe (pass --repo or --from-contextbench)")

    env = environment()
    print("environment: " + " · ".join(f"{k}={'yes' if v else 'NO'}" for k, v in env.items()))
    if not env["ctags"]:
        print("  warning: without universal-ctags the map cannot see non-Python files "
              "at all, and every non-Python row below will read as zero for the "
              "wrong reason.")
    print()

    records = [probe_repo(r, budget=args.budget, per_lang=args.per_lang) for r in roots]

    print(f"{'language':<12} {'advertised':>10} {'checked':>8} {'resolved':>9} {'rate':>6}  engines")
    print("-" * 72)
    totals: dict[str, dict] = defaultdict(lambda: {"advertised": 0, "checked": 0, "resolved": 0,
                                                   "engines": Counter()})
    for rec in records:
        for lang, row in rec["by_language"].items():
            t = totals[lang]
            t["advertised"] += row["advertised"]
            t["checked"] += row["checked"]
            t["resolved"] += row["resolved"]
            t["engines"].update(row["engines"])
    for lang in sorted(totals):
        t = totals[lang]
        rate = t["resolved"] / t["checked"] if t["checked"] else 0.0
        engines = ",".join(f"{k}:{v}" for k, v in sorted(t["engines"].items()))
        print(f"{lang:<12} {t['advertised']:>10} {t['checked']:>8} {t['resolved']:>9} "
              f"{rate:>6.0%}  {engines}")

    defects = [d for rec in records for d in rec["defects"]]
    print(f"\n== defect queue: {len(defects)} advertised addresses ctx def refused ==")
    for d in defects[:12]:
        print(f"  {d['language']:<11} repo:{d['file']}:{d['symbol']}")
        print(f"              {d['reason']}")
    if len(defects) > 12:
        print(f"  ... {len(defects) - 12} more (see --json)")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"environment": env, "repos": records,
                        "totals": {k: {**v, "engines": dict(v["engines"])}
                                   for k, v in totals.items()}}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
