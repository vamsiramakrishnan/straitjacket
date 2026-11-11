"""One glob dialect for the whole tool.

``ctx`` matched path globs two different ways depending on which door you
came in by, and the two disagreed on ordinary patterns:

* ``workspace.is_ignored`` and ``rg --glob`` use the **gitwildmatch**
  dialect, where a ``*`` segment stops at a ``/`` unless the pattern says
  ``**``.
* ``search --glob`` and ``corpus --glob/--exclude`` used raw ``fnmatch``,
  where ``*`` crosses ``/`` freely.

So ``--glob 'src/*.py'`` reached ``src/sub/nested.py`` on the retrieval side
and did not on the ignore side. Same pattern, same path, two answers -- a
silent scope leak in ``search`` and a silent over-exclusion in ``corpus``,
from one root. It was tracked as debt (``7884ed9a7d``) rather than fixed;
a bug bash then proved it end to end through the CLI, which is what a
dialect split eventually does.

This module is the single answer. ``pathspec`` is a hard dependency and
supplies the dialect; the stdlib fallback exists only so a broken install
degrades rather than dies, and it implements the same boundary rule, since
that rule is the whole point.

``!`` and ``#`` lead a gitignore *line*, not a path glob. A selector is a
single pattern with nowhere to negate to, so a leading one is escaped to
its literal self rather than silently inverting the caller's request.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

__all__ = ["matches"]


def matches(rel: str, pattern: str) -> bool:
    """True when repo-relative POSIX path ``rel`` matches ``pattern``.

    gitwildmatch semantics: ``*`` and ``?`` do not cross ``/``; ``**``
    does; a pattern containing no ``/`` matches by basename at any depth.
    """
    rel = rel.removeprefix("./")
    pattern = pattern.removeprefix("./")  # was stripped from rel but not pattern, so "./src/*.py" never matched
    if not pattern:
        return False
    return _compiled(pattern)(rel)


def _normalize(rel: str) -> str:
    """pathspec.util.normalize_file on POSIX: drop one leading "/" or "./"."""
    if os.sep != "/":
        rel = rel.replace(os.sep, "/")
    if os.altsep and os.altsep != "/":
        rel = rel.replace(os.altsep, "/")
    if rel.startswith("/"):
        return rel[1:]
    if rel.startswith("./"):
        return rel[2:]
    return rel


@lru_cache(maxsize=512)
def _compiled(pattern: str):
    literal = pattern
    if literal[:1] in ("!", "#"):
        literal = "\\" + literal
    try:
        # The bounded engine below: same semantics as pathspec's
        # "gitignore" dialect, without regex backtracking.
        program = _program(literal)
        return lambda rel: _run(program, _normalize(rel))
    except _Untranslatable:
        pass  # a pattern pathspec would reject: keep its old handling
    try:
        import pathspec

        try:
            spec = pathspec.PathSpec.from_lines("gitignore", [literal])
        except KeyError:  # older pathspec releases
            spec = pathspec.PathSpec.from_lines("gitwildmatch", [literal])
        return lambda rel: bool(spec.match_file(rel))
    except Exception:
        rx = re.compile(_translate(pattern))
        return lambda rel: rx.match(rel) is not None


def _translate(pattern: str) -> str:
    """gitwildmatch -> anchored regex. The fallback engine.

    The one rule that must survive: an unqualified wildcard is bounded by
    the next ``/``. Everything else here is convenience.
    """
    pat = pattern.strip("/")
    # No separator anywhere: gitignore matches such a pattern by basename at
    # any depth, so `*.py` still reaches `src/a.py`.
    if "/" not in pat:
        pat = "**/" + pat
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            close = pat.find("]", i + 1)
            if close == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1 : close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = close + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    # A directory pattern matches everything beneath it, as gitignore does.
    return "".join(out) + r"(?:/.*)?\Z"


# ------------------------------------------------------------ the engine
# pathspec turns a pattern into a Python regex and runs it with `re.match`,
# which backtracks: `*a*a*...*b0` against a long run of `a` is exponential
# (a review measured 10.5 s for a 30-character name, >120 s for a 1,000-glob
# file). pathspec applies it with `re.search`; every program below begins
# with "^" so that is an anchored match. This engine runs the SAME semantics -- pathspec 1.x's
# GitIgnoreBasicPattern segment rules, transcribed below -- as a left-to-right
# sweep over the path that keeps the SET of reachable positions per token, so
# a match costs O(tokens x path length) whatever the pattern. Character
# classes are still compiled by `re`, but each one only ever tests a single
# character, which cannot backtrack. tests/test_pathglob_engine.py runs it
# differentially against pathspec itself on thousands of generated pairs.

_START_DIRS, _MID_DIRS, _STAR, _PLUS, _ANY, _LIT, _CLS = range(7)
_END_SEG, _END_STAR, _TAIL_SLASH = "end_seg", "end_star", "tail_slash"


class _Untranslatable(Exception):
    """pathspec would reject this pattern; the caller keeps the old path."""


def _segment_tokens(seg: str) -> list:
    """One non-``**`` segment glob, as pathspec's _translate_segment_glob reads it."""
    out: list = []
    escape = False
    i, end = 0, len(seg)
    while i < end:
        c = seg[i]
        i += 1
        if escape:
            escape = False
            out.append((_LIT, c))
        elif c == "\\":
            escape = True
        elif c == "*":
            out.append((_STAR, None))
        elif c == "?":
            out.append((_ANY, None))
        elif c == "[":
            j = i
            if j < end and seg[j] in "!^":
                j += 1
            if j < end and seg[j] == "]":
                j += 1
            while j < end and seg[j] != "]":
                j += 1
            if j < end:
                j += 1
                expr = "["
                if seg[i] in "!^":
                    expr += "^"
                    i += 1
                expr += seg[i:j].replace("\\", "\\\\")
                i = j
                try:
                    import warnings

                    with warnings.catch_warnings():  # "[[...]" is a nested-set FutureWarning
                        warnings.simplefilter("ignore", FutureWarning)
                        rx = re.compile(expr)
                except re.error:
                    raise _Untranslatable from None
                out.append((_CLS, rx))
            else:
                out.append((_LIT, "["))
        else:
            out.append((_LIT, c))
    if escape:
        raise _Untranslatable
    return out


def _program(pattern: str):
    """Token program for one pattern, or None for a pattern that matches nothing.
    Mirrors GitIgnoreBasicPattern.pattern_to_regex (pathspec 1.1)."""
    if not pattern.endswith("\\ "):
        pattern = pattern.rstrip()
    if not pattern or pattern.startswith("#"):
        return None
    if pattern.startswith("!"):
        # A negation line never makes a single selector match (include=False).
        return None
    segs = pattern.split("/")
    is_dir = not segs[-1]
    if pattern == "/":
        is_dir = False
    if not segs[0]:
        del segs[0]
    elif len(segs) == 1 or (len(segs) == 2 and not segs[1]):
        if segs[0] != "**":
            segs.insert(0, "**")
    if not segs:
        raise _Untranslatable
    if not segs[-1]:
        segs[-1] = "**"
    for i in range(len(segs) - 1, 0, -1):
        if segs[i - 1] == "**" and segs[i] == "**":
            del segs[i]
    n = len(segs)
    if n == 1 and segs[0] == "**":
        return ("override", "/" if is_dir else ".")
    if n == 2 and segs == ["**", "*"]:
        return ("override", ".")
    if n == 3 and segs == ["**", "*", "**"]:
        return ("override", "/")
    toks: list = []
    need_slash = False
    end = n - 1
    for i, seg in enumerate(segs):
        if seg == "**":
            if i == 0:
                toks.append((_START_DIRS, None))
            elif i < end:
                toks.append((_MID_DIRS, None))
                need_slash = True
            else:
                toks.append((_LIT, "/"))
                toks.append((_TAIL_SLASH, None))
        else:
            if need_slash:
                toks.append((_LIT, "/"))
            if seg == "*":
                toks.append((_PLUS, None))
            else:
                toks.extend(_segment_tokens(seg))
            if i == end:
                toks.append((_END_STAR if seg == "*" else _END_SEG, None))
            need_slash = True
    return ("tokens", toks)


class _Path:
    """Per-path bitmasks (bit p = position p, 0..n) shared by every token."""

    __slots__ = ("s", "n", "full", "char", "nonslash", "slash", "segs", "nl")

    def __init__(self, s: str):
        self.s, self.n = s, len(s)
        self.full = (1 << (self.n + 1)) - 1
        self.char: dict[str, int] = {}
        for i, c in enumerate(s):
            self.char[c] = self.char.get(c, 0) | (1 << i)
        self.slash = self.char.get("/", 0)
        self.nonslash = ((1 << self.n) - 1) & ~self.slash
        self.nl = "\n" in s
        # Slash-free stretches as position masks [a, b]: b is the slash (or n).
        self.segs = []
        a = 0
        for i, c in enumerate(s):
            if c == "/":
                self.segs.append(((1 << (i - a + 1)) - 1) << a)
                a = i + 1
        self.segs.append(((1 << (self.n - a + 1)) - 1) << a)

    def cls(self, rx) -> int:
        m = 0
        for i, c in enumerate(self.s):
            if rx.fullmatch(c):
                m |= 1 << i
        return m


def _run(program, s: str) -> bool:
    """Position-SET simulation of the program over ``s``: every token maps
    the set of reachable positions to the next in a handful of big-integer
    operations (per slash-separated stretch at most), so a match costs
    O(tokens x stretches), never exponential."""
    if program is None:
        return False
    kind, body = program
    if kind == "override":
        # pathspec runs regex "." / "/" with re.search: any non-newline char /
        # a "/" anywhere.
        return any(c != "\n" for c in s) if body == "." else "/" in s
    P = _Path(s)
    n = P.n
    cur = 1
    for op, arg in body:
        if op == _END_SEG:      # (?:/|$) then nothing: a prefix match succeeds.
            # `$` (no MULTILINE) also matches just before a final newline.
            return bool(cur >> n & 1 or (cur & P.slash)
                        or (n and s[-1] == "\n" and cur >> (n - 1) & 1))
        if op == _END_STAR:     # /?$
            if cur >> n & 1:
                return True
            if n and s[-1] in "/\n" and cur >> (n - 1) & 1:
                return True
            return bool(n > 1 and s[-1] == "\n" and s[-2] == "/" and cur >> (n - 2) & 1)
        if op == _TAIL_SLASH:   # the regex ends after a "/": matched already
            return bool(cur)
        if op == _LIT:
            new = (cur & P.char.get(arg, 0)) << 1
        elif op == _ANY:
            new = (cur & P.nonslash) << 1
        elif op == _CLS:
            new = (cur & P.cls(arg)) << 1
        elif op in (_STAR, _PLUS):  # [^/]* / [^/]+ : fill each stretch upward
            new = 0
            for seg in P.segs:
                x = cur & seg
                if x:
                    low = x & -x
                    new |= seg & ~((low << (1 if op == _PLUS else 0)) - 1)
        elif op == _START_DIRS:  # ^(?:.+/)? : a newline-free run of >= 1 char, then "/"
            new = cur & 1
            if cur & 1:
                limit = s.find("\n")
                for i, c in enumerate(s):
                    if limit != -1 and i - 1 >= limit:
                        break
                    if c == "/" and i >= 1:
                        new |= 1 << (i + 1)
        elif op == _MID_DIRS:    # (?:/.+)? : optionally "/" then a newline-free run
            new = cur
            starts = cur & P.slash
            if starts and not P.nl:
                low = starts & -starts
                new |= P.full & ~((low << 2) - 1)
            elif starts:
                for i in range(n):
                    if starts >> i & 1:
                        j = i + 1
                        while j < n and s[j] != "\n":
                            j += 1
                            new |= 1 << j
        else:  # pragma: no cover - every op is handled above
            raise AssertionError(op)
        cur = new & P.full
        if not cur:
            return False
    return bool(cur)  # no end token: re.match succeeds wherever it stops
