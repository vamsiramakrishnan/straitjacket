#!/usr/bin/env python3
"""Fail CI when local documentation links or repository-hosted images are broken."""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

OWNER_REPO = "vamsiramakrishnan/straitjacket"
RAW_PREFIXES = (
    f"https://raw.githubusercontent.com/{OWNER_REPO}/main/",
    f"https://raw.githubusercontent.com/{OWNER_REPO}/refs/heads/main/",
)
HTML_RE = re.compile(r"\b(?:src|srcset|href)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
MARKDOWN_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
HTML_ID_RE = re.compile(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


def local_target(md_path: Path, value: str, repo_root: Path) -> Path | None:
    value = value.strip()
    if not value or value.startswith(("#", "mailto:", "data:", "javascript:")):
        return None
    for prefix in RAW_PREFIXES:
        if value.startswith(prefix):
            path = value[len(prefix):].split("?", 1)[0].split("#", 1)[0]
            return repo_root / PurePosixPath(unquote(path))

    parsed = urlsplit(value)
    if parsed.scheme or value.startswith("//"):
        return None
    path = unquote(parsed.path)
    if not path:
        return None
    return (md_path.parent / path).resolve()


def validate_svg(path: Path) -> str | None:
    try:
        head = path.read_text(encoding="utf-8")[:4096].lower()
    except UnicodeDecodeError:
        return "SVG is not UTF-8 text"
    if "<svg" not in head:
        return "file does not contain an <svg> root"
    return None


def markdown_anchors(path: Path) -> set[str]:
    """Return rendered heading IDs and explicit HTML IDs for a Markdown file."""
    text = path.read_text(encoding="utf-8")
    anchors = set(HTML_ID_RE.findall(text))
    counts: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if not match:
            continue
        heading = html.unescape(match.group(1))
        heading = re.sub(r"`([^`]*)`", r"\1", heading)
        heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
        heading = re.sub(r"<[^>]+>", "", heading)
        slug = "".join(
            char.lower()
            for char in heading
            if char.isalnum() or char in {" ", "-", "_"}
        )
        slug = re.sub(r"\s+", "-", slug).strip("-")
        if not slug:
            continue
        duplicate = counts.get(slug, 0)
        counts[slug] = duplicate + 1
        anchors.add(slug if duplicate == 0 else f"{slug}-{duplicate}")
    return anchors


def main() -> int:
    repo_root = Path.cwd().resolve()
    docs_root = repo_root / "docs"
    failures: list[str] = []
    checked = 0
    anchor_cache: dict[Path, set[str]] = {}

    for md_path in sorted(docs_root.rglob("*.md")):
        text = md_path.read_text(encoding="utf-8")
        refs = [*HTML_RE.findall(text), *MARKDOWN_RE.findall(text)]
        for value in refs:
            target = local_target(md_path, value, repo_root)
            parsed = urlsplit(value.strip())
            fragment = unquote(parsed.fragment)
            if target is None and fragment and not parsed.scheme and not value.startswith("//"):
                target = md_path
            if target is None:
                continue
            checked += 1
            try:
                target.relative_to(repo_root)
            except ValueError:
                failures.append(f"{md_path.relative_to(repo_root)}: link escapes repository: {value}")
                continue
            if not target.exists():
                failures.append(f"{md_path.relative_to(repo_root)}: missing target: {value} -> {target.relative_to(repo_root)}")
                continue
            if fragment and target.suffix.lower() in {".md", ".mdx"}:
                anchors = anchor_cache.setdefault(target, markdown_anchors(target))
                if fragment not in anchors:
                    failures.append(
                        f"{md_path.relative_to(repo_root)}: missing fragment "
                        f"#{fragment} in {target.relative_to(repo_root)}"
                    )
            if target.suffix.lower() == ".svg":
                error = validate_svg(target)
                if error:
                    failures.append(f"{md_path.relative_to(repo_root)}: {value}: {error}")

    if failures:
        print("Documentation integrity failures:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"Documentation integrity: PASS ({checked} local/repository references checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
