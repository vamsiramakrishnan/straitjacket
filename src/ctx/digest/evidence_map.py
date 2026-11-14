"""Evidence map: exact accounting of the error-like lines a digest did not show.

A digest that shows a few lines of a 9,000-line log asks the reader to trust
whatever chose them. The map removes that trust: every *signal line* (a line
:func:`ctx.digest.text._signal_rank` rates tier >= 1 -- error words, failure
counts, exit codes, ``file:line`` coordinates) is grouped into a DISTINCT
MESSAGE by template, and the digest states, exactly, how many signal lines
and distinct messages there are, which messages it shows with context, which
it only lists, and how many it does not list -- with the command that lists
those. When nothing is left unlisted it says so in plain words, which is what
lets a reader stop searching.

Template (:func:`message_key`): quoted values that carry a digit, ``/`` or
``@`` become ``'<*>'``; ``0x`` hex ids become ``<*>``; a path keeps its leading ``./``/``/`` and first
segment and loses its tail (``src/<*>``, ``pod/<*>``); then the log-template
token mask (:func:`ctx.digest.logprof.mask_line`: timestamps, numbers, hex,
uuids, durations, IPs). The key groups lines; it is never shown -- each
message shows one verbatim sample line (its first occurrence).

Ranking (:func:`local_order`), all keys ascending:

1. more focus terms matched (``--focus``) first;
2. higher tier first;
3. rarer first: a message seen once outranks one seen 680 times;
4. later LAST occurrence first -- nearer the end of the run, where the
   failure that stopped it usually is;
5. earlier first occurrence; stderr before stdout; the key text.

The top ``EXPAND_K`` messages are expanded as verbatim excerpts with
``EXCERPT_CONTEXT`` lines either side, every line prefixed by its real line
number. Overlapping or adjacent windows share one excerpt. A message counts
as *shown* when it was expanded, or when it occurs once and that line is
inside an excerpt; every other message is *listed* (count, first and last
line, one sample) up to ``LIST_ROWS`` rows and the byte cap, and the rest are
*not listed*, with the exact command that lists every message. The three
numbers always add up to the number of distinct messages.

Deterministic: the same bytes and config render the same map. Every shown
line is redacted (whole secrets replaced by their marker) BEFORE it is cut
or measured, so the byte cap holds after redaction and a secret in a sample
never reaches the reader.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any

from ctx.digest.logprof import mask_line
from ctx.digest.text import _signal_rank
from ctx.textutil import EVIDENCE_LINE_CHARS, clip_redacting, fmt_int

EXPAND_K = 3
EXCERPT_CONTEXT = 2
LIST_ROWS = 8
EXCERPT_LINE_CHARS = EVIDENCE_LINE_CHARS
LIST_SAMPLE_CHARS = 110
#: A secret is judged on at most this many leading characters of a line
#: (constant cost per shown line, like ``redact_before_transform``).
_REDACT_SCAN_CHARS = 4096
MAP_VERSION = "evmap/v1"

# ---------------------------------------------------------------- signal scan
# Every alternative of every regex in _signal_rank needs one of these
# (case-insensitively) or a coordinate shape. Used only as a SUPERSET filter
# over ASCII text: each candidate is then ranked by _signal_rank itself, so the
# result is exactly the per-line scan's, at a fraction of the cost (one C-level
# find per keyword over the whole text instead of nine regexes per line).
_KEYWORDS = (
    "error", "fail", "exception", "traceback", "fatal", "panic", "denied",
    "refused", "timeout", "timed out", "cannot", "unable", "critical",
    "permission", "err!", "http/", "status", "exit", "code", "quota",
    "oomkilled", "elifecycle",
)
_COORD_PREFILTER = re.compile(r'\.\w+:\d|file "|--> ')


def _line_starts(text: str) -> list[int]:
    starts = [0]
    find = text.find
    pos = find("\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = find("\n", pos + 1)
    return starts


def signal_rows(text: str, lines: list[str]) -> list[tuple[int, int]]:
    """``(line_number, tier)`` of every line with ``_signal_rank >= 1``, in
    line order. ``lines`` is ``index_lines(text)``."""
    if not lines:
        return []
    if text.isascii():
        starts = _line_starts(text)
        low = text.lower()
        cand: set[int] = set()
        n = len(lines)
        for kw in _KEYWORDS:
            i = low.find(kw)
            while i != -1:
                li = bisect.bisect_right(starts, i) - 1
                if li < n:
                    cand.add(li)
                nxt = starts[li + 1] if li + 1 < len(starts) else len(low)
                i = low.find(kw, nxt)
        for m in _COORD_PREFILTER.finditer(text):
            li = bisect.bisect_right(starts, m.start()) - 1
            if li < n:
                cand.add(li)
        order = sorted(cand)
    else:
        # Case folding can change lengths outside ASCII: rank every line.
        order = range(len(lines))
    out = []
    for i in order:
        r = _signal_rank(lines[i])
        if r > 0:
            out.append((i + 1, r))
    return out


# ---------------------------------------------------------------- templates
_QUOTED_RE = re.compile(r"""'[^'\n]{0,200}'|"[^"\n]{0,200}"|`[^`\n]{0,200}`""")
_VOLATILE_RE = re.compile(r"[\d/@]")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_PATH_RE = re.compile(r"(?P<lead>(?:\.{1,2}/|/)*)(?P<first>[\w.+@-]+)/[\w.+@/-]*")


def _mask_quoted(m: re.Match[str]) -> str:
    q = m.group(0)
    if _VOLATILE_RE.search(q) is None:
        return q
    return q[0] + "<*>" + q[-1]


def message_key(line: str) -> str:
    """The template two lines share when they are the same message."""
    s = _QUOTED_RE.sub(_mask_quoted, line.strip())
    s = _HEX_RE.sub("<*>", s)
    s = _PATH_RE.sub(lambda m: f"{m.group('lead')}{m.group('first')}/<*>", s)
    return mask_line(s)


# ---------------------------------------------------------------- the map
@dataclass
class Message:
    stream: str
    key: str
    tier: int
    count: int
    first: int
    last: int
    focus: int = 0

    def ref(self) -> str:
        return f"{self.stream}:L{self.first}"


@dataclass
class EvidenceMap:
    messages: list[Message]          # local order (see local_order)
    signal_lines: int
    partial: bool                    # a stream was cut at the parse cap
    lines: dict[str, list[str]] = field(default_factory=dict)  # stream -> lines


def build(ctx: Any) -> EvidenceMap | None:
    """The map of ``ctx``'s streams, or None when they hold no signal line."""
    groups: dict[tuple[str, str], Message] = {}
    total = 0
    partial = False
    lines_by: dict[str, list[str]] = {}
    terms = tuple(sorted(set(getattr(ctx, "focus_terms", ()) or ())))
    for view in (ctx.stderr, ctx.stdout):
        if not view.bytes or not view.text:
            continue
        lines = view.text_lines
        lines_by[view.name] = lines
        partial = partial or not view.parsed_fully
        keys: dict[str, str] = {}   # repeated identical lines (tracebacks) template once
        for n, tier in signal_rows(view.text, lines):
            total += 1
            line = lines[n - 1]
            key = keys.get(line)
            if key is None:
                key = keys[line] = message_key(line)
            k = (view.name, key)
            m = groups.get(k)
            if m is None:
                low = line.lower()
                groups[k] = Message(view.name, k[1], tier, 1, n, n,
                                    sum(1 for t in terms if t in low))
            else:
                m.count += 1
                m.last = n
                if tier > m.tier:
                    m.tier = tier
    if not groups:
        return None
    return EvidenceMap(local_order(list(groups.values())), total, partial, lines_by)


def local_order(messages: list[Message]) -> list[Message]:
    return sorted(messages, key=lambda m: (-m.focus, -m.tier, m.count, -m.last, m.first,
                                           0 if m.stream == "stderr" else 1, m.key))


# ---------------------------------------------------------------- rendering
@dataclass
class Rendered:
    lines: list[str]
    suggestions: list[str]
    expanded: list[int]          # indices into emap.messages, pick order
    shown: int
    listed: int
    unlisted: int
    fired: set[str] = field(default_factory=set)
    windows: int = 0
    excerpt_lines: int = 0


def _shown_line(text: str, width: int, fired: set[str]) -> str:
    """``text`` with whole secrets replaced by their marker, then cut to
    ``width`` redaction-aware: redaction happens before the cut."""
    from ctx.textutil import mask_hits, secret_hits

    end = min(len(text), _REDACT_SCAN_CHARS)
    hits = secret_hits(text, 0, end)
    if hits:
        fired.update(h.name for h in hits)
        text = mask_hits(text, 0, end, hits) + text[end:]
    return clip_redacting(text, width)


def _windows(emap: EvidenceMap, picks: list[int], context: int) -> list[list[Any]]:
    """[stream, start, end, [message indices]] in pick order; a pick whose
    window overlaps or touches an earlier one in its stream joins it."""
    wins: list[list[Any]] = []
    for idx in picks:
        m = emap.messages[idx]
        n = len(emap.lines.get(m.stream, ()))
        a, b = max(1, m.first - context), min(n, m.first + context)
        for w in wins:
            if w[0] == m.stream and a <= w[2] + 1 and b >= w[1] - 1:
                w[1], w[2] = min(w[1], a), max(w[2], b)
                w[3].append(idx)
                break
        else:
            wins.append([m.stream, a, b, [idx]])
    # A single-occurrence message already inside a window gets its own
    # context too (one pass, not chained): a linker error two lines above the
    # picked `make: *** Error 1` is part of the same story.
    if context:
        singles: dict[str, list[int]] = {}
        for m in emap.messages:
            if m.count == 1:
                singles.setdefault(m.stream, []).append(m.first)
        for w in wins:
            lines_in = [n for n in singles.get(w[0], ()) if w[1] <= n <= w[2]]
            if lines_in:
                n_total = len(emap.lines.get(w[0], ()))
                w[1] = max(1, min(w[1], min(lines_in) - context))
                w[2] = min(n_total, max(w[2], max(lines_in) + context))
    # Merging can make two earlier windows touch: settle to a fixpoint.
    merged = True
    while merged:
        merged = False
        for i in range(len(wins)):
            for j in range(i + 1, len(wins)):
                x, y = wins[i], wins[j]
                if x[0] == y[0] and y[1] <= x[2] + 1 and y[2] >= x[1] - 1:
                    x[1], x[2] = min(x[1], y[1]), max(x[2], y[2])
                    x[3].extend(y[3])
                    del wins[j]
                    merged = True
                    break
            if merged:
                break
    return wins


def _loc(stream: str, n: int) -> str:
    return f"L{n}" if stream == "stdout" else f"{stream}:L{n}"


def render(ctx: Any, emap: EvidenceMap, *, cap_bytes: int, rid: str = "run:PENDING",
           order: list[int] | None = None, labels: dict[int, str] | None = None,
           k: int = EXPAND_K) -> Rendered:
    """The map's digest lines. ``order`` is the expansion order (indices into
    ``emap.messages``; default the local order); ``labels`` names who picked
    each expanded message. The list below the excerpts is always in the
    local order, whoever chose the excerpts."""
    msgs = emap.messages
    order = list(range(len(msgs))) if order is None else list(order)
    labels = labels or {}
    fired: set[str] = set()
    cap = max(200, int(cap_bytes))

    def excerpt_block(picks: list[int], context: int) -> tuple[list[str], set[int]]:
        wins = _windows(emap, picks, context)
        stats["windows"] = len(wins)
        stats["lines"] = sum(w[2] - w[1] + 1 for w in wins)
        shown = set(picks)
        out: list[str] = []
        for stream, a, b, members in wins:
            for i, m in enumerate(msgs):
                if m.count == 1 and m.stream == stream and a <= m.first <= b:
                    shown.add(i)
            head = f"  {stream} L{a}-L{b}"
            for idx in members:
                m = msgs[idx]
                bits = []
                if m.count > 1:
                    bits.append(f"L{m.first} recurs ×{fmt_int(m.count)}, last L{m.last}")
                if idx in labels:
                    bits.append(f"L{m.first} picked by {labels[idx]}")
                if bits:
                    head += " · " + " · ".join(bits)
            out.append(head + ":")
            lines = emap.lines[stream]
            for n in range(a, b + 1):
                out.append(f"    L{n}: {_shown_line(lines[n - 1], EXCERPT_LINE_CHARS, fired)}")
        return out, shown

    def size(rows: list[str]) -> int:
        return sum(len(r.encode("utf-8")) + 1 for r in rows)

    stats = {"windows": 0, "lines": 0}
    picks = order[:k]
    context = EXCERPT_CONTEXT
    block, shown = excerpt_block(picks, context)
    while size(block) > cap and (context > 0 or len(picks) > 1):
        if context > 0:
            context -= 1
        else:
            picks = picks[:-1]
        block, shown = excerpt_block(picks, context)

    listed: list[int] = []
    rows: list[str] = []
    used = size(block)
    for i, m in enumerate(msgs):
        if i in shown:
            continue
        if len(listed) >= LIST_ROWS:
            break
        span = (f"first {_loc(m.stream, m.first)}  last {_loc(m.stream, m.last)}"
                if m.count > 1 else _loc(m.stream, m.first))
        row = (f"    ×{fmt_int(m.count)}  {span}  "
               f"{_shown_line(emap.lines[m.stream][m.first - 1].strip(), LIST_SAMPLE_CHARS, fired)}")
        if used + len(row.encode("utf-8")) + 1 > cap:
            break
        rows.append(row)
        listed.append(i)
        used += len(row.encode("utf-8")) + 1

    total = len(msgs)
    unlisted = total - len(shown) - len(listed)
    scope = " in the parsed part" if emap.partial else ""
    out = [
        f"evidence map (exact{scope}): {fmt_int(emap.signal_lines)} error-like lines · "
        f"{fmt_int(total)} distinct messages · {fmt_int(len(shown))} shown with context · "
        f"{fmt_int(len(listed))} listed · {fmt_int(unlisted)} not listed",
    ]
    if block:
        out.append("  excerpts (verbatim, real line numbers):")
        out.extend(block)
    if rows:
        out.append("  other messages (×count · lines · first sample):")
        out.extend(rows)
    suggestions: list[str] = []
    if listed:
        m = msgs[listed[0]]
        n = len(emap.lines[m.stream])
        suggestions.append(
            f"ctx get {rid}#{m.stream} --lines {max(1, m.first - EXCERPT_CONTEXT)}:"
            f"{min(n, m.first + EXCERPT_CONTEXT)}")
    if unlisted == 0:
        out.append(f"  all {fmt_int(total)} distinct messages are above.")
    else:
        cmds = []
        for stream in ("stderr", "stdout"):
            if not any(msgs[i].stream == stream for i in range(total)
                       if i not in shown and i not in listed):
                continue
            view = ctx.stderr if stream == "stderr" else ctx.stdout
            in_stream = sum(1 for m in msgs if m.stream == stream)
            sid = (ctx.mint_span(view, "messages", a=1, b=in_stream)
                   if hasattr(ctx, "mint_span") else None)
            if sid:
                cmds.append(f"ctx get {rid}#{stream} --span {sid}")
        line = f"  +{fmt_int(unlisted)} distinct messages not listed"
        if cmds:
            line += ": " + "  ·  ".join(cmds)
            suggestions.extend(cmds)
        out.append(line)
    return Rendered(out, suggestions, [i for i in picks], len(shown), len(listed), unlisted, fired,
                    stats["windows"], stats["lines"])


# ---------------------------------------------------------------- profiles
def section(ctx: Any) -> tuple[list[str], list[str], Rendered] | None:
    """The evidence map for a profile's digest: (lines, next-step
    suggestions, render), or None when the whole output fits inline (the
    profile can show everything) or no line is a signal line. Runs the
    configured picker when the caller enabled one (``ctx.picker``)."""
    ws = getattr(ctx, "ws", None)
    budgets = getattr(getattr(ws, "config", None), "budgets", None)
    if budgets is None:
        return None
    content = ctx.stdout.bytes + ctx.stderr.bytes
    if content <= budgets.result_tokens * 3:
        return None
    emap = build(ctx)
    if emap is None:
        return None
    order = labels = None
    picker = getattr(ctx, "picker", None)
    if picker is not None:
        try:
            order, labels = picker.choose(emap, EXPAND_K)
        except Exception:  # noqa: BLE001 -- the local map is always the fallback
            order = labels = None
    r = render(ctx, emap, cap_bytes=getattr(budgets, "evidence_map_bytes", 1500),
               order=order, labels=labels)
    fired = getattr(ctx, "evmap_fired", None)
    if isinstance(fired, set):
        fired.update(r.fired)
    return r.lines, r.suggestions, r


def budget_tokens(ws: Any, digest: str) -> int:
    """Extra emission-budget tokens for a digest that carries an evidence
    map: the map has its own byte cap (``[budgets] evidence_map_bytes``),
    so the backstop that bounds the rest of the digest must not cut it."""
    if "\nevidence map (exact" not in digest:
        return 0
    return int(getattr(ws.config.budgets, "evidence_map_bytes", 1500)) // 4
