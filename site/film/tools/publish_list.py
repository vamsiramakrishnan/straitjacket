#!/usr/bin/env python3
"""Print, as JSON, the files a sandboxed page host needs beside site/film/film.html to show the film.

Such a host publishes the fragment itself as the page and every other file at a relative path. The fragment names its
diagrams at assets/diagrams/, so each of those is mapped from the copy the documentation site already mirrors
(site/public/diagrams/); index.html and fonts/ belong to the documentation site's build of the page and are left out.
Paths are relative to site/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SITE = Path(__file__).resolve().parents[2]
SERVED = SITE / "public" / "film"
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json",
         ".svg": "image/svg+xml", ".glb": "model/gltf-binary", ".bin": "application/octet-stream", ".png": "image/png",
         ".jpg": "image/jpeg", ".webp": "image/webp"}
files: dict[str, dict[str, str]] = {}
for path in sorted(p for p in SERVED.rglob("*") if p.is_file()):
    rel = path.relative_to(SERVED).as_posix()
    if rel == "index.html" or rel.startswith("fonts/") or path.suffix == ".LICENSE":
        continue
    files[rel] = {"from": path.relative_to(SITE).as_posix(), "contentType": TYPES[path.suffix]}
fragment = (SITE / "film" / "film.html").read_text(encoding="utf-8")
for name in sorted(set(re.findall(r"assets/diagrams/([\w.-]+\.svg)", fragment))):
    files[f"assets/diagrams/{name}"] = {"from": f"public/diagrams/{name}", "contentType": "image/svg+xml"}
print(json.dumps(files, indent=1))
