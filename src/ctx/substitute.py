"""Collapse substitution -- the replacement-surface layer, now retired.

This module used to replace an agent's own shell command with a "collapsed"
ctx op: a recursive grep with `ctx q refs`/`search`, `cat` with an outline,
`head`/`sed` with `ctx get`, `find`/`ls -R` with a corpus listing, and a
whole-suite pytest re-run with the last run's failures. Each was supposed to
answer the same question more cheaply; audited against that rule, none did --
they answered nearby questions with the same confidence and declared nothing
missing (see the per-shape notes below). A substitution that changes the
answer is worse than none, so ``collapse`` now returns None for every command
and the guard runs the user's own command, captured and bounded where its
output is unbounded: the same answer by construction.

What remains: the ``Substitution`` type and ``collapse`` entry point (so a
substitution proven answer-preserving by a differential test can return), the
compound-command detection, and :func:`symbol_hint`, which offers ``ctx q
refs`` BESIDE an identifier search as the different question it is.

Hot-path module: hook.py imports it per call, so it stays stdlib-light (re,
shlex) and pure.
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

# a bare identifier: the only search pattern for which a `refs` HINT is offered.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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


#: Characters that mean something to a consumer of the assembled query --
#: the SHELL (quotes) or `ctx q`'s own pipeline parser (`|`). A user's
#: argument is data; every one of these turns part of it into syntax.
_QUERY_UNSAFE = ("'", '"', "|")


def _shell_safe(*fields: str) -> bool:
    """Can these untrusted fields ride inside a ctx command safely?"""
    return not any(ch in field for field in fields for ch in _QUERY_UNSAFE)


# ------------------------------------------------------------ search: retired
# grep/rg/ack/ag/git-grep are NOT substituted, in any shape. They used to be:
# an identifier-looking pattern became `ctx q 'refs X | group file'` and any
# other pattern `ctx q 'search X | files'`. Both answered a different question
# with the same confidence. `refs` is a code-symbol lookup: it never reads
# prose, config or most comments, so `grep -rn NEEDLE .` over a repo with the
# token in notes.md, config.yaml and app.py reported one match of three and
# declared nothing missing; path filters (`--include`, `-g`, `-t`) were
# dropped, so a `--include=*.md` search returned the .py file and missed the
# .md one. `search` differs from the original in regex dialect, case and
# ignore-file handling, and `| files` drops the lines that were asked for.
#
# The rule every search rewrite must meet: the result holds every match the
# original would have produced, restricted to exactly the files it would
# have searched, or says precisely what it left out and how to get it. No
# ctx query is equivalent to grep/rg across their flag surface, so the guard
# instead runs the user's OWN search whole under `ctx run --passthrough`
# (bounded capture of the real command: the same answer by construction,
# digested with an address when large). tests/test_search_substitution_
# differential.py runs a matrix of real searches against what the hook runs
# and fails on any lost or out-of-scope match.
#
# A symbol lookup survives only as a HINT beside that capture, labelled as
# the different question it is (:func:`symbol_hint`).

_GREP_PROGS = {"grep", "egrep", "fgrep", "rg", "ack", "ag"}
_GREP_VALUE_FLAGS = {
    "-e", "-f", "-m", "--max-count", "-A", "-B", "-C", "--glob", "-g", "-t", "-T",
    "--type", "--type-not", "--after-context", "--before-context", "--context",
    "--include", "--exclude", "--exclude-dir",
}


def symbol_hint(command: str) -> str | None:
    """An optional pointer to `ctx q refs`, for a search whose pattern is a
    bare identifier. Never a replacement: it says what it is and what it does
    not read. None for anything else."""
    toks = _split(command)
    if not toks or _is_compound(toks, command):
        return None
    prog = toks[0].rsplit("/", 1)[-1]
    if prog == "git" and len(toks) > 1 and toks[1] == "grep":
        toks = ["grep", *toks[2:]]
        prog = "grep"
    if prog not in _GREP_PROGS:
        return None
    pattern = None
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "-e" and i + 1 < len(toks):
            pattern = toks[i + 1]
            break
        if t in _GREP_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        pattern = t
        break
    if not pattern or not _IDENT_RE.match(pattern):
        return None
    return (f"Different question, if it is yours: where the code symbol `{pattern}` "
            f"is defined or referenced -- `ctx q 'refs {pattern} | group file'`. "
            "That reads the symbol index, not prose, config or comments, so it is "
            "not a substitute for this search.")


# ----------------------------------------------- other shapes: also retired
# Audited against the same rule (the substituted command must give the
# original's answer, or declare precisely what it left out). None did, so none
# is substituted; each command runs as itself, captured by the guard where
# its output is unbounded:
#
# * `cat <source>` -> `ctx stats repo:<f>`: an outline in place of the file's
#   contents. A different answer.
# * `wc -l <source>` -> `ctx stats`: right count, but `repo:` paths resolve
#   from the workspace root, not the shell's cwd, so from a subdirectory it
#   counted a different file.
# * `head -n N <f>` / `sed -n 'A,Bp' <f>` -> `ctx get repo:<f> --lines`: the
#   same cwd defect (verified: from `sub/`, `head f.txt` read `sub/f.txt`,
#   the substitute read `f.txt` at the root), and a range past EOF became an
#   error instead of the empty or short output the tools give. A bounded
#   `head` is allowed natively and `sed` is captured whole anyway.
# * `find <dir> -name <glob>` -> `ctx q 'corpus --glob'`: corpus honours
#   ignore files and `find` does not (verified: `build/b.txt` under a
#   gitignored `build/` vanished), so the file set differed.
# * `ls -R` / `tree` -> `ctx q corpus`: files only, ignore-aware; a listing
#   shows directories and ignored entries. A different answer.
# * whole-suite `pytest` after a captured failure -> `ctx q 'fails last |
#   in-changed'`: reports the LAST run's failures without re-running, so after
#   an edit it answers about code that no longer exists.


def collapse(command: str, *, failure_available: Probe = False,
             symbols_resolvable: Probe = True) -> Substitution | None:
    """The collapsed ``ctx`` op for ``command``, or ``None`` to leave it.

    Currently always ``None``: every substitution this module made was
    retired because it changed the answer (see the notes above). Commands run
    as themselves, under the guard's bounded capture where their output is
    unbounded. The probe parameters are kept for callers and never consulted.
    """
    # Every recogniser has been retired (see the notes above): no ctx op was
    # shown to give the same answer as the command it replaced. The function
    # and the Substitution type stay so a substitution that IS proven
    # answer-preserving -- by a differential test in the shape of
    # tests/test_search_substitution_differential.py -- can return here.
    del failure_available, symbols_resolvable
    return None


__all__ = ["Substitution", "collapse", "symbol_hint"]
