#!/usr/bin/env python3
"""Fail CI when documentation facts drift from the source of truth.

The docs repeatedly stated a product version, a `ctx ask` intent count, and a
test total by hand, and all three drifted out of sync with the code (a v0.25
README against a v0.30 package, "three intents" in one guide and "seven" in
another). This guard derives each fact from its authoritative source and
asserts the prose agrees — so a number can only be wrong if the code is too.

Authoritative sources:
  - product version   -> pyproject.toml  [project].version
  - ctx ask intents   -> src/ctx/ask.py  INTENTS dict (counted via AST)

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
    return data["project"]["version"]


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


def main() -> int:
    root = Path.cwd().resolve()
    failures: list[str] = []

    # 1. The top-level README status line must name the current version.
    version = product_version(root)
    readme = _read(root, "README.md")
    if f"v{version}" not in readme:
        failures.append(
            f"README.md does not mention the current version v{version} "
            f"(pyproject.toml says {version}); update the Status line."
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

    if failures:
        print("Documentation fact-check failures:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"Documentation facts: PASS (version v{version}, {count} ctx ask intents)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
