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
    a table. Trivially testable, and it can never hang or flood. The two
    environment facts a couple of recognisers need (``failure_available``,
    ``symbols_resolvable``) are *supplied* by the caller, never gathered here;
    they may be passed as thunks so the caller's I/O is deferred to the point
    of use, but this module still performs none of its own.
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

# `dataclasses`/`typing` stay out of this module: hook.py imports it on the hot
# path of every intercepted command (see the latency contract in hook.py), and
# `from __future__ import annotations` makes annotations strings, so nothing
# below needs them at runtime. Not `from typing import TYPE_CHECKING`, which
# would import a module this file exists to keep off the hot path.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable

    Probe = bool | Callable[[], bool]

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


# ``failure_available`` / ``symbols_resolvable`` are answers to questions that
# cost real I/O (a session-ledger scan and a repo scan respectively), but only a
# small minority of commands reach the recogniser branch that needs them. Both
# parameters therefore accept a zero-arg callable as well as a plain bool, and
# the callable is invoked at the point of use — never on the way in. Passing a
# bool keeps the original semantics exactly.
def _probe(flag: Probe) -> bool:
    return bool(flag() if callable(flag) else flag)


# The same operators, seen as CHARACTERS. shlex.split only separates on
# whitespace, so `wc -l < f` tokenizes the operator out but `src|wc` does not
# -- the whole pipeline arrives as one token and the exact-token test misses
# it. Substitution then replaced the compound command wholesale and silently
# discarded the stage the caller wrote.
_SHELL_OP_CHARS = frozenset("|&;<>")


def _is_compound(toks: list[str], raw: str) -> bool:
    if any(t in _SHELL_OPS for t in toks):
        return True
    # Look INSIDE the tokens too. A quoted argument may legitimately contain
    # these characters (`grep 'a|b' f`), and shlex has already stripped the
    # quotes by the time we see it -- so the character scan runs over the raw
    # text outside quotes, which is the only place an operator can operate.
    if _SHELL_OP_CHARS & set(_unquoted(raw)):
        return True
    return "$(" in raw or "`" in raw  # command substitution


def _unquoted(raw: str) -> str:
    """``raw`` with quoted spans removed, so a shell operator INSIDE a quoted
    argument is not mistaken for one the shell would act on."""
    out: list[str] = []
    quote = ""
    escaped = False
    for ch in raw:
        if escaped:
            escaped = False
            continue
        # A backslash is LITERAL inside single quotes -- sh has no escapes
        # there at all. Treating it as one desynchronized the quote tracking
        # on `grep 'a\' | wc -l`, which then read as a bare invocation and
        # got its pipeline stage silently substituted away.
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            continue
        out.append(ch)
    return "".join(out)


class Substitution:
    """A collapsed op that replaces a loop-shape command.

    Hand-rolled rather than ``@dataclass(frozen=True)``: this module is
    imported on the hot path of every intercepted command, and the decorator's
    only cost that matters is the ``dataclasses`` import, which drags in
    ``inspect`` → ``ast``/``dis``/``tokenize``/``linecache`` — measured at
    ~9.8 ms per tool call, the single largest item in the guard's import
    graph. Four read-only string fields do not earn that.

    Semantics are kept identical to the frozen dataclass — positional and
    keyword construction, value equality against its own type only,
    hashability, immutability (including no new attributes, via ``__slots__``),
    and the same ``repr`` shape — so the type stays substitutable and only the
    import cost is gone. `tests/test_hook_hot_path.py` pins that equivalence.
    """

    __slots__ = ("command", "reason", "rung", "shape")

    def __init__(self, command: str, reason: str, rung: str, shape: str) -> None:
        s = object.__setattr__
        s(self, "command", command)  # the ctx invocation to run instead
        s(self, "reason", reason)    # remediation: what it does, why it's cheaper
        s(self, "rung", rung)        # Ponytail ladder rung (for the ctx-debt ledger)
        s(self, "shape", shape)      # which loop-shape matched (telemetry)

    def _fields(self) -> tuple[str, str, str, str]:
        return (self.command, self.reason, self.rung, self.shape)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"cannot delete field {name!r}")

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not Substitution:
            return NotImplemented
        return self._fields() == other._fields()  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash(self._fields())

    def __repr__(self) -> str:
        return (f"Substitution(command={self.command!r}, reason={self.reason!r}, "
                f"rung={self.rung!r}, shape={self.shape!r})")


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


def _shell_safe(query: str) -> bool:
    """Can this query survive being quoted ONCE as a single shell argument?

    Checked on the assembled string rather than on each part, so a new
    interpolated field inherits the guard instead of needing its own.
    """
    return "'" not in query and '"' not in query


def _scope_hint(paths: list[str]) -> str | None:
    """A ``--glob`` that preserves the SCOPE the caller wrote.

    This only ever looked for a trailing ``*.ext``, so `grep -rn X tests/`
    and `grep -rn X` collapsed to the identical whole-repo command: a
    substitution that WIDENED what was asked. The replacement surface is
    allowed to make a search cheaper; it is not allowed to make it bigger,
    because the extra results are indistinguishable from real ones.

    A directory becomes ``dir/**``, a single file becomes itself, and a
    literal ``*.ext`` keeps working. Several paths that cannot be expressed
    as one glob yield "" -- and the caller declines to substitute rather
    than silently dropping the scope.
    """
    real = [p for p in paths if p not in (".", "./")]
    if not real:
        return ""
    if len(real) > 1:
        return None  # not expressible as one --glob; the caller must not widen
    p = real[0].rstrip("/")
    if re.search(r"\*\.\w+$", p):
        return re.search(r"\*\.\w+$", p).group(0)
    if any(ch in p for ch in "*?["):
        return p  # already a glob the caller wrote
    return p if re.search(r"\.\w+$", p) else f"{p}/**"


#: grep flags that change what a match MEANS, rather than how much is
#: printed. `ctx q search` has no equivalent for these, so a command
#: carrying one cannot be collapsed -- substituting anyway answers a
#: different question. `-v` is the sharp one: it inverts the match, so the
#: collapse returned the files that DO contain the pattern when the caller
#: asked for the ones that do not.
_SEMANTIC_GREP_FLAGS = frozenset({
    "-v", "--invert-match", "-L", "--files-without-match",
    "-c", "--count", "-x", "--line-regexp",
})
#: NOT in that set: `-w`. Word-boundary matching is exactly what
#: `ctx q refs` does, so `grep -rnw <symbol>` is the one semantic flag the
#: substitution PRESERVES rather than contradicts.


def _collapse_grep(toks: list[str], symbols_resolvable: Probe) -> Substitution | None:
    prog = toks[0].rsplit("/", 1)[-1]
    for t in toks[1:]:
        if t in _SEMANTIC_GREP_FLAGS:
            return None
        # Bundled short flags (`-rvn`): a semantic flag hides inside them.
        if re.fullmatch(r"-[A-Za-z]{2,}", t) and any(
            c in t[1:] for c in ("v", "L", "c", "x")
        ):
            return None
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
    # `_probe` second: the repo scan behind `symbols_resolvable` is only worth
    # paying once we know this really is a bare-identifier hunt.
    if _IDENT_RE.match(pattern) and _probe(symbols_resolvable):
        # a symbol hunt on a repo where refs can resolve → the index answers it
        # exactly, span-precise, grouped. When symbols are NOT resolvable (no
        # index, unsupported language) we fall through to bounded content search
        # below — never to nothing, so the agent is never stranded.
        return Substitution(
            command=f"ctx q {shlex.quote(f'refs {pattern} | group file')}",
            reason=("CTX_CONTEXT_GUARD: recursive search for the identifier "
                    f"`{pattern}` is a navigation loop — the grep dump is "
                    "re-ingested next turn. `ctx q 'refs … | group file'` "
                    "resolves it through the symbol index in one bounded, "
                    "addressable call (cite the file:line handles)."),
            rung="reuse-index", shape="grep_symbol")
    scope = _scope_hint(paths)
    if scope is None:
        # The caller scoped this search somewhere we cannot express as one
        # --glob. Substituting anyway would run it over the whole repo, so
        # this shape is left alone (still bounded at the emission gate).
        return None
    # INSIDE the query string. `ctx q`'s own parser takes only [--trace] and
    # the query, so a top-level `--glob` was an unrecognized argument and the
    # substituted command exited 2 -- the collapse producing something that
    # cannot run. Latent before (only a trailing `*.ext` ever set it) and
    # made common by the scope-preserving fix, which sets it for every
    # directory-scoped grep. `search` is the stage that reads --glob.
    # No inner shlex.quote: the whole query is quoted once as a single
    # argument below, and quoting twice produced a valid but unreadable
    # '"'"' nest. Nothing embedded in it may carry a quote character.
    #
    # The first cut guarded only the SCOPE. The PATTERN goes into the same
    # string and needed the same guard -- `grep -rn "TODO's" src` collapsed
    # to a query with an unbalanced quote, which is the identical
    # "collapsed command does not parse" defect one commit later, through
    # the other half of the same expression.
    scoped_query = (
        f"search {pattern} --glob {scope} | files" if scope
        else f"search {pattern} | files"
    )
    if not _shell_safe(scoped_query):
        return None
    return Substitution(
        command=f"ctx q {shlex.quote(scoped_query)}",
        reason=("CTX_CONTEXT_GUARD: recursive content search floods the next "
                "turn with re-ingested matches. `ctx q 'search … | files'` "
                "returns a bounded, addressable slice; page exact bytes with "
                "`ctx get`."),
        rung="bounded-search", shape="grep_content")


# source extensions the skeleton tier can outline (kept in sync with
# skeleton.py's supported languages) — a whole-file `cat` of one of these is a
# read-the-whole-file flood the skeleton-first outline collapses.
_SKELETON_EXTS = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
                  ".go", ".rs")


def _collapse_cat(toks: list[str]) -> Substitution | None:
    """Whole-file ``cat <source-file>`` → the priced symbol outline. Maki's
    skeleton-first read, delivered under the agent's own command: it gets the
    structure (signatures + line ranges) plus handles to page exact bytes,
    instead of the entire file re-ingested next turn."""
    prog = toks[0].rsplit("/", 1)[-1]
    if prog != "cat":
        return None
    args = [t for t in toks[1:] if not t.startswith("-")]
    if len(args) != 1:  # `cat a b` concatenates — not a single-file read
        return None
    f = args[0]
    if not f.endswith(_SKELETON_EXTS):
        return None
    return Substitution(
        command=f"ctx stats {shlex.quote('repo:' + f)}",
        reason=("CTX_CONTEXT_GUARD: cat of a whole source file re-floods the "
                "turn. `ctx stats repo:<file>` gives the priced symbol outline "
                "(signatures + line ranges) with handles to page exact bytes — "
                "`ctx get repo:<file> --symbol <Name>` or `--lines A:B`."),
        rung="skeleton-first", shape="cat_skeleton")


# flags that narrow a pytest run to a subset (the agent is already slicing).
_PYTEST_NARROW_FLAGS = {"-k", "-m", "--lf", "--last-failed", "--ff", "--failed-first"}


def _collapse_pytest(command: str, failure_available: Probe) -> Substitution | None:
    # ``pytest`` with no path/-k/-m narrowing: the whole-suite (re-)run. On its
    # own this is captured at the emission gate; the collapse only helps on a
    # *re-run after a captured failure*, hence ``failure_available``.
    #
    # The syntactic tests come FIRST and the `failure_available` probe last.
    # The two are a plain conjunction, so the outcome is unchanged — but the
    # probe is a scan of the session intervention ledger, and this way it runs
    # only for a command that is genuinely a bare whole-suite pytest run,
    # instead of on every command the agent issues.
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
    if not _probe(failure_available):
        return None
    return Substitution(
        command="ctx q 'fails last | in-changed'",
        reason=("CTX_CONTEXT_GUARD: a captured failure is on record — re-running "
                "the whole suite re-floods the transcript. `ctx q 'fails last | "
                "in-changed'` returns just the failing cases touching your "
                "changed files, from the last run, without re-executing."),
        rung="failure-slice", shape="pytest_rerun")


def collapse(command: str, *, failure_available: Probe = False,
             symbols_resolvable: Probe = True) -> Substitution | None:
    """Recognise a collapsible loop-shape in ``command`` and return the
    collapsed ``ctx`` op, or ``None`` to leave the command untouched.

    Cheapest rung first: symbol/content search (always safe, pure) before the
    store-gated pytest re-run slice. ``symbols_resolvable`` degrades a symbol
    hunt to bounded content search when the repo has no way to resolve refs,
    so the collapse is safe on any repo and never strands the agent.

    ``failure_available`` and ``symbols_resolvable`` each accept either a bool
    or a zero-arg callable returning one. The callable form is what the guard
    passes: both answers cost I/O, and only a small minority of commands reach
    a branch that consults them, so the caller hands over the *question* and
    the recogniser asks it only if the answer can change the outcome. Results
    are identical either way; this module stays pure and total.
    """
    toks = _split(command)
    if not toks:
        return None
    if _is_compound(toks, command):
        return None  # a pipe/redirect/chain changes meaning — never clobber it
    sub = _collapse_grep(toks, symbols_resolvable)
    if sub:
        return sub
    sub = _collapse_cat(toks)
    if sub:
        return sub
    return _collapse_pytest(command, failure_available)


__all__ = ["Substitution", "collapse"]
