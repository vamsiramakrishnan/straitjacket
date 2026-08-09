#!/usr/bin/env python3
"""Reachability audit: find capabilities that exist but never reach the agent.

Two defects found by hand during an eval run turned out to be the same species,
and neither was a logic error -- both mechanisms worked exactly as written:

  * the navigation governor never saw the host's native `Grep` tool. The
    PostToolUse matcher routed those events to it; the handler discarded them
    for not being Bash. Matcher and handler disagreed.
  * `ctx map` / `def` / `refs` appeared in exactly one string in the whole
    codebase, so a structural query never reached the repo map however large
    its result.

Neither is visible to a test suite: nothing is broken, a capability is simply
unreachable. Both ARE visible to a few seconds of static and dynamic probing,
which is what this does. Model-free, no API cost, seconds to run -- cheap
enough to sit in front of any paid eval, because a benchmark cannot measure a
capability the agent is never offered.

    python evals/reachability_audit.py

Probes:
  A  dead verbs        -- CLI verbs never named in any agent-facing string
  B  broken offers     -- `ctx <verb>` suggestions naming a verb that is gone
  C  discarded events  -- hook matchers routing tools the handlers ignore

Exit is non-zero when probe B finds anything: a suggestion naming a verb that
does not exist is a defect outright, not a candidate. A and C report suspects
for triage -- some dead verbs are deliberate (operator-facing, not agent-facing)
and the audit cannot know which, so it ranks rather than fails.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "ctx"

# Verbs whose audience is the operator at a terminal, not the agent in a
# transcript. Silence about these is correct, so they are not suspects.
OPERATOR_ONLY = {
    "init", "doctor", "gc", "pin", "wrap", "proxy", "antigravity", "install",
    "checkpoint", "policy", "compile", "show", "surface", "orchestrate",
    "replay", "ops", "ladders", "add", "list", "resolve", "debt",
}


# Verbs handled before argparse ever sees the args (cli.py intercepts these on
# argv[0]), so they never appear as add_parser calls. Omitting them makes the
# audit report a working command as a broken offer.
PRE_PARSER_VERBS = {"help"}


def _top_level_verbs(cli: str) -> list[str]:
    """Only the top-level parser. `debt_sub.add_parser("add")` is `ctx debt add`,
    a different string, and counting it as a bare verb invents dead capabilities
    that were never named that way."""
    parsed = set(re.findall(r'(?<![\w_])sub\.add_parser\(\s*"([a-z][a-z-]*)"', cli))
    return sorted(parsed | PRE_PARSER_VERBS)


def _agent_facing_text() -> str:
    """Every string that can land in a transcript: digests, hooks, nudges."""
    parts = []
    for p in SRC.rglob("*.py"):
        parts.append(p.read_text(errors="ignore"))
    return "\n".join(parts)


def probe_dead_verbs() -> list[dict]:
    cli = (SRC / "cli.py").read_text()
    verbs = _top_level_verbs(cli)
    blob = _agent_facing_text()
    out = []
    for v in verbs:
        if v in OPERATOR_ONLY:
            continue
        n = len(re.findall(rf"ctx {re.escape(v)}\b", blob))
        if n == 0:
            out.append({"verb": v, "mentions": 0})
    return out


# An OFFER is something the agent could paste and run, so the verb has to be
# followed by argument-shaped text: a flag, a quoted argument, a <placeholder>,
# or a handle like repo:/run:. Emitted prose says things like "route into ctx
# for Y" and "ctx handles the rest"; without this the audit reports those as
# missing verbs and buries the one real finding.
OFFER_RE = re.compile(
    r"(?:^|[`\s(])ctx (?P<verb>[a-z][a-z-]{1,20})"
    r"(?=\s+(?:-{1,2}\w|['\"<]|(?:repo|run|blob|job|span):|--))"
)


def _emitted_strings() -> list[str]:
    """String literals that can be EMITTED, excluding docstrings and comments.

    Scanning raw source instead matches English: "the directory ctx owns",
    "hosts ctx would have to build". Those are prose about the tool, not
    offers to an agent, and counting them buries the real finding under
    fifty false positives.
    """
    import ast

    out: list[str] = []
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    if isinstance(body[0].value.value, str):
                        docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    out.append(node.value)
    return out


def probe_broken_offers() -> list[dict]:
    """Any `ctx <verb>` we emit must name a verb the CLI still has. Catches a
    rename that updated the parser and left the suggestion strings behind."""
    cli = (SRC / "cli.py").read_text()
    known = set(_top_level_verbs(cli))
    counts: dict[str, int] = {}
    for text in _emitted_strings():
        for m in OFFER_RE.finditer(text):
            verb = m.group("verb")
            if verb not in known and verb not in {"hook"}:
                counts[verb] = counts.get(verb, 0) + 1
    return [{"verb": v, "occurrences": n} for v, n in sorted(counts.items())]


# A representative PostToolUse payload per tool the matcher routes. The point is
# not realism of content but whether the handler does ANYTHING with the shape.
def _payloads(cwd: str) -> dict[str, dict]:
    # Large enough to exceed the emission budget. A small payload is supposed
    # to pass through untouched, so probing with one makes every handler look
    # dead when it is merely under budget.
    flood = "\n".join(
        f"src/pkg/mod{i % 90}.py:{i}:    value = lookup(key_{i}) # {'x' * 60}"
        for i in range(1400)
    )
    return {
        "Bash": {"tool_input": {"command": "grep -rn lookup src/"}, "tool_response": flood},
        "Grep": {"tool_input": {"pattern": "lookup", "path": "src/"}, "tool_response": flood},
        "Glob": {"tool_input": {"pattern": "**/*.py"}, "tool_response": flood},
        "Read": {"tool_input": {"file_path": "src/ctx/cli.py"}, "tool_response": flood},
        "WebFetch": {"tool_input": {"url": "https://example.com"}, "tool_response": flood},
        "WebSearch": {"tool_input": {"query": "lookup"}, "tool_response": flood},
        "Task": {"tool_input": {"prompt": "investigate"}, "tool_response": flood},
        "mcp__x__search_code": {"tool_input": {"query": "lookup"}, "tool_response": flood},
    }


def _matcher_tools() -> list[str]:
    """Tool names named in the wrap config's PostToolUse matcher."""
    try:
        cfg = subprocess.run(
            [sys.executable, "-m", "ctx", "wrap", "claude", "--print-config"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        ).stdout
        doc = json.loads(cfg)
    except Exception:
        return []
    tools: list[str] = []
    for entry in doc.get("hooks", {}).get("PostToolUse", []):
        for part in str(entry.get("matcher", "")).split("|"):
            part = part.strip()
            if part and part.replace("_", "").replace(".", "").replace("*", "").isalnum():
                tools.append(part)
    return tools


def probe_discarded_events(verbose: bool = False) -> list[dict]:
    """Send one payload per routed tool; report tools the handlers do nothing with.

    A no-op for every payload means the matcher pays the process-spawn cost on
    every one of those tool calls and the agent gets nothing back.
    """
    rows = []
    with tempfile.TemporaryDirectory(prefix="reach_") as td:
        ws = pathlib.Path(td)
        subprocess.run(["git", "init", "-q", "."], cwd=ws, capture_output=True)
        (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")

        matcher = _matcher_tools()
        payloads = _payloads(str(ws))
        for tool in matcher:
            body = payloads.get(tool)
            if body is None:
                continue
            acted = False
            # Three distinct symbols: the navigation governor fires on the
            # third, so a single probe would under-report it as inert.
            for sym in ("alpha_sym", "beta_sym", "gamma_sym"):
                probe = json.loads(json.dumps(body))
                ti = probe["tool_input"]
                for key in ("command", "pattern", "query"):
                    if key in ti:
                        ti[key] = ti[key].replace("lookup", sym)
                payload = {"tool_name": tool, "cwd": str(ws), **probe}
                proc = subprocess.run(
                    [sys.executable, "-m", "ctx", "hook", "claude-code", "post-tool-use"],
                    cwd=ws, input=json.dumps(payload), capture_output=True, text=True, timeout=120,
                )
                try:
                    doc = json.loads(proc.stdout or "{}")
                except Exception:
                    doc = {}
                acted = acted or (bool(doc) and doc != {})
            rows.append({"tool": tool, "routed": True, "handler_acted": acted})
            if verbose:
                print(f"    {tool:<22} {'acted' if acted else 'NO-OP'}", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print("== A. capabilities never offered to an agent ==")
    dead = probe_dead_verbs()
    for d in dead:
        print(f"  SUSPECT  ctx {d['verb']:<10} named in 0 agent-facing strings")
    if not dead:
        print("  (none)")

    print("\n== B. offers naming a verb the CLI does not have ==")
    broken = probe_broken_offers()
    for b in broken:
        print(f"  DEFECT   ctx {b['verb']:<10} suggested {b['occurrences']}x, no such verb")
    if not broken:
        print("  (none)")

    print("\n== C. tool events routed to a hook that ignores them ==")
    rows = probe_discarded_events(args.verbose)
    noop = [r for r in rows if not r["handler_acted"]]
    for r in noop:
        print(f"  SUSPECT  {r['tool']:<22} matcher routes it, handlers returned no-op")
    if not noop:
        print("  (none)")
    if not rows:
        print("  (matcher unavailable — skipped)")

    print(f"\nsuspects: {len(dead)} unreachable verb(s), {len(noop)} discarded event shape(s)")
    print(f"defects:  {len(broken)} broken offer(s)")
    print("\nA and C are candidates, not verdicts: some silence is deliberate.\n"
          "B is a defect outright — a suggestion the agent cannot run.")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
