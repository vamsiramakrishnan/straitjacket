#!/usr/bin/env python3
"""What must be true of the film before it is published. Exit status 1 with the reasons if not.

    python site/film/tools/check_film.py

1. site/public/film/data/ is what make_data.py writes from the receipts, and index.html is what build_page.py
   writes from film.html.
2. Every number marked `data-fact="path.in.film.json"` in film.html is that value of film.json, written the way the
   page writes numbers. The page cannot drift from the receipts unnoticed.
3. The digest printed in the page is tests/fixtures/field_needle_main_digest.txt, line for line.
4. The served page asks no other host for anything; every file it refers to exists; the favicon is the repository's.
5. No file is larger than its budget.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
FRAGMENT = REPO / "site" / "film" / "film.html"
SERVED = REPO / "site" / "public" / "film"
FONT_SERVICE = "https://fonts.googleapis.com/"
BUDGET = {".glb": 2_000_000, ".js": 800_000, ".mjs": 200_000, ".css": 120_000, ".json": 60_000, ".bin": 60_000,
          ".html": 80_000, ".png": 400_000, ".jpg": 400_000, ".webp": 400_000, ".woff2": 60_000, ".svg": 20_000}
TOTAL_BUDGET = 12_000_000
bad: list[str] = []

for tool in ("make_data.py", "build_page.py"):
    done = subprocess.run([sys.executable, str(TOOLS / tool), "--check"], capture_output=True, text=True)
    if done.returncode:
        bad.append((done.stdout + done.stderr).strip().splitlines()[-1])

page = FRAGMENT.read_text(encoding="utf-8")
film = json.loads((SERVED / "data" / "film.json").read_text(encoding="utf-8"))


def lookup(path: str):
    value = film
    for key in path.split("."):
        value = value[key]
    return value


def shown(value) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


facts = re.findall(r'data-fact="([^"]+)"[^>]*>([^<]*)<', page)
for path, text in facts:
    try:
        want = shown(lookup(path))
    except (KeyError, TypeError):
        bad.append(f"data-fact {path}: not in film.json")
        continue
    if text.strip() != want:
        bad.append(f"data-fact {path}: the page says {text.strip()!r}, the receipt says {want!r}")
if len(facts) < 20:
    bad.append(f"only {len(facts)} marked numbers found in film.html: the markers have been lost")

block = re.search(r'id="digest-specimen".*?<code>(.*?)</code>', page, re.S)
printed = html.unescape(re.sub(r"<[^>]+>", "", block.group(1))) if block else ""
want_lines, got_lines = film["digest"]["text"].rstrip("\n").split("\n"), printed.rstrip("\n").split("\n")
if want_lines != got_lines:
    first = next((i for i, (a, b) in enumerate(zip(want_lines, got_lines)) if a != b), min(len(want_lines), len(got_lines)))
    bad.append(f"the digest in the page differs from the fixture at line {first + 1}")

# The served document: nothing from another host, every reference resolves.
index = (SERVED / "index.html").read_text(encoding="utf-8") if (SERVED / "index.html").is_file() else ""
for ref in re.findall(r'<(?:link|script|img|source)\b[^>]*?\b(?:href|src|srcset)="([^"]+)"', index):
    if re.match(r"[a-z]+:", ref):
        bad.append(f"index.html loads from another host: {ref}")
    elif not (SERVED / ref.split("#")[0].split("?")[0]).resolve().is_file():
        bad.append(f"index.html refers to a missing file: {ref}")
for ref in re.findall(r'<(?:link|script|img|source)\b[^>]*?\b(?:href|src|srcset)="([a-z]+:[^"]+)"', page):
    if not ref.startswith(FONT_SERVICE):
        bad.append(f"film.html loads from another host: {ref}")
for source in sorted(list(SERVED.rglob("*.js")) + list(SERVED.rglob("*.mjs")) + list(SERVED.rglob("*.css"))):
    if source.parent.name == "vendor":
        continue
    text = source.read_text(encoding="utf-8")
    for ref in re.findall(r"""(?:from\s+|import\s*\(\s*|url\(\s*|fetch\(\s*)['"]?(https?://[^'")\s]+)""", text):
        bad.append(f"{source.relative_to(SERVED)} loads from another host: {ref}")
    for ref in re.findall(r"""(?:from\s+|import\s*\(\s*)['"](\.{1,2}/[^'"]+)['"]""", text):
        if not (source.parent / ref).resolve().is_file():
            bad.append(f"{source.relative_to(SERVED)} imports a missing file: {ref}")
    for ref in re.findall(r"""url\(\s*['"]?(?!data:)([^'")\s]+)""", text) if source.suffix == ".css" else ():
        if not (source.parent / ref.split("#")[0]).resolve().is_file():
            bad.append(f"{source.relative_to(SERVED)} refers to a missing file: {ref}")

favicon, mark = SERVED / "assets" / "favicon.svg", REPO / "assets" / "icon" / "favicon.svg"
if not favicon.is_file() or favicon.read_bytes() != mark.read_bytes():
    bad.append("site/public/film/assets/favicon.svg is not assets/icon/favicon.svg")

total = 0
for file in sorted(p for p in SERVED.rglob("*") if p.is_file()):
    size = file.stat().st_size
    total += size
    limit = BUDGET.get(file.suffix)
    if limit is not None and size > limit:
        bad.append(f"{file.relative_to(SERVED)} is {size:,} bytes (budget {limit:,})")
if total > TOTAL_BUDGET:
    bad.append(f"the film is {total:,} bytes in all (budget {TOTAL_BUDGET:,})")

print(f"film: {len(facts)} marked numbers against film.json, digest {len(got_lines)} lines, "
      f"{sum(1 for p in SERVED.rglob('*') if p.is_file())} served files, {total:,} bytes")
for line in bad:
    print("FAIL", line)
sys.exit(1 if bad else 0)
