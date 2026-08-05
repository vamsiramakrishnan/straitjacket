"""Mine recorded agent sessions for the real distribution of shell commands.

Why this exists: the replacement surface grew from three shapes to eight, and
every one of those eight was chosen **by inspection** — someone thought about
which commands an agent probably runs. That is the same guessing the field scan
criticised in others, and it produces a surface tuned to our intuitions rather
than to agent behaviour.

This is the instrument that replaces the guess. It reads stream-json
transcripts from real sessions, extracts every shell command actually issued,
and reports three things:

1. **The distribution** — which programs agents reach for, by frequency.
2. **Coverage** — what fraction of those commands the replacement surface
   recognises today, and what fraction it categorically cannot.
3. **The head of the uncovered tail** — where the next rung is worth building.

Deliberately reports *commands*, not output bytes. The digest profiles already
cover output shapes well (~15 families); the gap this measures is on the
command side, where a substitution can prevent the flood instead of digesting
it.

Usage:

    python evals/command_corpus.py <dir-with-stream.jsonl-files> [...]
    python evals/command_corpus.py --json <dirs>      # machine-readable

Transcripts are found recursively (``stream.jsonl``), so a directory of round
outputs works directly. Nothing here writes, executes, or phones home; it is a
read-only scan over files you already have.
"""

from __future__ import annotations

import json
import shlex
import sys
from collections import Counter
from pathlib import Path

# Reuse the real recogniser rather than a copy of its rules — a coverage number
# computed against a re-implementation measures the re-implementation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ctx.substitute import _is_compound, _split, collapse  # noqa: E402


def iter_commands(paths):
    """Yield every shell command string issued across the given transcripts."""
    for root in paths:
        root = Path(root)
        files = [root] if root.is_file() else sorted(root.rglob("stream.jsonl"))
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    if block.get("name") not in ("Bash", "run_command"):
                        continue
                    args = block.get("input") or {}
                    cmd = args.get("command") or args.get("CommandLine")
                    if isinstance(cmd, str) and cmd.strip():
                        yield cmd.strip()


def program_of(command: str) -> str:
    """The leading program name, normalised. `None` when unparseable."""
    toks = _split(command)
    if not toks:
        return "?"
    prog = toks[0].rsplit("/", 1)[-1]
    # `python -m pytest` is a pytest run, not a python run; likewise `git grep`.
    if prog in ("python", "python3") and len(toks) > 2 and toks[1] == "-m":
        return toks[2]
    if prog == "git" and len(toks) > 1 and not toks[1].startswith("-"):
        return f"git {toks[1]}"
    return prog


def classify(command: str) -> str:
    """How the replacement surface treats this command today."""
    toks = _split(command)
    if not toks:
        return "unparseable"
    if _is_compound(toks, command):
        # Declined by rule, not by omission: a pipe/redirect/chain means the
        # operator is composing, and rewriting one half changes the whole.
        return "compound"
    return "substituted" if collapse(command, failure_available=True,
                                     symbols_resolvable=True) else "bare-uncovered"


def report(paths, as_json=False):
    commands = list(iter_commands(paths))
    if not commands:
        print("no commands found — is the path right?", file=sys.stderr)
        return 1

    verdicts = Counter()
    progs = Counter()
    uncovered = Counter()
    compound_progs = Counter()
    for c in commands:
        v = classify(c)
        verdicts[v] += 1
        p = program_of(c)
        progs[p] += 1
        if v == "bare-uncovered":
            uncovered[p] += 1
        elif v == "compound":
            compound_progs[p] += 1

    total = len(commands)
    data = {
        "commands": total,
        "distinct_programs": len(progs),
        "verdicts": dict(verdicts),
        "top_programs": progs.most_common(25),
        "uncovered_bare": uncovered.most_common(15),
        "compound_by_program": compound_progs.most_common(15),
    }
    if as_json:
        print(json.dumps(data, indent=2))
        return 0

    def pct(n):
        return f"{100.0 * n / total:5.1f}%"

    print(f"# command corpus — {total:,} commands from {len(list(paths))} path(s)")
    print(f"# {len(progs)} distinct programs\n")

    print("## What the replacement surface does with them today\n")
    for verdict in ("substituted", "bare-uncovered", "compound", "unparseable"):
        n = verdicts.get(verdict, 0)
        print(f"  {verdict:16} {n:>6,}  {pct(n)}")

    print("\n## The distribution (top 25 by frequency)\n")
    for prog, n in progs.most_common(25):
        print(f"  {prog:24} {n:>6,}  {pct(n)}")

    print("\n## Head of the uncovered tail — BARE invocations with no rung\n")
    print("   (these are the ones a new rung could actually reach)\n")
    for prog, n in uncovered.most_common(15):
        print(f"  {prog:24} {n:>6,}  {pct(n)}")

    print("\n## Compound commands by program — declined by rule, not omission\n")
    for prog, n in compound_progs.most_common(15):
        print(f"  {prog:24} {n:>6,}  {pct(n)}")
    return 0


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__.strip().split("Usage:")[1].strip(), file=sys.stderr)
        return 2
    return report(args, as_json="--json" in argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
