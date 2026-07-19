#!/usr/bin/env python3
"""Normalize SVG embeds under docs/ so they render on GitHub and GitHub Pages.

The repository publishes documentation from docs/, while the visual assets currently
live at repository-root assets/. Parent-relative embeds work in GitHub's repository
view but escape the Pages base path. This script converts those embeds to a stable raw
GitHub URL and validates that every target exists locally.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

OWNER_REPO = "vamsiramakrishnan/straitjacket"
BRANCH = "main"
RAW_PREFIX = f"https://raw.githubusercontent.com/{OWNER_REPO}/{BRANCH}/"

HTML_ATTR_RE = re.compile(
    r"(?P<prefix>\b(?:src|srcset)\s*=\s*(?P<quote>['\"]))"
    r"(?P<value>[^'\"]+\.svg(?:[?#][^'\"]*)?)"
    r"(?P=quote)",
    re.IGNORECASE,
)
MARKDOWN_IMAGE_RE = re.compile(
    r"(?P<prefix>!\[[^\]]*\]\()(?P<value>[^)\s]+\.svg(?:[?#][^)]*)?)(?P<suffix>\))",
    re.IGNORECASE,
)


def repo_path_for_reference(md_path: Path, value: str, repo_root: Path) -> Path | None:
    """Return the referenced repository path, or None for unrelated external URLs."""
    parsed = urlsplit(value)
    clean = unquote(parsed.path)

    if value.startswith(RAW_PREFIX):
        return repo_root / PurePosixPath(value[len(RAW_PREFIX):].split("?", 1)[0].split("#", 1)[0])

    if parsed.scheme or value.startswith("//") or value.startswith("data:"):
        return None

    candidate = (md_path.parent / clean).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"reference escapes repository: {md_path}: {value}") from exc
    return candidate


def normalized_value(md_path: Path, value: str, repo_root: Path) -> str:
    target = repo_path_for_reference(md_path, value, repo_root)
    if target is None:
        return value
    if not target.exists():
        raise FileNotFoundError(f"missing SVG: {md_path}: {value} -> {target}")

    parsed = urlsplit(value)
    suffix = ""
    if parsed.query:
        suffix += f"?{parsed.query}"
    if parsed.fragment:
        suffix += f"#{parsed.fragment}"

    repo_relative = target.resolve().relative_to(repo_root.resolve()).as_posix()
    return f"{RAW_PREFIX}{repo_relative}{suffix}"


def transform(md_path: Path, repo_root: Path) -> tuple[str, int]:
    original = md_path.read_text(encoding="utf-8")
    replacements = 0

    def html_replace(match: re.Match[str]) -> str:
        nonlocal replacements
        value = match.group("value")
        new_value = normalized_value(md_path, value, repo_root)
        if new_value == value:
            return match.group(0)
        replacements += 1
        quote = match.group("quote")
        return f"{match.group('prefix')}{new_value}{quote}"

    def markdown_replace(match: re.Match[str]) -> str:
        nonlocal replacements
        value = match.group("value")
        new_value = normalized_value(md_path, value, repo_root)
        if new_value == value:
            return match.group(0)
        replacements += 1
        return f"{match.group('prefix')}{new_value}{match.group('suffix')}"

    updated = HTML_ATTR_RE.sub(html_replace, original)
    updated = MARKDOWN_IMAGE_RE.sub(markdown_replace, updated)
    return updated, replacements


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail instead of rewriting")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    docs_root = repo_root / "docs"
    if not docs_root.is_dir():
        print("error: run from the straitjacket repository root", file=sys.stderr)
        return 2

    changed: list[tuple[Path, int]] = []
    errors: list[str] = []
    for md_path in sorted(docs_root.rglob("*.md")):
        try:
            updated, count = transform(md_path, repo_root)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if count:
            changed.append((md_path, count))
            if not args.check:
                md_path.write_text(updated, encoding="utf-8")

    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    if args.check and changed:
        for path, count in changed:
            print(f"UNNORMALIZED {path.relative_to(repo_root)}: {count} SVG reference(s)")
    elif not args.check:
        for path, count in changed:
            print(f"UPDATED {path.relative_to(repo_root)}: {count} SVG reference(s)")

    return 1 if errors or (args.check and changed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
