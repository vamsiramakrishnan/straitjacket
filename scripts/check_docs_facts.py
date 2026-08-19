#!/usr/bin/env python3
"""Fail CI when documentation facts drift from the source of truth.

The docs repeatedly stated a product version, a `ctx ask` intent count, and a
test total by hand, and all three drifted out of sync with the code (a v0.25
README against a v0.30 package, "three intents" in one guide and "seven" in
another). This guard derives each fact from its authoritative source and
asserts the prose agrees — so a number can only be wrong if the code is too.

Authoritative sources:
  - product version   -> Hatch's configured version source
  - ctx ask intents   -> src/ctx/ask.py  INTENTS dict (counted via AST)
  - test total        -> tests/**/*.py   test functions (counted via AST)

The test total was named above as one of the three drifting facts but was
never actually asserted, so it drifted furthest: the README said 1,159 while
the tree held 1,573. Counting is by AST rather than by running pytest, so the
check stays pure-stdlib and costs milliseconds.

Pure stdlib, no third-party imports, so it runs anywhere the hook does.
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def product_version(root: Path) -> str:
    data = tomllib.loads(_read(root, "pyproject.toml"))
    static = data["project"].get("version")
    if static:
        return str(static)

    version_path = data.get("tool", {}).get("hatch", {}).get("version", {}).get("path")
    if not version_path:
        raise RuntimeError("pyproject.toml declares no static or Hatch version source")
    tree = ast.parse(_read(root, str(version_path)))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    raise RuntimeError(f"could not read __version__ from {version_path}")


def intent_count(root: Path) -> int:
    """Count keys in the INTENTS dict in src/ctx/ask.py, via AST (not regex)."""
    tree = ast.parse(_read(root, "src/ctx/ask.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "INTENTS" and isinstance(node.value, ast.Dict):
                return len(node.value.keys)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            if any(isinstance(t, ast.Name) and t.id == "INTENTS" for t in node.targets):
                return len(node.value.keys)
    raise RuntimeError("could not find the INTENTS dict in src/ctx/ask.py")


def test_count(root: Path) -> int:
    """Count test functions across tests/ via AST.

    Counts module-level `def test_*` plus methods named `test_*` inside
    `class Test*`, which is what pytest collects by default. Parametrised
    cases expand at run time and are deliberately NOT multiplied out — the
    figure the README states is "tests written", and a stable definition
    matters more than matching a pytest tally that moves with every id.
    """
    total = 0
    for path in sorted((root / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    total += 1
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if sub.name.startswith("test_"):
                            total += 1
    return total


_TEST_CLAIM = re.compile(r"(\d[\d,]*)\s+(test functions|tests)\b", re.IGNORECASE)


def _stated_test_counts(text: str) -> list[tuple[str, str]]:
    """Every '<number> test functions' / '<number> tests' claim, as (n, noun).

    Anchored on a leading digit: a bare `[\\d,]*` also matches a lone comma
    (as in "…, tests…"), which reduces to an empty string once separators are
    stripped.

    Both nouns are matched, but only "test functions" is a claim this checker
    can honour — see the note in main() on why the bare noun is rejected.
    """
    return _TEST_CLAIM.findall(text)


def main() -> int:
    root = Path.cwd().resolve()
    failures: list[str] = []

    # 1. The top-level README status line must name the current version.
    version = product_version(root)
    readme = _read(root, "README.md")
    if f"v{version}" not in readme:
        failures.append(
            f"README.md does not mention the current version v{version} "
            f"(the package version is {version}); update the Status line."
        )

    # 2. Any doc that states an intent count must state the right one.
    count = intent_count(root)
    word = _NUMBER_WORDS.get(count, str(count))
    # Match "<word> intents" or "<digit> intents", case-insensitive.
    claim = re.compile(r"\b(\w+)\s+intents\b", re.IGNORECASE)
    for rel in ("docs/CLI.md", "docs/ASK.md"):
        text = _read(root, rel)
        for stated in claim.findall(text):
            low = stated.lower()
            if low in _NUMBER_WORDS.values() or low.isdigit():
                ok = low == word or low == str(count)
                if not ok:
                    failures.append(
                        f"{rel}: claims '{stated} intents' but ask.py ships "
                        f"{count} ({word}). Update the count."
                    )

    # 3. Any stated test total must match what the tree actually holds.
    #
    # The docs must say "N test functions", not "N tests". Those are different
    # numbers — parametrised cases expand at collection, so pytest reports
    # more than the tree defines — and the bare noun invites whichever figure
    # was on screen that day. This is exactly how the README ended up carrying
    # two different stale counts (1,159 and 1,074) in three places. Only the
    # AST-countable claim is checkable without running pytest, so only the
    # AST-countable claim is allowed.
    tests = test_count(root)
    for stated, noun in _stated_test_counts(readme):
        if noun.lower() != "test functions":
            failures.append(
                f"README.md: says '{stated} {noun}', which nothing can verify "
                f"cheaply (pytest expands parametrised cases). Write "
                f"'{tests:,} test functions' — that figure is checked."
            )
        elif int(stated.replace(",", "")) != tests:
            failures.append(
                f"README.md: claims '{stated} test functions' but tests/ "
                f"defines {tests:,}. Update it."
            )

    if failures:
        print("Documentation fact-check failures:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"Documentation facts: PASS (version v{version}, "
        f"{count} ctx ask intents, {tests:,} test functions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
