"""Optional ripgrep-backed repo search engine — auto-detected, transparent
fallback to the pure-Python engine in ``search.py`` when absent (SPEC §6.3,
CONTRIBUTING.md convention 1: opportunistic binary, deterministic fallback)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ctx.textutil import fmt_bytes, fmt_int
from ctx.workspace import Workspace


@dataclass(frozen=True, slots=True)
class RgMatch:
    target: str
    line_no: int
    pattern_index: int
    line: str


def _rg_available() -> bool:
    import os
    import shutil

    if os.environ.get("CTX_SEARCH_ENGINE") == "python":
        return False
    return shutil.which("rg") is not None


def _rg_repo_search(
    ws: Workspace,
    paths: list[str],
    patterns: list[str],
    rxs: list["re.Pattern[str]"],
    *,
    fixed: bool,
    glob: str | None,
) -> tuple[list[RgMatch], str, int] | None:
    """Repo search via ripgrep (SIMD prefilter, parallel walk, native
    gitignore). Returns (matches, coverage line) or None to fall back.

    Determinism: ``--sort path`` plus our final (target, line, pattern) sort.
    Ignore policy: rg's own .gitignore handling plus our deny globs; the
    pattern-index for ordering is recovered by re-matching the emitted line.
    """
    import json as _json
    import subprocess

    argv = ["rg", "--json", "--no-config", "--sort", "path", "--stats"]
    if fixed:
        argv.append("--fixed-strings")
    if not (ws.git is not None and ws.config.workspace.respect_gitignore):
        argv.append("--no-ignore")
    if not ws.config.workspace.follow_symlinks:
        pass  # rg does not follow symlinks by default
    argv.append("--hidden")  # parity with the Python engine's os.walk
    if glob:
        argv += ["--glob", glob]
    # Deny globs come after the include glob: rg gives the last matching
    # glob precedence, and capture exclusions must always win.
    argv += ["--glob", "!.git/**"]
    for deny in ws.ignore_globs:
        argv += ["--glob", f"!{deny}"]
    for p in patterns:
        argv += ["-e", p]
    argv += ["--"] + (paths or ["."])

    try:
        proc = subprocess.run(
            argv, cwd=ws.root, capture_output=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):  # 2 = error (bad pattern already caught)
        return None

    matches: list[RgMatch] = []
    scanned = ""
    bytes_searched = 0
    for raw_line in proc.stdout.splitlines():
        try:
            msg = _json.loads(raw_line)
        except _json.JSONDecodeError:
            continue
        mtype = msg.get("type")
        data = msg.get("data") or {}
        if mtype == "match":
            path_obj = data.get("path") or {}
            lines_obj = data.get("lines") or {}
            if "text" not in path_obj or "text" not in lines_obj:
                continue  # non-UTF-8 path/line: python engine handles via lossy decode
            line = lines_obj["text"].rstrip("\n")
            pi = next((i for i, rx in enumerate(rxs) if rx.search(line)), 0)
            rel = path_obj["text"]
            if rel.startswith("./"):
                rel = rel[2:]
            matches.append(
                RgMatch(
                    target=rel.replace("\\", "/"),
                    line_no=int(data.get("line_number") or 0),
                    pattern_index=pi,
                    line=line,
                )
            )
        elif mtype == "summary":
            stats = data.get("stats") or {}
            bytes_searched = int(stats.get("bytes_searched", 0))
            # rg's prefilter proves most files cannot match without a full
            # scan; coverage over the glob/ignore-filtered corpus is complete.
            scanned = (
                "  scanned: complete over corpus · "
                f"{fmt_int(int(stats.get('searches', 0)))} deep-searched · "
                f"{fmt_bytes(bytes_searched)}"
            )
    matches.sort(key=lambda m: (m.target, m.line_no, m.pattern_index))
    return matches, scanned or "  scanned: complete over corpus", bytes_searched
