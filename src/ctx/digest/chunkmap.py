"""Chunk map: a deterministic, navigable table of contents for large text.

A digest of a 3 MB paste that shows ten lines and says "the rest is omitted"
is bounded but not navigable: the reader knows the address of the whole and
nothing about its parts. A chunk map cuts the text into ordered, addressable
pieces of roughly one retrieval's worth each, so any part can be fetched in
one bounded call and the table itself can be paged.

Contract (pure, deterministic, O(n) in the input):

* chunks are ordered, contiguous and cover every line of the input exactly
  once; line ranges are 1-based inclusive, counted on ``\\n`` only (the
  store's line index semantics, :func:`ctx.textutil.index_lines`);
* byte ranges are 1-based inclusive offsets into the UTF-8 encoding of the
  input -- the coordinates ``ctx get --bytes A:B`` takes;
* boundaries prefer structure: a markdown heading (outside a code fence)
  starts a chunk early (h1 from half the target, h2 from three quarters), a
  blank line ends one once it has reached the target
  size, a plain line boundary is taken only past three quarters of the hard
  cap, and the hard cap (cost or line count) always wins;
* a single line too large for one chunk -- minified JSON, base64 -- is cut
  into byte-range chunks (``split == "bytes"``) on UTF-8 character
  boundaries, preferring a separator byte near the cut;
* ``signal`` is the strongest :func:`line_rank` of any line in the chunk
  (0 = none): the shared :func:`ctx.digest.text._signal_rank`, reused, plus
  one intake-only widening for exception-class lines (see there).

Nothing here touches the store or a workspace: the profile and the paging
command derive the cost parameters from the budgets and call
:func:`chunk_map` with the same values, so the digest's rows and the paged
listing are the same map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ctx.digest.text import _signal_rank

LABEL_CHARS = 48

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# A chunk-level pre-check. A line can only rank > 0 if one of the ranking
# regexes in ``ctx.digest.text`` matches it, and every one of them needs one
# of these lowercase literals, or a colon followed by a number (file:line
# coordinates), to be present. Substring tests are an order of magnitude
# faster than the regexes over a 16 MB paste, so a chunk with none of them
# gets signal 0 without ranking its lines. tests/test_chunkmap.py holds this
# list to the ranking: any line the ranking scores above 0 must pass it.
_SIGNAL_LITERALS = (
    "error", "fail", "exception", "traceback", "fatal", "panic", "denied",
    "refused", "timeout", "timed out", "cannot", "unable", "critical",
    "permission", "err!", "elifecycle", "oomkilled", "http/", "status",
    "exit", "code", "quota", 'file "', "interrupt",
)
_COLON_NUMBER_RE = re.compile(r":\d")


def may_signal(text: str) -> bool:
    """Necessary condition for any line of ``text`` to rank above 0 under
    :func:`line_rank`."""
    low = text.lower()
    return any(k in low for k in _SIGNAL_LITERALS) or _COLON_NUMBER_RE.search(text) is not None


# Bytes a byte-range cut prefers to land just after, within the last quarter
# of the window: structure separators first, then whitespace.
_SEPARATORS = (b"\n", b"}", b"]", b",", b";", b" ", b"\t")
# The ranking regexes are not linear on whitespace-free text (an unanchored
# ``\S+\.py:\d+`` restarts at every word boundary), so a byte-range chunk is
# ranked over bounded windows of its text rather than as one line.
_RANK_WINDOW = 512

# Intake-only widening of the shared ranking. ``_signal_rank`` scores
# ``RecursionError: maximum recursion depth exceeded`` 0 (its weak keyword
# needs a word boundary before ``error``), so the decisive last line of a
# pasted traceback was invisible. The chunk map ranks a line that STARTS with
# an exception class name followed by a colon (or ending the line) as
# decisive. This lives here, not in ``ctx.digest.text``: the chunk map is only
# ever built for intake payloads and for ``ctx intake --chunks`` listings, so
# command-output digests keep the shared ranking unchanged.
_EXCEPTION_LINE_RE = re.compile(
    r"^\s*(?:[A-Za-z_][\w]*\.)*[A-Z]\w*(?:Error|Exception|Interrupt|Exit)(?::|\s*$)"
)


def line_rank(line: str) -> int:
    """:func:`ctx.digest.text._signal_rank`, plus exception-class lines at 3."""
    rank = _signal_rank(line)
    if rank >= 0 and rank < 3 and _EXCEPTION_LINE_RE.match(line):
        return 3
    return rank


@dataclass(frozen=True)
class Chunk:
    index: int        # 1-based position in the map
    start_line: int   # 1-based inclusive
    end_line: int     # 1-based inclusive
    start_byte: int   # 1-based inclusive (``ctx get --bytes`` coordinates)
    end_byte: int     # 1-based inclusive
    label: str        # heading path, else first non-empty line, clipped
    signal: int       # strongest line_rank in the chunk; 0 = none
    split: str        # "lines" (whole lines) or "bytes" (piece of one line)
    # The chunk's strongest signal line (highest rank; the LATEST among equals,
    # since conclusions -- the exception a traceback ends in -- come last) and its last
    # signal line; 0 / "" when it has none. Line numbers are absolute.
    signal_line: int = 0
    signal_text: str = ""
    last_signal_line: int = 0
    last_signal_text: str = ""
    # Where the label and signal text come from: character spans into the
    # mapped text. ``label`` / ``signal_text`` above are the RAW strings; a
    # renderer never shows them. It re-derives what it displays from these
    # spans, redacted in context (ctx.digest.intakeprof.SecretView), so a
    # secret is judged whole -- never on a clipped or piece-split fragment.
    label_spans: tuple = ()          # ((start, end), ...) heading titles or one line
    label_path: bool = False         # True: label_spans are a heading path
    signal_span: tuple = (0, 0)
    last_signal_span: tuple = (0, 0)

    @property
    def bytes(self) -> int:
        return self.end_byte - self.start_byte + 1

    @property
    def lines(self) -> int:
        return self.end_line - self.start_line + 1


#: What a redacted span shows as in a chunk-map rendering.
REDACTED_MARK = "[ctx:redacted]"


def clip_label(text: str, limit: int = LABEL_CHARS) -> str:
    """Collapse whitespace, drop control characters, clip with an ellipsis.
    Pure formatting: callers that display payload text pass text already
    redacted in context (``SecretView``), never a raw slice."""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def heading_path(titles: list[str]) -> str:
    """Heading path from component titles, deepest last; leading components
    fall off first."""
    parts = [t for t in titles if t]
    if not parts:
        return ""
    label = " > ".join(parts)
    while len(label) > LABEL_CHARS and len(parts) > 1:
        parts = parts[1:]
        label = "… > " + " > ".join(parts)
    return clip_label(label)


def _heading_label(stack) -> str:
    return heading_path([t for _, t, _ in stack])


def _rank_lines(lines: list[str], first_line: int) -> tuple[int, int, str, int, str]:
    """(best rank, its line, its text, last signal line, its text) over
    ``lines`` numbered from ``first_line``; zeros when nothing ranks. Among
    lines of equal rank the latest is the best."""
    if not may_signal("\n".join(lines)):
        return 0, 0, "", 0, ""
    best = (0, 0, "")
    last = (0, "")
    for k, line in enumerate(lines):
        if len(line) > _RANK_WINDOW:
            rank = max(line_rank(line[j:j + _RANK_WINDOW])
                       for j in range(0, len(line), _RANK_WINDOW))
        else:
            rank = line_rank(line)
        if rank > 0:
            last = (first_line + k, line.strip())
            if rank >= best[0]:
                best = (rank, first_line + k, line.strip())
    return best[0], best[1], best[2], last[0], last[1]


def _byte_cuts(data: bytes, target: int, cap: int) -> list[int]:
    """Cut points (exclusive ends, 0-based) splitting ``data`` into pieces of
    at most ``cap`` bytes, aimed at ``target``, never inside a UTF-8 sequence."""
    cuts: list[int] = []
    pos, n = 0, len(data)
    while n - pos > cap:
        end = pos + target
        floor = pos + max(1, (target * 3) // 4)
        best = -1
        for sep in _SEPARATORS:
            j = data.rfind(sep, floor, end)
            if j >= 0:
                best = j + 1
                break
        if best <= pos:
            best = end
            # Back off a UTF-8 continuation byte (10xxxxxx) so a character is
            # never split across two chunks.
            while best > pos + 1 and (data[best] & 0xC0) == 0x80:
                best -= 1
        cuts.append(best)
        pos = best
    cuts.append(n)
    return cuts


def chunk_map(
    text: str,
    *,
    target_cost: int = 2048,
    max_cost: int = 4096,
    max_lines: int = 240,
    line_overhead: int = 0,
) -> tuple[Chunk, ...]:
    """Cut ``text`` into an ordered tuple of :class:`Chunk`.

    ``cost`` of a line is its UTF-8 byte length (newline included) plus
    ``line_overhead`` -- the per-line prefix the retrieval that resolves a
    chunk will add, so a chunk's cost bounds what fetching it costs.
    ``max_cost`` and ``max_lines`` are hard caps; ``target_cost`` is where a
    structural boundary is taken.
    """
    if not text:
        return ()
    target = max(1, int(target_cost))
    cap = max(target, int(max_cost))
    line_cap = max(1, int(max_lines))
    plain_cut = target + (cap - target) // 2
    # A heading starts a new chunk early, earlier the shallower it is: an h1
    # at half the target, an h2 at three quarters, anything deeper only once
    # the chunk is as full as a blank-line boundary would need.
    heading_cut = (max(1, target // 2), max(1, (target * 3) // 4), target)

    parts = text.split("\n")
    trailing_nl = parts[-1] == ""
    if trailing_nl:
        parts.pop()
    last = len(parts) - 1
    ascii_only = text.isascii()

    chunks: list[Chunk] = []
    heads: list[tuple[int, str, tuple[int, int]]] = []   # (level, title, span) stack
    fence: str | None = None

    # The open chunk.
    c_start = 1
    c_byte = 1
    c_cost = 0
    c_bytes = 0
    c_label = ""
    c_label_spans: tuple = ()
    c_texts: list[str] = []
    c_chars: list[int] = []   # char offset of each open line in ``text``
    byte_pos = 1  # 1-based offset of the next line's first byte
    char_pos = 0  # 0-based char offset of the current line in ``text``
    prev_blank = False

    def close(end_line: int) -> None:
        nonlocal c_cost, c_bytes, c_texts, c_label, c_chars, c_label_spans
        if not c_texts:
            return
        label, spans, path = c_label, c_label_spans, bool(c_label)
        if not label:
            first = next((k for k, t in enumerate(c_texts) if t.strip()), None)
            label = clip_label(c_texts[first]) if first is not None else "(blank)"
            spans = ((c_chars[first], c_chars[first] + len(c_texts[first])),) if first is not None else ()
        rank, s_line, s_text, l_line, l_text = _rank_lines(c_texts, c_start)

        def line_span(n: int) -> tuple[int, int]:
            if not n:
                return (0, 0)
            k = n - c_start
            return (c_chars[k], c_chars[k] + len(c_texts[k]))

        chunks.append(Chunk(len(chunks) + 1, c_start, end_line, c_byte,
                            c_byte + c_bytes - 1, label, rank, "lines",
                            s_line, s_text, l_line, l_text,
                            spans, path, line_span(s_line), line_span(l_line)))
        c_cost = c_bytes = 0
        c_texts = []
        c_chars = []
        c_label = ""
        c_label_spans = ()

    for i, raw in enumerate(parts):
        lineno = i + 1
        size = (len(raw) if ascii_only else len(raw.encode("utf-8"))) + (
            1 if (i < last or trailing_nl) else 0)
        cost = size + line_overhead
        line = raw[:-1] if raw.endswith("\r") else raw

        marker = _FENCE_RE.match(line)
        heading = _HEADING_RE.match(line) if fence is None else None
        blank = not line.strip()

        if c_texts:
            cut = False
            if c_cost + cost > cap or len(c_texts) >= line_cap:
                cut = True
            elif fence is None and heading is not None and c_cost >= heading_cut[
                    min(len(heading.group(1)), 3) - 1]:
                cut = True
            elif fence is None and prev_blank and c_cost >= target:
                cut = True
            elif fence is None and c_cost >= plain_cut:
                cut = True
            if cut:
                close(lineno - 1)

        if heading is not None:
            level = len(heading.group(1))
            while heads and heads[-1][0] >= level:
                heads.pop()
            heads.append((level, clip_label(heading.group(2)),
                          (char_pos + heading.start(2), char_pos + heading.end(2))))

        if cost > cap:
            # One line too large for any chunk: byte-range pieces of it.
            close(lineno - 1)
            data = raw.encode("utf-8")
            if i < last or trailing_nl:
                data += b"\n"
            prefix = _heading_label(heads)
            head_spans = tuple(sp for _, t, sp in heads if t)
            pos = 0
            piece_char = char_pos
            for end in _byte_cuts(data, max(1, target - line_overhead),
                                  max(1, cap - line_overhead)):
                piece = data[pos:end].decode("utf-8", "replace")
                label = prefix or clip_label(piece) or "(blank)"
                span = (piece_char, piece_char + len(piece.rstrip("\n")))
                rank, _, s_text, _, l_text = _rank_lines([piece], lineno)
                chunks.append(Chunk(len(chunks) + 1, lineno, lineno, byte_pos + pos,
                                    byte_pos + end - 1, label, rank, "bytes",
                                    lineno if rank else 0, s_text[:_RANK_WINDOW],
                                    lineno if rank else 0, l_text[:_RANK_WINDOW],
                                    head_spans if prefix else (span,), bool(prefix),
                                    span if rank else (0, 0), span if rank else (0, 0)))
                piece_char += len(piece)
                pos = end
            byte_pos += size
            c_start, c_byte = lineno + 1, byte_pos
            prev_blank = False
        else:
            if not c_texts:
                c_start, c_byte = lineno, byte_pos
                c_label = _heading_label(heads)
                c_label_spans = tuple(sp for _, t, sp in heads if t)
            c_texts.append(line)
            c_chars.append(char_pos)
            c_cost += cost
            c_bytes += size
            byte_pos += size
            prev_blank = blank and fence is None

        char_pos += len(raw) + 1

        if marker is not None:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif (token[0] == fence[0] and len(token) >= len(fence)
                  and not line[marker.end():].strip()):
                fence = None

    close(len(parts))
    return tuple(chunks)


# ------------------------------------------------------------------ flavor
_LEVEL_RE = re.compile(r"\b(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|FATAL)\b")
_B64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")


def flavor(text: str, sample_bytes: int = 65536) -> str:
    """What the text looks like, from a bounded head sample. A label for the
    census line only -- it never selects a rendering."""
    head = text[:sample_bytes]
    stripped = head.lstrip()
    if not stripped:
        return "blank"
    lines = [ln for ln in head.split("\n") if ln.strip()][:200]
    if stripped[0] in "{[":
        if len(lines) > 1 and all(ln.lstrip()[:1] == "{" for ln in lines[:20]):
            return "json lines"
        return "json"
    if len(lines) <= 2 and len(stripped) >= 256 and _B64_RE.match(stripped.split("\n")[0].strip()):
        return "base64"
    fence = None
    headings = 0
    for ln in lines:
        m = _FENCE_RE.match(ln)
        if fence is None and _HEADING_RE.match(ln):
            headings += 1
        if m:
            fence = m.group(1) if fence is None else None
    if headings:
        return "markdown"
    if lines and sum(1 for ln in lines if _LEVEL_RE.search(ln)) * 5 >= len(lines) * 2:
        return "log"
    commas = [ln.count(",") for ln in lines[:50]]
    if len(commas) >= 3 and commas[0] >= 2 and all(c == commas[0] for c in commas):
        return "csv"
    if len(lines) > 1 and sum(len(ln) for ln in lines) // len(lines) > 400:
        return "long-line text"
    return "text"


__all__ = ["Chunk", "chunk_map", "clip_label", "flavor", "line_rank", "may_signal", "LABEL_CHARS"]
