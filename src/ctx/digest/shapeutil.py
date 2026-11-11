"""Shared machinery for the intake-only shape profiles (stack trace,
delimited data, plain unified diff).

Two jobs, both learned from reviewing ``chunkmap/v1``:

* :class:`Quoter` -- every string taken from a payload (an exception message,
  a CSV cell, a file path in a diff) is redacted, then rendered as a single
  quoted line with control characters removed and a length cap, so it can neither start a line
  nor close its own quotes. The reader of a digest is a model that treats a
  line beginning ``next:`` as a suggested command; content must never be able
  to forge one, a ``[ctx ...]`` header or a span id.
* :func:`fit` -- a digest is fitted to its budget on the text as it will be
  EMITTED (control-stripped, redacted, with its redaction note), by stepping
  through progressively smaller renderings of the same facts. Every
  rendering carries the ``next:`` block, so no cut can remove the follow-up
  commands.

Nothing here touches the store, a workspace or the clock.
"""

from __future__ import annotations

import bisect
import functools
import hashlib
import unicodedata
from typing import Callable

from ctx import bounds
from ctx.digest.base import DigestContext
from ctx.digest.intakeprof import DIGEST_HEADER_RESERVE, SPAN_HEADER_RESERVE, emitted
from ctx.textutil import (
    _ANSI_RE, _CTRL_RE, EVIDENCE_LINE_CHARS, REDACTION_PATTERNS, encode_exact, strip_control,
)

# Hard ceiling on any one quoted payload string, whatever the caller asks:
# the width every evidence line in the project is held to.
# A payload line is classified on at most this many characters: patterns run
# on a bounded prefix, so no line, however long, costs more than this. FIELDS
# taken from a line (a message, a path, a cell) are NOT cut to it: they reach
# the quoter whole, because redaction must see them whole.
LINE_EXAMINE_CAP = 400
# The most of one field the quoter looks at, and the most any profile stores
# of one (one more than WINDOW, so the quoter can tell a field was cut).
WINDOW = 4096
FIELD_KEEP = WINDOW + 1
# Distance from a cut edge within which nothing is shown: longer than the
# shortest text any redaction pattern needs to recognise a token.
TAIL_GUARD = 100
# Unicode categories that can break a line or hide structure: controls,
# format characters (zero-width, bidi), line and paragraph separators.
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# The registry swaps this token for the real run id; payload text must not be
# able to name a run through it.
_PENDING = "run:PENDING"
_LINE_BREAKS = "\n\r\x0b\x0c\x85\u2028\u2029"


class Quoter:
    """The ONE door every string taken from a payload passes through on its
    way into a digest line: redacted, cleaned, redacted again, clipped,
    quoted -- in that order, whatever the caller does.

    **The guarantee.** Let ``original`` be a field exactly as the profile
    took it from the payload (whole; at most :data:`FIELD_KEEP` characters of
    it are ever stored, see ``truncated``) and ``cleaned`` be what the
    cleaning step makes of it (terminal escapes, control, format and
    zero-width characters removed, whitespace collapsed). Every substring
    that one of the project's shared redaction patterns matches in
    ``original`` or in ``cleaned`` is replaced, whole, by its marker, and no
    fragment of it appears in the digest either: nothing is clipped, stripped,
    sliced or joined before both readings have been redacted, and where the
    storage window itself cut the field the last :data:`TAIL_GUARD`
    characters are not shown, so a clip can never expose a piece of something
    redaction would have matched on the whole. Both readings are matched
    because the cleaned text is what the model reads, and the original is
    where a token preceded by a removable character still has its word
    boundary. The policy is required: a quoter without one cannot be built.

    **Not guaranteed.** A secret that is deliberately disguised so that
    neither the original nor the cleaned text matches a shared pattern is not
    detected -- here or anywhere else in the product -- because that is a
    limit of pattern-based redaction and of the shared patterns themselves.
    That covers a token split by characters the cleaner removes AND glued to
    the word before it, a token glued to a preceding word with no separator,
    credentials inside a URL, look-alike (full-width) letters, a token split
    by a combining mark, and a secret spread across two fields. No field is
    ever withheld or altered beyond what the two readings match: a field the
    patterns do not match is shown as cleaned.

    The names of the patterns that fired are kept in :attr:`fired` because
    the registry's own pass over the final body finds nothing left to redact
    and would otherwise append no ``redaction:`` note."""

    def __init__(self, limit: int, policy) -> None:
        self.limit = min(bounds.count(limit), EVIDENCE_LINE_CHARS)
        self.policy = policy
        self.clipped = 0
        self.fired: set[str] = set()

    def __call__(self, text: object, limit: int | None = None, *, truncated: bool = False,
                 tail: bool = False) -> str:
        """``text`` as one quoted line of at most ``limit`` characters.
        ``truncated`` says the caller itself kept only part of the field;
        ``tail`` keeps the END of an over-long string (a path's distinguishing
        part) instead of its start."""
        cap = self.limit if limit is None else min(bounds.count(limit), EVIDENCE_LINE_CHARS)
        out, was_clipped = _inert(str(text), cap, self.policy, self.fired, truncated, tail)
        if was_clipped:
            self.clipped += 1
        return out

    def note(self) -> list[str]:
        """The ``redaction:`` line for what this quoter redacted, or nothing."""
        return ["redaction: applied [" + ", ".join(sorted(self.fired)) + "]"] if self.fired else []


def _clean(text: str) -> str:
    """One visible line: ANSI and control characters gone, line breaks and
    runs of whitespace single spaces, format characters (zero-width, bidi)
    dropped. The reference definition; :func:`_clean_mapped` does the same
    while remembering where each kept character came from."""
    text = strip_control(text).replace(_PENDING, "run\u00b7PENDING")
    chars: list[str] = []
    for ch in text:
        if ch in " \t":
            chars.append(" ")
        elif unicodedata.category(ch) in _DROP_CATEGORIES:
            # A newline, NEL or U+2028 is a line break for some reader; make
            # it a space rather than gluing two words together.
            chars.append(" " if ch in _LINE_BREAKS else "")
        else:
            chars.append(ch)
    return " ".join("".join(chars).split())


def _clean_mapped(text: str) -> tuple[str, list[int]]:
    """``(cleaned, origin)`` where ``origin[j]`` is the index in ``text`` of
    the character that became ``cleaned[j]``. Same result as :func:`_clean`
    (without its run-id neutralising) -- a test holds the two equal."""
    out: list[str] = []
    origin: list[int] = []
    i, n = 0, len(text)
    space_at = -1
    while i < n:
        ch = text[i]
        if ch == "\x1b":
            m = _ANSI_RE.match(text, i)
            i = m.end() if m else i + 1
            continue
        if _CTRL_RE.match(ch):
            i += 1
            continue
        if ch.isspace() or ch in " \t" or (unicodedata.category(ch) in _DROP_CATEGORIES and ch in _LINE_BREAKS):
            if out and space_at < 0:
                space_at = i
            i += 1
            continue
        if unicodedata.category(ch) in _DROP_CATEGORIES:
            i += 1
            continue
        if space_at >= 0:
            out.append(" ")
            origin.append(space_at)
            space_at = -1
        out.append(ch)
        origin.append(i)
        i += 1
    return "".join(out), origin


def _marker(name: str, matched: str) -> str:
    """The same marker ``ctx.textutil.redact`` writes."""
    tag = hashlib.sha256(matched.encode("utf-8", "replace")).hexdigest()[:8]
    return f"[ctx:redacted:{name}:{tag}]"


@functools.lru_cache(maxsize=8192)
def _redacted_clean(window: str, patterns: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """``window`` cleaned, with every span the shared patterns match in the
    original OR in the cleaned text replaced by its marker (see the guarantee
    on :class:`Quoter`). Spans found in the original are carried over to the
    cleaned text through :func:`_clean_mapped`; overlapping spans merge."""
    cleaned, origin = _clean_mapped(window)
    spans: list[tuple[int, int, str]] = []
    for name in patterns:
        rx = REDACTION_PATTERNS.get(name)
        if rx is None:
            continue
        for m in rx.finditer(cleaned):
            spans.append((m.start(), m.end(), name))
        for m in rx.finditer(window):
            lo, hi = bisect.bisect_left(origin, m.start()), bisect.bisect_left(origin, m.end())
            if lo < hi:
                spans.append((lo, hi, name))
    spans.sort()
    out: list[str] = []
    names: set[str] = set()
    pos = 0
    k = 0
    while k < len(spans):
        start, end, name = spans[k]
        k += 1
        while k < len(spans) and spans[k][0] < end:
            end = max(end, spans[k][1])
            k += 1
        out.append(cleaned[pos:start])
        out.append(_marker(name, cleaned[start:end]))
        names.add(name)
        pos = end
    out.append(cleaned[pos:])
    return "".join(out).replace(_PENDING, "run\u00b7PENDING"), tuple(sorted(names))


def _safe_text(window: str, policy, fired: set[str]) -> str:
    """``window`` as one clean line, redacted per the guarantee on
    :class:`Quoter`."""
    if not getattr(policy, "enabled", True):
        return _clean(window)
    text, names = _redacted_clean(window, tuple(getattr(policy, "patterns", ())))
    fired.update(names)
    return text


def _inert(text: str, limit: int, policy, fired: set[str], truncated: bool,
           tail: bool) -> tuple[str, bool]:
    """(quoted, clipped). Linear: only the first :data:`WINDOW` characters are
    examined, so a megabyte field costs what a 4 KiB one does."""
    window = text[:WINDOW]
    cut = truncated or len(text) > WINDOW
    flat = _safe_text(window, policy, fired)           # redacted whole, THEN clipped
    if cut:
        # The end of a window cut a token in two; its tail is unredactable.
        # Nothing within the guard distance of the cut survives.
        flat = flat[: bounds.count(len(flat) - TAIL_GUARD)]
    clipped = cut or len(flat) > limit
    if len(flat) > limit:
        keep = bounds.count(limit - 1)
        flat = ("\u2026" + flat[len(flat) - keep:]) if tail else (flat[:keep] + "\u2026")
    elif cut:
        flat += "\u2026"
    flat = flat.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{flat}"', clipped


def inert(text: object, limit: int, policy) -> str:
    """One-shot :class:`Quoter` call for callers that do not track clipping."""
    return Quoter(limit, policy)(text)


_ASCII_DIGITS = frozenset("0123456789")


def ascii_digits(text: str) -> bool:
    """True for a non-empty string of ASCII digits ONLY. ``str.isdigit`` also
    accepts ``\u00b2`` and its kin, which ``int`` then refuses: a paste must
    never raise, so every number read from content is tested with this."""
    return bool(text) and text.isascii() and text.isdigit()


def digits_in(text: str) -> str:
    """The ASCII digits of ``text``, in order."""
    return "".join(ch for ch in text if ch in _ASCII_DIGITS)


def size_of(body: str, policy) -> int:
    """Bytes ``body`` will occupy once emitted, counting the run id that
    replaces ``run:PENDING`` (12 hex characters for 7)."""
    return len(encode_exact(emitted(body, policy))) + 5 * body.count("run:PENDING")


def fit(
    ctx: DigestContext,
    build: Callable[[int], str],
    levels: int,
) -> str:
    """The richest ``build(level)`` (level 0 = richest) that fits the digest
    budget measured on emitted text.

    ``build`` must return a complete body, ``next:`` block included, at every
    level, and the last level must be small enough to fit on any input; if it
    somehow is not, it is still returned, because a digest that is a little
    long but keeps its follow-ups is safer than one cut off from them (the
    caller's ``bounded`` backstop adds the handle if it ever cuts).
    """
    policy = ctx.ws.config.redaction
    room = bounds.budget_bytes(ctx.ws.config.budgets.digest_tokens) - DIGEST_HEADER_RESERVE
    body = ""
    for level in range(levels):
        body = build(level)
        if size_of(body, policy) <= room:
            return body
    return body


def plain_head(text: str, limit: int = 262144) -> str:
    """``text`` for the cheap pre-checks of ``detect``: when it holds terminal
    escapes, its first ``limit`` characters with them removed (colour codes
    hide ``Traceback`` and ``+++ `` from a substring test); otherwise the text
    itself, uncopied."""
    if "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text[:limit])


def inline_limit(ctx: DigestContext) -> int:
    """Payload size up to which ``text/v1`` shows everything verbatim. Above
    it a digest is genuinely needed; at or below it no shape view can beat
    the payload itself, so the shape profiles decline (the same rule
    ``chunkmap/v1`` applies)."""
    return ctx.ws.config.budgets.result_tokens * 3


def pct(part: int, whole: int) -> str:
    """``part`` as a whole-number percentage of ``whole`` ('<1%' for a
    nonzero share that rounds to zero). Integer arithmetic only."""
    if whole <= 0 or part <= 0:
        return "0%"
    value = (part * 100 + whole // 2) // whole
    return f"{value}%" if value > 0 else "<1%"


class Windows:
    """What one ``ctx get`` can hand back exactly, for a given payload.

    A region of lines resolves to its exact text only when it is at most
    ``max_inline_lines`` lines AND its bytes (each line carrying the
    ``L<n>: `` prefix the retrieval adds) fit ``result_tokens`` less the room
    its own header needs; beyond either the retrieval answers with a zoom
    digest (lines) or a truncation notice (bytes). A digest must not print a
    span or ``--lines`` range as if it were exact when it is not, so every
    profile asks here first. Construction splits the text once; each question
    reads at most ``max_inline_lines + 1`` lines.
    """

    def __init__(self, ctx: DigestContext, text: str, total_lines: int) -> None:
        budgets = ctx.ws.config.budgets
        self.lines = text.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.max_lines = bounds.count(budgets.max_inline_lines)
        self.max_cost = bounds.budget_bytes(budgets.result_tokens) - SPAN_HEADER_RESERVE
        self.overhead = len(f"L{total_lines}: ") + 1
        self.count = len(self.lines)

    def fit_end(self, a: int, b: int) -> int:
        """The last line ``e`` in ``[a, b]`` such that ``[a, e]`` resolves
        exactly (at least ``a`` itself: one line is the unit of retrieval)."""
        cost, e = 0, a - 1
        limit = min(b, a + self.max_lines - 1, self.count)
        while e < limit:
            c = len(self.lines[e].encode("utf-8", "replace")) + self.overhead   # lines[e] is line e+1
            if cost + c > self.max_cost and e >= a:
                break
            cost += c
            e += 1
        return max(e, a)

    def fit_start(self, a: int, b: int) -> int:
        """The first line ``s`` in ``[a, b]`` such that ``[s, b]`` resolves
        exactly (at least ``b`` itself): the window that keeps the END, for a
        paste whose decisive line comes last."""
        cost, s = 0, b + 1
        limit = max(a, b - self.max_lines + 1)
        while s > limit:
            c = len(self.lines[s - 2].encode("utf-8", "replace")) + self.overhead   # line s-1
            if cost + c > self.max_cost and s <= b:
                break
            cost += c
            s -= 1
        return min(s, b)

    def exact(self, a: int, b: int) -> bool:
        """True when ``[a, b]`` resolves to its exact lines in one call."""
        return self.fit_end(a, b) >= b

    def zoom(self, a: int, b: int) -> bool:
        """True when ``[a, b]`` is past ``max_inline_lines``: its span
        resolves to a bounded zoom digest, which says so."""
        return b - a + 1 > self.max_lines


__all__ = [
    "Windows", "plain_head", "FIELD_KEEP", "WINDOW", "ascii_digits", "digits_in",
    "LINE_EXAMINE_CAP", "Quoter", "fit", "inert", "inline_limit",
    "pct", "size_of",
]
