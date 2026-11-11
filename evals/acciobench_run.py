#!/usr/bin/env python3
"""Top-level CLI entrypoint for AccioBench (CommerceAgentBench x Straitjacket).

Usage:
    python evals/acciobench_run.py doctor --dataset-root /path/to/CommerceAgentBench
    python evals/acciobench_run.py validate --dataset-root /path/to/CommerceAgentBench --limit 2
    python evals/acciobench_run.py run --dataset-root /path/to/CommerceAgentBench --arm raw,sj --sample 10
    python evals/acciobench_run.py report --workdir ./.acciobench/workdir --format markdown
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.dont_write_bytecode = True

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_SRC = _REPO_ROOT / "src"
for _path in (_REPO_ROOT, _REPO_SRC):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evals.acciobench.cli import main  # noqa: E402

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
