"""Split a shell command into the simple commands it would run.

One conservative splitter for every rule of the form "a command is only as
permitted as its least permitted part": the guard's classifier and the
Claude Code permission check both ask it. It is quote-aware and splits on
every joiner the shell has: newline, ``;``, ``&&``, ``||``, ``|``, ``|&``,
``&``, a subshell or brace group, and it descends into command substitutions
(``$(...)``, backticks, ``<(...)``, ``>(...)``, also inside double quotes)
and into a here-document body that is fed to a shell. Anything it cannot
split with confidence (an unterminated quote or substitution, a ``case``
statement, nesting past a depth limit) gives None, which callers treat as
needing an ask.

Hot path: imports nothing but ``re``.
"""

from __future__ import annotations

import re

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "busybox"})
#: Words that open or continue a compound command; stripped from the front
#: of a segment so the command behind them is what gets classified.
_LEAD = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "time", "{", "}"})
#: Segments that are only a closing keyword.
_CLOSE = frozenset({"fi", "done", "esac", "}", "then", "else", "do"})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
MAX_DEPTH = 4


class _Unsplittable(Exception):
    pass


def _match_close(s: str, i: int) -> int:
    """Index of the ``)`` closing the ``(`` at ``s[i]``, quote-aware."""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise _Unsplittable("quote")
            i = j + 1
            continue
        if c == '"':
            i = _end_dquote(s, i) + 1
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j < 0:
                raise _Unsplittable("backtick")
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _Unsplittable("paren")


def _end_dquote(s: str, i: int) -> int:
    """Index of the ``"`` closing the one at ``s[i]``."""
    j = i + 1
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j
        if s.startswith("$(", j):
            j = _match_close(s, j + 1) + 1
            continue
        if c == "`":
            k = s.find("`", j + 1)
            if k < 0:
                raise _Unsplittable("backtick")
            j = k + 1
            continue
        j += 1
    raise _Unsplittable("dquote")


def _first_program(words: list[str]) -> str:
    for w in words:
        if _ASSIGN.match(w) or w in _LEAD:
            continue
        return w.rsplit("/", 1)[-1]
    return ""


class _Splitter:
    def __init__(self) -> None:
        self.segments: list[str] = []
        self.functions: set[str] = set()

    def run(self, cmd: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise _Unsplittable("depth")
        cur: list[str] = []
        heredocs: list[tuple[str, bool, bool]] = []
        word_start = True
        i, n = 0, len(cmd)

        def flush() -> None:
            text = "".join(cur).strip()
            cur.clear()
            if text:
                self.segments.append(text)

        def sub(inner: str) -> None:
            self.run(inner, depth + 1)

        while i < n:
            c = cmd[i]
            if c == "\\":
                if cmd.startswith("\\\n", i):
                    i += 2
                    continue
                cur.append(cmd[i:i + 2])
                i += 2
                word_start = False
                continue
            if c == "'":
                j = cmd.find("'", i + 1)
                if j < 0:
                    raise _Unsplittable("quote")
                cur.append(cmd[i:j + 1])
                i = j + 1
                word_start = False
                continue
            if c == '"':
                end = _end_dquote(cmd, i)
                body = cmd[i + 1:end]
                # Substitutions inside double quotes still execute: split
                # them out and leave a placeholder in the word.
                kept = ['"']
                k = 0
                while k < len(body):
                    if body[k] == "\\":
                        kept.append(body[k:k + 2])
                        k += 2
                        continue
                    if body.startswith("$((", k):
                        close = _match_close(body, k + 1)
                        kept.append(body[k:close + 1])
                        k = close + 1
                        continue
                    if body.startswith("$(", k):
                        close = _match_close(body, k + 1)
                        sub(body[k + 2:close])
                        kept.append("_")
                        k = close + 1
                        continue
                    if body[k] == "`":
                        close = body.find("`", k + 1)
                        sub(body[k + 1:close])
                        kept.append("_")
                        k = close + 1
                        continue
                    kept.append(body[k])
                    k += 1
                kept.append('"')
                cur.append("".join(kept))
                i = end + 1
                word_start = False
                continue
            if c == "#" and word_start:
                j = cmd.find("\n", i)
                i = n if j < 0 else j
                continue
            if c == "\n":
                flush()
                i += 1
                word_start = True
                for delim, strip_tabs, feeds_shell in heredocs:
                    body: list[str] = []
                    while True:
                        if i >= n:
                            raise _Unsplittable("heredoc")
                        j = cmd.find("\n", i)
                        line = cmd[i:n if j < 0 else j]
                        i = n if j < 0 else j + 1
                        if (line.lstrip("\t") if strip_tabs else line) == delim:
                            break
                        body.append(line)
                    if feeds_shell:
                        sub("\n".join(body))
                heredocs = []
                continue
            if cmd.startswith(("&&", "||", "|&", ";;"), i):
                if cmd.startswith(";;", i):
                    raise _Unsplittable("case")
                flush()
                i += 2
                word_start = True
                continue
            if c in ";|":
                flush()
                i += 1
                word_start = True
                continue
            if c == "&":
                # `&>file`, `>&2`, `2>&1` are redirections, not a background job.
                if cmd.startswith("&>", i) or (i > 0 and cmd[i - 1] in "<>"):
                    cur.append(c)
                    i += 1
                    word_start = False
                    continue
                flush()
                i += 1
                word_start = True
                continue
            if cmd.startswith("$((", i):
                close = _match_close(cmd, i + 1)
                cur.append(cmd[i:close + 1])
                i = close + 1
                word_start = False
                continue
            if cmd.startswith(("$(", "<(", ">("), i):
                close = _match_close(cmd, i + 1)
                sub(cmd[i + 2:close])
                cur.append("_")
                i = close + 1
                word_start = False
                continue
            if c == "`":
                close = cmd.find("`", i + 1)
                if close < 0:
                    raise _Unsplittable("backtick")
                sub(cmd[i + 1:close])
                cur.append("_")
                i = close + 1
                word_start = False
                continue
            if cmd.startswith("${", i):
                close = cmd.find("}", i)
                if close < 0 or "$(" in cmd[i:close] or "`" in cmd[i:close]:
                    raise _Unsplittable("parameter")
                cur.append(cmd[i:close + 1])
                i = close + 1
                word_start = False
                continue
            if cmd.startswith("()", i) or re.match(r"\(\s*\)", cmd[i:]):
                # `name()`: a function definition; its body is split below,
                # and later calls of the name run nothing else.
                words = "".join(cur).split()
                if words and _NAME.match(words[-1]):
                    self.functions.add(words[-1])
                    words = words[:-1]
                if words and words[0] == "function":
                    words = words[1:]
                cur[:] = [" ".join(words)] if words else []
                flush()
                i = cmd.index(")", i) + 1
                word_start = True
                continue
            if c == "(":
                text = "".join(cur)
                if text.endswith("="):  # array literal: arr=(a b c)
                    close = _match_close(cmd, i)
                    cur.append(cmd[i:close + 1])
                    i = close + 1
                    word_start = False
                    continue
                flush()
                i += 1
                word_start = True
                continue
            if c == ")":
                flush()
                i += 1
                word_start = True
                continue
            if c in "{}" and word_start and (i + 1 >= n or cmd[i + 1] in " \t\n;&|)"):
                flush()
                i += 1
                word_start = True
                continue
            if cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
                j = i + 2
                strip_tabs = cmd.startswith("-", j)
                if strip_tabs:
                    j += 1
                while j < n and cmd[j] in " \t":
                    j += 1
                m = re.match(r"""(['"]?)([^\s'";&|<>()]+)\1""", cmd[j:])
                if not m:
                    raise _Unsplittable("heredoc")
                words = "".join(cur).split()
                heredocs.append((m.group(2), strip_tabs, _first_program(words) in _SHELLS))
                cur.append(" ")  # the body is stdin: data, unless a shell reads it
                i = j + m.end()
                word_start = False
                continue
            cur.append(c)
            word_start = c in " \t"
            i += 1
        if heredocs:
            raise _Unsplittable("heredoc")
        flush()


def _tidy(segment: str) -> str | None:
    """Drop leading keywords; None for a segment that runs nothing."""
    words = segment.split()
    while words and words[0] in _LEAD:
        words = words[1:]
    if not words or (len(words) == 1 and words[0] in _CLOSE):
        return None
    if words[0] in ("for", "select", "function"):
        return None  # a loop header's words are data; its substitutions were split out
    if words[0] in ("case", "coproc"):
        raise _Unsplittable(words[0])
    return segment[segment.index(words[0]):].strip()


def split_segments(command: str) -> list[str] | None:
    """The simple commands ``command`` runs, in order, or None when it cannot
    be split with confidence. Calls of functions the command itself defines
    are dropped (their bodies are among the segments)."""
    sp = _Splitter()
    try:
        sp.run(command, 0)
        out = []
        for seg in sp.segments:
            seg = _tidy(seg)
            if seg is None:
                continue
            if seg.split()[0] in sp.functions:
                continue
            out.append(seg)
        return out
    except (_Unsplittable, ValueError, IndexError):
        return None
