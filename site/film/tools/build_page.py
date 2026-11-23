#!/usr/bin/env python3
"""Write site/public/film/index.html, the page the documentation site serves, from site/film/film.html.

    python site/film/tools/build_page.py            # write it
    python site/film/tools/build_page.py --check    # fail if it is not what this script writes

film.html is the page as it is authored: a fragment without <html>, <head> or <body>, with its diagrams at
assets/diagrams/ and its two typefaces from a font service. That form is what a sandboxed page host publishes.
The documentation site needs a whole document, and it must not ask another host for anything, so this script:

  - wraps the fragment in a document and lifts <title>, <meta> and <link> into <head>;
  - replaces the font service's stylesheet with fonts/fonts.css (the same faces, self-hosted);
  - points the diagrams at ../diagrams/, the copies the site already mirrors from assets/readme/diagrams.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FRAGMENT = REPO / "site" / "film" / "film.html"
TARGET = REPO / "site" / "public" / "film" / "index.html"
HEAD_TAG = re.compile(r"^(<title>.*?</title>|<meta [^>]*>|<link [^>]*>)\s*$", re.M)
FONT_SERVICE = re.compile(r'<link rel="stylesheet" href="https://fonts\.googleapis\.com/[^"]*">')
SKELETON = (":root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
            "body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}")


def build() -> str:
    fragment = FRAGMENT.read_text(encoding="utf-8")
    if len(FONT_SERVICE.findall(fragment)) != 1:
        raise SystemExit("film.html must name the font service's stylesheet exactly once")
    fragment = FONT_SERVICE.sub('<link rel="stylesheet" href="fonts/fonts.css">', fragment)
    fragment = fragment.replace("assets/diagrams/", "../diagrams/")
    head = [m.group(1) for m in HEAD_TAG.finditer(fragment)]
    body = HEAD_TAG.sub("", fragment).lstrip("\n")
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            "<link rel=\"icon\" href=\"assets/favicon.svg\" type=\"image/svg+xml\">\n"
            f"<style>{SKELETON}</style>\n" + "\n".join(head) + "\n</head>\n<body>\n" + body + "</body>\n</html>\n")


def main() -> int:
    document = build()
    if "--check" in sys.argv:
        if not TARGET.is_file() or TARGET.read_text(encoding="utf-8") != document:
            print("stale site/public/film/index.html (run site/film/tools/build_page.py)")
            return 1
        print("site/public/film/index.html is current")
        return 0
    TARGET.write_text(document, encoding="utf-8")
    print("wrote", TARGET.relative_to(REPO), len(document), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
