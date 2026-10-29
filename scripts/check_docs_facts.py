#!/usr/bin/env python3
"""Fail CI when documentation facts drift from the source of truth.

The docs repeatedly stated product and evaluation numbers by hand. Several
drifted out of sync with the code or their committed machine records. This
guard derives each checked fact from its authoritative source and asserts the
front-door prose agrees.

Authoritative sources:
  - product version   -> Hatch's configured version source
  - ctx ask intents   -> src/ctx/ask.py  INTENTS dict (counted via AST)
  - test total        -> tests/**/*.py   test functions (counted via AST)
  - evaluation claims -> committed JSON records plus deterministic replays

The test total was named above as one of the three drifting facts but was
never actually asserted, so it drifted furthest: the README said 1,159 while
the tree held 1,573. Counting is by AST rather than by running pytest, so the
check stays pure-stdlib and costs milliseconds.

The core check is stdlib-only. Exact field-receipt token replay additionally
uses pinned ``tiktoken`` in CI; without it, the corpus and live straitjacket arm
are still replayed and a local notice names the skipped token assertion.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _json(root: Path, rel: str) -> dict:
    return json.loads(_read(root, rel))


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
    intent_total = intent_count(root)
    word = _NUMBER_WORDS.get(intent_total, str(intent_total))
    # Match "<word> intents" or "<digit> intents", case-insensitive.
    claim = re.compile(r"\b(\w+)\s+intents\b", re.IGNORECASE)
    for rel in ("docs/CLI.md", "docs/ASK.md"):
        text = _read(root, rel)
        for stated in claim.findall(text):
            low = stated.lower()
            if low in _NUMBER_WORDS.values() or low.isdigit():
                ok = low == word or low == str(intent_total)
                if not ok:
                    failures.append(
                        f"{rel}: claims '{stated} intents' but ask.py ships "
                        f"{intent_total} ({word}). Update the count."
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

    # 4. Front-door evidence claims must match their committed machine records,
    # and the field headline must still be reproducible with the current code.
    # These numbers previously diverged between the README, site, prose receipt,
    # and JSON record. The record is reviewable history; replay keeps it from
    # becoming a self-asserting source of truth.
    field = _json(root, "evals/field-needle-record.json")
    corpus = field["corpus"]
    sj = next(arm for arm in field["arms"] if arm["tool"].startswith("sj "))
    raw_tokens = int(corpus["raw_tokens_o200k"])
    visible_tokens = int(sj["out_tokens"])
    needle_line = int(corpus["quiet_needle_line"])
    quiet_needle_survived = bool(sj["quiet_needle_survived"])
    retrieval_address = bool(sj["retrieval_address"])
    ratio = round(raw_tokens / visible_tokens)
    field_token_replayed = False

    sys.path.insert(0, str(root))
    from evals.field_needle import (
        QUIET_NEEDLE_LINE,
        QUIET_NEEDLE_MARK,
        build_corpus,
        run_sj,
        token_counter,
    )

    raw = build_corpus()
    actual_corpus = {
        "lines": raw.count("\n"),
        "bytes": len(raw.encode("utf-8")),
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "quiet_needle_line": QUIET_NEEDLE_LINE,
    }
    for key, actual in actual_corpus.items():
        if corpus.get(key) != actual:
            failures.append(
                f"evals/field-needle-record.json: corpus {key} is "
                f"{corpus.get(key)!r}, replay produced {actual!r}."
            )
    raw_lines = raw.splitlines()
    if (
        QUIET_NEEDLE_LINE > len(raw_lines)
        or QUIET_NEEDLE_MARK not in raw_lines[QUIET_NEEDLE_LINE - 1]
    ):
        failures.append(
            "evals/field_needle.py: the quiet needle is no longer present at "
            f"the declared line {QUIET_NEEDLE_LINE}."
        )

    try:
        token_count = token_counter()
    except (ImportError, ModuleNotFoundError) as exc:
        token_count = lambda _text: 0
        if os.environ.get("CTX_DOCS_REQUIRE_FIELD_TOKEN_REPLAY") == "1":
            failures.append(
                "field token replay is required but tiktoken is unavailable: "
                f"{exc}. Install the workflow-pinned tiktoken version."
            )
        else:
            print(
                "Documentation facts: note: tiktoken unavailable; replayed "
                "the corpus and straitjacket behavior but skipped exact token counts.",
                file=sys.stderr,
            )
    else:
        field_token_replayed = True
        replayed_raw_tokens = token_count(raw)
        if replayed_raw_tokens != raw_tokens:
            failures.append(
                "evals/field-needle-record.json: raw o200k_base tokens are "
                f"{raw_tokens:,}, replay produced {replayed_raw_tokens:,}."
            )

    previous_state_home = os.environ.get("CTX_STATE_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="ctx-docs-field-state-") as state_home:
            os.environ["CTX_STATE_HOME"] = state_home
            replayed_sj = run_sj(raw, token_count)
    except Exception as exc:
        replayed_sj = {}
        failures.append(
            "evals/field_needle.py: current straitjacket arm did not replay: "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        if previous_state_home is None:
            os.environ.pop("CTX_STATE_HOME", None)
        else:
            os.environ["CTX_STATE_HOME"] = previous_state_home

    for key in (
        "quiet_needle_survived",
        "loud_needle_survived",
        "retrieval_address",
    ):
        if key in replayed_sj and replayed_sj[key] != sj.get(key):
            failures.append(
                f"evals/field-needle-record.json: sj {key} is {sj.get(key)!r}, "
                f"current replay produced {replayed_sj[key]!r}."
            )
    if field_token_replayed and replayed_sj.get("out_tokens") != visible_tokens:
        failures.append(
            "evals/field-needle-record.json: sj out_tokens is "
            f"{visible_tokens:,}, current replay produced "
            f"{replayed_sj.get('out_tokens')!r}."
        )
    if not quiet_needle_survived:
        failures.append(
            "evals/field-needle-record.json: the straitjacket arm no longer "
            "supports the published claim that the quiet needle survives."
        )
    if not retrieval_address:
        failures.append(
            "evals/field-needle-record.json: the straitjacket arm no longer "
            "supports the published claim that the digest emits a retrieval address."
        )

    field_claims = (
        f"{raw_tokens:,}",
        f"{visible_tokens:,}",
        f"{needle_line:,}",
    )
    for rel in (
        "README.md",
        "docs/WHY-STRAITJACKET.md",
        "site/src/content/docs/index.mdx",
    ):
        text = _read(root, rel)
        missing = [value for value in field_claims if value not in text]
        if missing:
            failures.append(
                f"{rel}: field-needle headline is missing record-derived "
                f"value(s) {', '.join(missing)}."
            )

    field_receipt = _read(root, "evals/field-needle-2026-07-20.md")
    expected_row_values = (
        f"| {visible_tokens:,} |",
        f"| {ratio:,}× |",
        "| **SURVIVED** | **yes** |",
    )
    if not all(value in field_receipt for value in expected_row_values):
        failures.append(
            "evals/field-needle-2026-07-20.md: straitjacket table does not "
            f"match the JSON record ({visible_tokens:,} tokens, {ratio:,}×)."
        )

    anchor_record = _json(root, "evals/anchor-drift-2026-08-20.json")
    from evals.anchor_drift import run as run_anchor_drift

    replayed_anchor = run_anchor_drift(
        seed=int(anchor_record["seed"]), files=int(anchor_record["files"])
    )
    if replayed_anchor != anchor_record:
        failures.append(
            "evals/anchor-drift-2026-08-20.json: record does not match the "
            "commit-pinned corpus and current resolver; rerun "
            "`python evals/anchor_drift.py --json` and review the receipt."
        )

    anchor = anchor_record["total"]
    anchored_wrong = int(anchor["anchored_wrong"])
    anchor_claims = (
        f"{int(anchor['cases']):,}",
        f"{int(anchor['relocated']):,}",
        f"{int(anchor['refused']):,}",
    )
    for rel in ("README.md", "docs/WHY-STRAITJACKET.md"):
        text = _read(root, rel)
        missing = [value for value in anchor_claims if value not in text]
        if missing:
            failures.append(
                f"{rel}: anchor-drift claim is missing record-derived "
                f"value(s) {', '.join(missing)}."
            )
        if anchored_wrong != 0:
            failures.append(
                f"{rel}: claims zero wrong-content resolutions, but the "
                f"anchor-drift record reports {anchored_wrong}."
            )
        elif not re.search(r"wrong content\s+zero\s+times", text, re.IGNORECASE):
            failures.append(
                f"{rel}: anchor-drift summary must state the record-derived "
                "zero wrong-content result."
            )

    if failures:
        print("Documentation fact-check failures:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"Documentation facts: PASS (version v{version}, "
        f"{intent_total} ctx ask intents, {tests:,} test functions, "
        f"field needle {raw_tokens:,}→{visible_tokens:,})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
