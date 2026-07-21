"""Collapse substitution — the replacement-surface layer.

The neighbouring tools (wozcode, Maki) win adoption not by *teaching* an agent
to prefer a cheaper op but by *replacing* the tool surface: the efficient path
IS the tool the agent already reaches for. straitjacket already has the collapse
engine (``ctx q`` — a symbol index, bounded search, a failure slice) and the
lossless store behind it; what it lacked was delivery. This module is that
delivery, done the straitjacket way — transparent substitution under the
agent's own shell commands, so no new tool schema is added to the window and
the collapsed op is chosen *for* the model, not *by* it.

Given a shell command the agent is about to run, ``collapse`` recognises a few
high-value *loop shapes* — the search-read-search navigation loop, chiefly —
and returns the collapsed, addressable ``ctx q`` op that answers the same
question in one bounded call instead of a grep dump the next turn re-ingests.

Design constraints, all deliberate:

  * **Pure and total.** No I/O, no store access, no shell-out; a token scan and
    a table. Trivially testable, and it can never hang or flood.
  * **Cheapest rung first (the Ponytail ladder).** Recognisers are ordered so
    the first match is the cheapest equivalent; each carries its ``rung`` for
    the ctx-debt ledger.
  * **Conservative.** Anything ambiguous returns ``None`` and the command runs
    untouched (and is still bounded at the emission gate). We substitute only
    when the collapsed op is provably equivalent to what the agent asked for.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# a bare identifier — the signal that a search is for a *symbol* (→ refs, which
# resolves through the symbol index) rather than free text (→ bounded search).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# `grep`-family programs whose recursive form is a navigation-loop smell.
_GREP_PROGS = {"grep", "egrep", "fgrep", "rg", "ack", "ag"}

# flags that take a value we must skip when hunting for the pattern argument.
_GREP_VALUE_FLAGS = {
    "-e", "-f", "-m", "--max-count", "-A", "-B", "-C", "--glob", "-g",
    "--after-context", "--before-context", "--context", "--include", "--exclude",
}

# shell operators that make a command compound — a pipe/redirect/chain changes
# what the agent asked for (`grep … | wc -l` counts; `grep … > f` redirects), so
# the whole-command substitution would be wrong. We substitute only a *bare*
# invocation; anything compound passes through untouched (still bounded at the
# emission gate).
_SHELL_OPS = {"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "2>", "2>>", "|&", "1>"}


def _is_compound(toks: list[str], raw: str) -> bool:
    if any(t in _SHELL_OPS for t in toks):
        return True
    return "$(" in raw or "`" in raw  # command substitution


@dataclass(frozen=True)
class Substitution:
    """A collapsed op that replaces a loop-shape command."""
    command: str   # the ctx invocation to run instead
    reason: str    # remediation text: what it does and why it's cheaper
    rung: str      # Ponytail ladder rung (for the ctx-debt ledger)
    shape: str     # which loop-shape matched (telemetry)


def _split(command: str) -> list[str] | None:
    try:
        toks = shlex.split(command)
    except ValueError:
        return None
    return toks or None


def _is_recursive_grep(prog: str, flags: list[str]) -> bool:
    if prog in ("rg", "ack", "ag"):
        return True  # recursive by default
    for f in flags:
        if f in ("-r", "-R", "--recursive", "--dereference-recursive"):
            return True
        # clustered short flags like -rn / -rin
        if f.startswith("-") and not f.startswith("--") and (
                "r" in f[1:] or "R" in f[1:]):
            return True
    return False


def _grep_pattern_and_globs(toks: list[str]) -> tuple[str | None, list[str]]:
    """Extract the search pattern and any path/glob positionals from a grep
    argv. Returns (pattern, path_positionals). Pattern via ``-e`` wins."""
    pattern: str | None = None
    positionals: list[str] = []
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "-e" and i + 1 < len(toks):
            pattern = toks[i + 1]
            i += 2
            continue
        if t in _GREP_VALUE_FLAGS and i + 1 < len(toks):
            i += 2  # skip the flag and its value
            continue
        if t.startswith("-"):
            i += 1
            continue
        # first bare positional is the pattern (unless -e set it); rest are paths
        if pattern is None:
            pattern = t
        else:
            positionals.append(t)
        i += 1
    return pattern, positionals


def _glob_hint(paths: list[str]) -> str:
    """A single ``--glob`` hint if the paths clearly scope a file type."""
    for p in paths:
        m = re.search(r"\*\.\w+$", p)
        if m:
            return m.group(0)
    return ""


def _collapse_grep(toks: list[str]) -> Substitution | None:
    prog = toks[0].rsplit("/", 1)[-1]
    git_grep = prog == "git" and len(toks) > 1 and toks[1] == "grep"
    if git_grep:
        toks = ["grep", *toks[2:]]  # normalise `git grep …` (recursive by default)
        prog = "grep"
    if prog not in _GREP_PROGS:
        return None
    flags = [t for t in toks[1:] if t.startswith("-")]
    if not git_grep and not _is_recursive_grep(prog, flags):
        return None  # single-file/bounded grep is handled elsewhere (the -m cap)
    pattern, paths = _grep_pattern_and_globs(toks)
    if not pattern:
        return None
    if _IDENT_RE.match(pattern):
        # a symbol hunt → the index answers it exactly, span-precise, grouped.
        return Substitution(
            command=f"ctx q {shlex.quote(f'refs {pattern} | group file')}",
            reason=("CTX_CONTEXT_GUARD: recursive search for the identifier "
                    f"`{pattern}` is a navigation loop — the grep dump is "
                    "re-ingested next turn. `ctx q 'refs … | group file'` "
                    "resolves it through the symbol index in one bounded, "
                    "addressable call (cite the file:line handles)."),
            rung="reuse-index", shape="grep_symbol")
    glob = _glob_hint(paths)
    tail = f" --glob {shlex.quote(glob)}" if glob else ""
    return Substitution(
        command=f"ctx q {shlex.quote(f'search {pattern} | files')}{tail}",
        reason=("CTX_CONTEXT_GUARD: recursive content search floods the next "
                "turn with re-ingested matches. `ctx q 'search … | files'` "
                "returns a bounded, addressable slice; page exact bytes with "
                "`ctx get`."),
        rung="bounded-search", shape="grep_content")


# flags that narrow a pytest run to a subset (the agent is already slicing).
_PYTEST_NARROW_FLAGS = {"-k", "-m", "--lf", "--last-failed", "--ff", "--failed-first"}


def _collapse_pytest(command: str, failure_available: bool) -> Substitution | None:
    # ``pytest`` with no path/-k/-m narrowing: the whole-suite (re-)run. On its
    # own this is captured at the emission gate; the collapse only helps on a
    # *re-run after a captured failure*, hence ``failure_available``.
    if not failure_available:
        return None
    toks = _split(command)
    if not toks:
        return None
    prog = toks[0].rsplit("/", 1)[-1]
    body = toks[1:]
    if prog in ("python", "python3"):
        # `python -m pytest …` — drop the `-m pytest` preamble
        if "pytest" not in body[:3]:
            return None
        body = body[body.index("pytest") + 1:]
    elif prog != "pytest":
        return None
    # any positional (a path or a `node::id`) or a narrowing flag → the agent
    # is already slicing; leave the command untouched.
    for t in body:
        if t in _PYTEST_NARROW_FLAGS or "::" in t or (not t.startswith("-")):
            return None
    return Substitution(
        command="ctx q 'fails last | in-changed'",
        reason=("CTX_CONTEXT_GUARD: a captured failure is on record — re-running "
                "the whole suite re-floods the transcript. `ctx q 'fails last | "
                "in-changed'` returns just the failing cases touching your "
                "changed files, from the last run, without re-executing."),
        rung="failure-slice", shape="pytest_rerun")


def collapse(command: str, *, failure_available: bool = False) -> Substitution | None:
    """Recognise a collapsible loop-shape in ``command`` and return the
    collapsed ``ctx`` op, or ``None`` to leave the command untouched.

    Cheapest rung first: symbol/content search (always safe, pure) before the
    store-gated pytest re-run slice.
    """
    toks = _split(command)
    if not toks:
        return None
    if _is_compound(toks, command):
        return None  # a pipe/redirect/chain changes meaning — never clobber it
    sub = _collapse_grep(toks)
    if sub:
        return sub
    return _collapse_pytest(command, failure_available)


__all__ = ["Substitution", "collapse"]
