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


@lru_cache(maxsize=512)
def _compiled(pattern: str):
    literal = pattern
    if literal[:1] in ("!", "#"):
        literal = "\\" + literal
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
