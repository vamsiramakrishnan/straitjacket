"""Thin eval-side wrapper for the session-history replay simulator.

The mechanism was promoted to a first-class verb: ``ctx replay`` (module
``ctx.replay``, ROADMAP M-F). This shim keeps the original eval entry point
working for archived-transcript studies:

    python evals/replay_sim.py <transcript.jsonl> [...]
    python evals/replay_sim.py --self          # this machine's history
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx.replay import default_history_paths, render_report, simulate_session  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcripts", nargs="*")
    ap.add_argument("--self", action="store_true", help="mine ~/.claude/projects")
    ap.add_argument("--gaps", action="store_true")
    args = ap.parse_args()
    paths = list(args.transcripts)
    if getattr(args, "self"):
        paths += default_history_paths()
    if not paths:
        ap.error("no transcripts given (pass paths or --self)")
    print(render_report([simulate_session(p) for p in paths], gaps=args.gaps))


if __name__ == "__main__":
    main()
