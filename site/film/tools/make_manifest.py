#!/usr/bin/env python3
"""Write assets/manifest.json: which props exist as GLB files, and the baked contact shadow of each (if any).

    python tools/make_manifest.py [ASSETS_DIR]

The scene reads this one file and loads only what it lists, so a prop that has not been built yet is simply drawn
as its stand-in, with no failed request. Contact shadows come from contact.json, which bake_contact.py writes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROPS = ("console", "capture_gate", "context_frame", "evidence_store", "ledger_stand")
here = Path(__file__).resolve()
default = next((p for p in (here.parents[2] / "public" / "film" / "assets", here.parents[1] / "site" / "assets") if p.is_dir()), None)
assets = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default
contact = json.loads((assets / "contact.json").read_text())["props"] if (assets / "contact.json").is_file() else {}
props = {}
for name in PROPS:
    if (assets / f"{name}.glb").is_file():
        props[name] = {"file": f"{name}.glb", "bytes": (assets / f"{name}.glb").stat().st_size}
        if name in contact and (assets / contact[name]["file"]).is_file():
            props[name]["contact"] = contact[name]
(assets / "manifest.json").write_text(json.dumps({"props": props}, indent=1) + "\n")
print("wrote", assets / "manifest.json", "props:", ", ".join(props) or "none yet")
