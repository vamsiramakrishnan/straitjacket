"""Evidence map: exact accounting of the error-like lines a digest did not show.

A digest that shows a few lines of a 9,000-line log asks the reader to trust
whatever chose them. The map removes that trust: every *signal line* (a line
:func:`ctx.digest.text._signal_rank` rates tier >= 1 -- error words, failure
counts, exit codes, ``file:line`` coordinates; ANSI colour removed first) is
grouped into a DISTINCT MESSAGE, and the digest states, exactly, how many
signal lines and distinct messages there are, which messages it shows with
context, which it lists, and how many it does not list -- with the command
that lists those. A claim is printed only when the delivered bytes support it.

Grouping (:func:`skeleton` then :func:`build`):

* Masked unconditionally, because they are volatile by nature: timestamps
  and dates, durations with a unit, long hex ids and memory addresses,
  uuids, and pids/thread ids in the usual positions (``prog[123]``,
  ``pid=123``, ``thread-7``).
* Every other token that carries a digit, a quote, ``/``, ``@`` or ``=`` is
  a *slot*. Lines with the same skeleton (words + slot positions) form a
  group; within it a slot that takes at most ``FEW_VALUES`` distinct values,
  some of them repeated (or a group of at most two lines), SPLITS the group (``exit code 1`` and ``exit code 137`` are two messages,
  and the second ranks as rare), while a slot with more values (ids,
  counters, row counts) stays collapsed and the message says so exactly:
  ``varies: <value in the sample> (4,191 distinct values)``.

Each message shows one verbatim sample (its first occurrence), which carries
every split value. Ranking (:func:`local_order`): focus matches, higher tier,
rarer, later last occurrence, earlier first occurrence, stderr first, key.

Spending order: one list row per message, rare first, is reserved before any
excerpt -- the list is what the guarantee rests on, context is a convenience.
The top ``EXPAND_K`` messages are then expanded with ``EXCERPT_CONTEXT``
lines either side (real line numbers; adjacent windows merge) only as far as
that never pushes a message off the list. A message counts as *shown* when it
is expanded or occurs once inside an excerpt; shown + listed + not listed
always equals the number of distinct messages. Long lines are windowed
around the signal and every cut is marked (``… +N chars``).

Deterministic; every shown line is redacted (whole secrets replaced by their
marker, private-key body lines replaced) BEFORE it is cut or measured.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any

from ctx.digest.text import _signal_rank
from ctx.textutil import EVIDENCE_LINE_CHARS, clip_redacting, fmt_int, index_lines

EXPAND_K = 3
EXCERPT_CONTEXT = 2
EXCERPT_LINE_CHARS = EVIDENCE_LINE_CHARS
LIST_SAMPLE_CHARS = 110
#: A slot with at most this many distinct values splits its group.
FEW_VALUES = 8
#: A secret is judged on at most this many leading characters of a line
#: (constant cost per shown line, like ``redact_before_transform``).
_REDACT_SCAN_CHARS = 4096
MAP_VERSION = "evmap/v2"
ACCOUNT_PREFIX = "evidence map (exact"

# ---------------------------------------------------------------- ANSI
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text) if "\x1b" in text else text


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


# ---------------------------------------------------------------- skeletons
_VOLATILE_RES = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?"
                r"(?:Z|[+-]\d{2}:?\d{2})?)?\b"), "<ts>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ns|us|µs|ms|s|sec|secs|m|min|h)\b"), "<dur>"),
    (re.compile(r"\b0x[0-9a-fA-F]{4,}\b|\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"(?<=[A-Za-z_])\[\d+\](?=:)"), "[<pid>]"),
    (re.compile(r"\b(?:pid|tid)([=: ])\d+\b", re.IGNORECASE), r"pid\1<pid>"),
    (re.compile(r"\bthread[-_ ]?\d+\b", re.IGNORECASE), "thread-<tid>"),
)
_SLOT_RE = re.compile(r"[\d/'\"`@=]")
_PLACEHOLDERS = ("<ts>", "<dur>", "<hex>", "<uuid>", "pid=<pid>", "pid:<pid>", "[<pid>]", "<pid>",
                 "<tid>")


def skeleton(line: str) -> tuple[str, tuple[str, ...]]:
    """(skeleton, slot values) of a (stripped) line."""
    s = line.strip()
    for rx, rep in _VOLATILE_RES:
        s = rx.sub(rep, s)
    out: list[str] = []
    vals: list[str] = []
    for tok in s.split():
        bare = tok
        if "<" in tok:
            for ph in _PLACEHOLDERS:
                bare = bare.replace(ph, "")
        if _SLOT_RE.search(bare):
            out.append("<*>")
            vals.append(tok)
        else:
            out.append(tok)
    return " ".join(out), tuple(vals)


def message_key(line: str) -> str:
    """The skeleton key (slots unresolved). Kept for callers that only need
    the coarse template; grouping uses :func:`build`."""
    return skeleton(line)[0]


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
    #: collapsed slots: (value in the sample line, distinct values)
    varies: list[tuple[str, int]] = field(default_factory=list)

    def ref(self) -> str:
        return f"{self.stream}:L{self.first}"


@dataclass
class EvidenceMap:
    messages: list[Message]          # local order (see local_order)
    signal_lines: int
    partial: bool                    # a stream was cut at the parse cap
    lines: dict[str, list[str]] = field(default_factory=dict)  # stream -> clean lines
    pem: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    parsed: dict[str, tuple[int, int]] = field(default_factory=dict)  # stream -> (parsed, total)
    first_line: int = 1              # number of the first mapped line (spans)


def _group(stream: str, rows: list[tuple[int, int, tuple[str, ...]]], skel: str,
           lines: list[str], terms: tuple[str, ...], first_line: int) -> list[Message]:
    nslots = len(rows[0][2])
    split = []
    count = len(rows)
    for j in range(nslots):
        seen: set[str] = set()
        for _, _, vals in rows:
            seen.add(vals[j])
            if len(seen) > FEW_VALUES:
                break
        # Few values split the group -- unless every line has its own value
        # (3+ lines, all different: an id or a counter, not a category).
        split.append(len(seen) <= FEW_VALUES and (len(seen) < count or count <= 2))
    msgs: dict[tuple[str, ...], list[Any]] = {}
    for n, tier, vals in rows:
        key = tuple(v for j, v in enumerate(vals) if split[j])
        rec = msgs.get(key)
        if rec is None:
            rec = msgs[key] = [n, n, 0, tier, [set() for j in range(nslots) if not split[j]], vals]
        rec[1] = n
        rec[2] += 1
        rec[3] = max(rec[3], tier)
        col = 0
        for j in range(nslots):
            if not split[j]:
                rec[4][col].add(vals[j])
                col += 1
    out = []
    for key, (first, last, count, tier, sets, sample_vals) in msgs.items():
        coll = [sample_vals[j] for j in range(nslots) if not split[j]]
        low = lines[first - first_line].lower()
        out.append(Message(stream, skel + "\x00" + "\x00".join(key), tier, count, first, last,
                           sum(1 for t in terms if t in low),
                           [(coll[c], len(s)) for c, s in enumerate(sets)]))
    return out


def build_streams(views: list[tuple[str, str, bool, int]], terms: tuple[str, ...] = (),
                  first_line: int = 1) -> EvidenceMap | None:
    """Map of ``(name, text, parsed_fully, total_lines)`` streams."""
    from ctx.textutil import private_key_line_ranges

    total = 0
    partial = False
    lines_by: dict[str, list[str]] = {}
    pem: dict[str, list[tuple[int, int]]] = {}
    parsed: dict[str, tuple[int, int]] = {}
    messages: list[Message] = []
    for name, raw, whole, nlines in views:
        if not raw:
            continue
        text = strip_ansi(raw)
        lines = index_lines(text)
        lines_by[name] = lines
        pem[name] = [(a + first_line - 1, b + first_line - 1)
                     for a, b in private_key_line_ranges(text)]
        parsed[name] = (len(lines), max(nlines, len(lines)))
        partial = partial or not whole
        groups: dict[str, list[tuple[int, int, tuple[str, ...]]]] = {}
        cache: dict[str, tuple[str, tuple[str, ...]]] = {}
        for n, tier in signal_rows(text, lines):
            total += 1
            line = lines[n - 1]
            sk = cache.get(line)
            if sk is None:
                sk = cache[line] = skeleton(line)
            groups.setdefault(sk[0], []).append((n + first_line - 1, tier, sk[1]))
        for skel, rows in groups.items():
            messages.extend(_group(name, rows, skel, lines, terms, first_line))
    if not messages:
        return None
    return EvidenceMap(local_order(messages), total, partial, lines_by, pem, parsed, first_line)


def build(ctx: Any) -> EvidenceMap | None:
    """The map of ``ctx``'s streams, or None when they hold no signal line."""
    terms = tuple(sorted(set(getattr(ctx, "focus_terms", ()) or ())))
    views = [(v.name, v.text, v.parsed_fully, v.lines)
             for v in (ctx.stderr, ctx.stdout) if v.bytes and v.text]
    return build_streams(views, terms)


def local_order(messages: list[Message]) -> list[Message]:
    return sorted(messages, key=lambda m: (-m.focus, -m.tier, m.count, -m.last, m.first,
                                           0 if m.stream == "stderr" else 1, m.key))


def line_of(emap: EvidenceMap, stream: str, n: int) -> str:
    """Line ``n`` of ``stream`` as it may be shown: ANSI removed, a
    private-key body line replaced by the redaction marker."""
    from ctx.textutil import PRIVATE_KEY_LINE

    for a, b in emap.pem.get(stream, ()):
        if a <= n <= b:
            return PRIVATE_KEY_LINE
    return emap.lines[stream][n - emap.first_line]


def n_lines(emap: EvidenceMap, stream: str) -> int:
    return len(emap.lines.get(stream, ())) + emap.first_line - 1


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


_SIGNAL_POS_RE = re.compile(
    r"error|fail|exception|traceback|fatal|panic|denied|refused|timeout|timed out|cannot|unable"
    r"|critical|permission|err!|exit|status|quota|oomkilled|\.\w+:\d", re.IGNORECASE)
_WINDOW_LEAD = 40


def _redacted(text: str, fired: set[str]) -> str:
    from ctx.textutil import mask_hits, secret_hits

    end = min(len(text), _REDACT_SCAN_CHARS)
    hits = secret_hits(text, 0, end)
    if hits:
        fired.update(h.name for h in hits)
        text = mask_hits(text, 0, end, hits) + text[end:]
    return text


def _shown_line(text: str, width: int, fired: set[str]) -> str:
    """``text`` redacted (whole secrets replaced by their marker), then fitted
    to ``width``: a line that is too long is windowed around the first signal
    word and every cut is marked ``… +N chars``. Redaction happens before
    the cut; the cuts themselves are redaction-aware."""
    from ctx.textutil import tail_redacting

    text = _redacted(text, fired)
    if len(text) <= width:
        return text
    m = _SIGNAL_POS_RE.search(text)
    pos = m.start() if m else 0
    if pos + 30 <= width:
        return clip_redacting(text, width) + f" … +{len(text) - width} chars"
    a = max(0, pos - _WINDOW_LEAD)
    rest = tail_redacting(text, len(text) - a)
    shown = clip_redacting(rest, width)
    after = len(rest) - len(shown)
    return f"… +{a} chars " + shown + (f" … +{after} chars" if after > 0 else "")


def _windows(emap: EvidenceMap, picks: list[int], context: int) -> list[list[Any]]:
    """[stream, start, end, [message indices]] in pick order; a pick whose
    window overlaps or touches an earlier one in its stream joins it."""
    wins: list[list[Any]] = []
    for idx in picks:
        m = emap.messages[idx]
        lo, hi = emap.first_line, n_lines(emap, m.stream)
        a, b = max(lo, m.first - context), min(hi, m.first + context)
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
                w[1] = max(emap.first_line, min(w[1], min(lines_in) - context))
                w[2] = min(n_lines(emap, w[0]), max(w[2], max(lines_in) + context))
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


def _varies(m: Message, fired: set[str]) -> str:
    if not m.varies:
        return ""
    bits = [f"`{_shown_line(v, 24, fired)}` ({fmt_int(c)} distinct values)" for v, c in m.varies]
    return " · varies: " + ", ".join(bits)


def _size(rows: list[str]) -> int:
    return sum(len(r.encode("utf-8")) + 1 for r in rows)


def render(ctx: Any, emap: EvidenceMap, *, cap_bytes: int, rid: str = "run:PENDING",
           order: list[int] | None = None, labels: dict[int, str] | None = None,
           k: int = EXPAND_K) -> Rendered:
    """The map's digest lines. ``order`` is the expansion order (indices into
    ``emap.messages``; default the local order); ``labels`` names who picked
    each expanded message. The list is always in the local order, whoever
    chose the excerpts, and excerpts never push a message off it."""
    msgs = emap.messages
    order = list(range(len(msgs))) if order is None else list(order)
    labels = labels or {}
    fired: set[str] = set()
    cap = max(200, int(cap_bytes))

    row_text: dict[int, str] = {}

    def row(i: int) -> str:
        r = row_text.get(i)
        if r is None:
            m = msgs[i]
            span = (f"first {_loc(m.stream, m.first)}  last {_loc(m.stream, m.last)}"
                    if m.count > 1 else _loc(m.stream, m.first))
            r = row_text[i] = (f"    ×{fmt_int(m.count)}  {span}  "
                               f"{_shown_line(line_of(emap, m.stream, m.first).strip(), LIST_SAMPLE_CHARS, fired)}"
                               f"{_varies(m, fired)}")
        return r

    def fit_rows(exclude: set[int], room: int) -> tuple[list[int], int]:
        used = 0
        out: list[int] = []
        for i in range(len(msgs)):        # local order: rare first
            if i in exclude:
                continue
            b = len(row(i).encode("utf-8")) + 1
            if used + b > room:
                break
            out.append(i)
            used += b
        return out, used

    def excerpt_block(picks: list[int], context: int) -> tuple[list[str], set[int], int, int]:
        wins = _windows(emap, picks, context)
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
                    bits.append(f"L{m.first} recurs ×{fmt_int(m.count)}, last L{m.last}"
                                + _varies(m, fired).replace(" · varies:", ", varies:"))
                if idx in labels:
                    bits.append(f"L{m.first} picked by {labels[idx]}")
                if bits:
                    head += " · " + " · ".join(bits)
            out.append(head + ":")
            for n in range(a, b + 1):
                out.append(f"    L{n}: {_shown_line(line_of(emap, stream, n), EXCERPT_LINE_CHARS, fired)}")
        return out, shown, len(wins), sum(w[2] - w[1] + 1 for w in wins)

    # 1. the list first: as many rows as fit, rare first
    base_listed, _ = fit_rows(set(), cap)
    base_set = set(base_listed)
    # 2. excerpts with what is left, never dropping a row from (1)
    block: list[str] = []
    shown: set[int] = set()
    listed = base_listed
    windows = excerpt_n = 0
    picks: list[int] = []
    done = False
    for j in range(min(k, len(order)), 0, -1):
        for context in (EXCERPT_CONTEXT, 1, 0):
            cand = order[:j]
            blk, shw, nw, nl = excerpt_block(cand, context)
            room = cap - _size(blk)
            if room < 0:
                continue
            lst, _ = fit_rows(shw, room)
            if (base_set - shw) <= set(lst):
                block, shown, listed, windows, excerpt_n, picks = blk, shw, lst, nw, nl, cand
                done = True
                break
        if done:
            break

    total = len(msgs)
    unlisted = total - len(shown) - len(listed)
    if emap.partial:
        spans = [f"{s} lines {emap.first_line}-{p + emap.first_line - 1} of {fmt_int(t)}"
                 for s, (p, t) in sorted(emap.parsed.items())]
        scope = " for " + ", ".join(spans) + "; later lines are not in this map"
    else:
        scope = ""
    out = [
        f"{ACCOUNT_PREFIX}{scope}): {fmt_int(emap.signal_lines)} error-like lines · "
        f"{fmt_int(total)} distinct messages · {fmt_int(len(shown))} shown with context · "
        f"{fmt_int(len(listed))} listed · {fmt_int(unlisted)} not listed",
    ]
    if block:
        out.append("  excerpts (verbatim except control characters; real line numbers):")
        out.extend(block)
    if listed:
        out.append("  other messages (×count · lines · first sample):")
        out.extend(row(i) for i in listed)
    suggestions: list[str] = []
    if listed:
        m = msgs[listed[0]]
        suggestions.append(
            f"ctx get {rid}#{m.stream} --lines {max(1, m.first - EXCERPT_CONTEXT)}:"
            f"{min(n_lines(emap, m.stream), m.first + EXCERPT_CONTEXT)}")
    if unlisted == 0 and not emap.partial:
        varies = any(msgs[i].varies for i in range(total))
        out.append(f"  every error-like line is one of these {fmt_int(total)} messages"
                   + ("; values marked varies are collapsed." if varies else "."))
    if unlisted:
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
    if emap.partial:
        cmds = []
        for stream, (p, t) in sorted(emap.parsed.items()):
            if t > p and hasattr(ctx, "mint_span"):
                view = ctx.stderr if stream == "stderr" else ctx.stdout
                sid = ctx.mint_span(view, "messages_from", a=p + 1, b=1)
                if sid:
                    cmds.append(f"ctx get {rid}#{stream} --span {sid}")
        line = "  lines past the parse cap are not mapped"
        if cmds:
            line += "; map them: " + "  ·  ".join(cmds)
            suggestions.extend(cmds)
        out.append(line)
    return Rendered(out, suggestions, list(picks), len(shown), len(listed), unlisted, fired,
                    windows, excerpt_n)


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
    r = render(ctx, emap, cap_bytes=map_cap(budgets), order=order, labels=labels)
    fired = getattr(ctx, "evmap_fired", None)
    if isinstance(fired, set):
        fired.update(r.fired)
    return r.lines, r.suggestions, r


def map_cap(budgets: Any) -> int:
    """``[budgets] evidence_map_bytes``, clamped to 400..8000."""
    try:
        v = int(getattr(budgets, "evidence_map_bytes", 1500))
    except (TypeError, ValueError):
        v = 1500
    return max(400, min(v, 8000))


def budget_tokens(ws: Any, digest: str) -> int:
    """Extra emission-budget tokens for a digest that carries an evidence
    map: exactly the map section's own size (at most its cap plus its
    accounting and closing lines), so the backstop that bounds the rest of
    the digest never cuts the map. Not multiplied by the failure factor."""
    sec = map_section(digest)
    if sec is None:
        return 0
    _, a, b = sec
    lines = digest.split("\n")
    return (len("\n".join(lines[a:b]).encode("utf-8")) + 4) // 4


# ---------------------------------------------------------------- delivery
_ACCOUNT_RE = re.compile(
    r"^evidence map \(exact[^)]*\): [\d,]+ error-like lines · ([\d,]+) distinct messages · "
    r"([\d,]+) shown with context · ([\d,]+) listed · ([\d,]+) not listed$")


def map_section(text: str) -> tuple[list[str], int, int] | None:
    """(lines, start, end) of the map inside ``text`` (end exclusive)."""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(ACCOUNT_PREFIX):
            j = i + 1
            while j < len(lines) and lines[j].startswith("  "):
                j += 1
            return lines, i, j
    return None


def map_supported(text: str) -> bool:
    """Does ``text`` -- what the reader receives -- support every claim its
    evidence map makes? True when there is no map. Checks: the accounting
    adds up, every listed row is present, a non-zero "not listed" count has
    its line, and the closing sentence appears only when nothing is
    unlisted and the parse was whole."""
    sec = map_section(text)
    if sec is None:
        return True
    lines, a, b = sec
    m = _ACCOUNT_RE.match(lines[a])
    if not m:
        return False
    total, shown, listed, unlisted = (int(g.replace(",", "")) for g in m.groups())
    if shown + listed + unlisted != total:
        return False
    body = lines[a + 1:b]
    rows = sum(1 for ln in body if ln.startswith("    ×"))
    closing = any(ln.startswith("  every error-like line is one of these") for ln in body)
    notlisted = any(re.match(r"^  \+[\d,]+ distinct messages not listed", ln) for ln in body)
    if rows != listed:
        return False
    if unlisted and not notlisted:
        return False
    if closing and (unlisted or "later lines are not in this map" in lines[a]):
        return False
    if not unlisted and "later lines are not in this map" not in lines[a] and not closing:
        return False
    return True


def fit_digest(text: str, budget_tokens_: int, reserve_bytes: int = 0) -> str:
    """``text`` within ``budget_tokens_`` with its evidence map whole: what
    must give way is the profile body above the map (cut from its bottom,
    with a marker) and then the lines below it. The map is never cut; a
    map that does not fit even alone is removed whole, with one line saying
    so, rather than delivered with claims its bytes no longer support."""
    from ctx import bounds as _bounds

    budget = _bounds.budget_bytes(budget_tokens_)
    if len(text.encode("utf-8")) <= budget:
        return text
    budget = max(0, budget - reserve_bytes)
    sec = map_section(text)
    if sec is None:
        return text
    lines, a, b = sec
    pre, mp, post = lines[:a], lines[a:b], lines[b:]
    marker = "[ctx:truncated profile summary to fit; the evidence map below is whole]"
    need = _size(pre[:1]) + _size([marker]) + _size(mp)
    if need > budget:
        handle = re.search(r"\[ctx run:([0-9a-f]+|PENDING)", text)
        note = "evidence map omitted: it did not fit the output budget"
        if handle:
            note += f"; ctx get run:{handle.group(1)}#stdout --lines 1:40 to start reading"
        return "\n".join(pre + [note] + post)
    keep = 1
    while keep < len(pre) and _size(pre[:keep + 1]) + _size([marker]) + _size(mp) <= budget:
        keep += 1
    out = pre[:keep] + ([marker] if keep < len(pre) else []) + mp
    for ln in post:
        if _size(out + [ln]) > budget:
            break
        out.append(ln)
    return "\n".join(out)


def drop_map(text: str) -> str:
    """``text`` without its evidence map (the last resort of delivery)."""
    sec = map_section(text)
    if sec is None:
        return text
    lines, a, b = sec
    return "\n".join(lines[:a] + ["evidence map omitted: it did not fit the output budget"]
                     + lines[b:])


def deliver(text: str, budget: int, continuation: str | None = None,
            truncation_continuation: str | None = None) -> str:
    """The emission backstop for a digest that may carry a map: fit it map
    first (:func:`fit_digest`), bound it, then check that the delivered
    bytes support every claim the map makes; if they do not, deliver the
    digest without the map instead."""
    from ctx.textutil import bounded

    tail = f"\nnext: {truncation_continuation}" if truncation_continuation else ""
    fitted = fit_digest(text, budget, len(tail.encode("utf-8")))
    if fitted != text and map_section(fitted) is not None:
        # Cut around a whole map: the cut is declared by fit_digest's marker
        # and the reader still gets the retrieval handle.
        fitted += tail
    out = bounded(fitted, budget, continuation, truncation_continuation=truncation_continuation)
    if map_supported(out):
        return out
    return bounded(drop_map(fitted), budget, continuation,
                   truncation_continuation=truncation_continuation)
