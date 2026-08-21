"""Content anchors: a stable, verifiable identifier for lines of a mutable file.

straitjacket's promise is that omission is reversible — *"the same address
returns the same bytes tomorrow, next week, on another machine."* That holds
for the immutable side of the store (``run:``, ``blob:``, ``snapshot:``), where
the address names content that cannot change. It did **not** hold for the one
address family the model uses most while it is actually working: ``repo:``.

``repo:`` addresses are line numbers into a file the agent is editing, and a
line number is not an identity — it is a position. Insert two imports at the
top of a file and ``ctx get repo:m.py --lines 4:5`` still resolves, still exits
0, and returns *different lines*, with nothing in the output saying so. Every
navigation verb that hands the model a ``repo:<path> L<a>:<b>`` address (``ctx
def``, ``ctx refs``, ``ctx diag``, ``ctx map``, ``ctx get``) was minting
addresses with that failure mode built in.

This is the read-side twin of the edit-side failure the field already knows
well: string-replacement edits that miss because the model could not reproduce
a region byte-for-byte, and patch formats that apply against the wrong lines.
Both are the same missing primitive — *a stable, verifiable identifier for the
lines in question that does not cost the whole file in context.* An anchor is
that primitive on the retrieval side.

## The grammar

An anchor rides inside the selector the address already carries, so no emission
site grows a second field and no parser learns a second address shape::

    ctx get repo:m.py --lines 4:5@a3f1c2d9
                              ^^^^^^^^^ the content that WAS at lines 4:5

The anchor is a short digest over the *content* of the span, never over its
position. Re-resolving that address takes one of three outcomes, and which one
happened is always visible:

  * **verified** — the anchored content is still at those lines. The address
    resolves exactly as before, silently.
  * **relocated** — the content moved. Resolution finds it at its new position,
    returns the bytes the caller actually asked for, and declares the move
    along with the corrected address. An anchored address self-heals across
    edits that shift lines, which is what makes it usable as a working
    identifier and not merely a tripwire.
  * **lost** — the content is gone from the file. Resolution **refuses** rather
    than returning whatever now occupies those coordinates. A read that cannot
    keep its promise fails loudly; it does not quietly answer a different
    question.

Line tags (``L40:a3|``) are the same idea at line granularity, for when the
model needs to name individual lines rather than a span — two characters per
line, rendered only on request (``ctx get --hashlines``).

## Design constraints

  * **Pure and total.** No I/O, no store access, no shell-out — a hash and a
    scan over lines the caller already holds. Trivially testable, and it can
    never hang or flood.
  * **Position-free.** Anchors hash content and nothing else. An anchor minted
    for lines 4:5 and one minted for the same two lines at 6:7 are equal — that
    equality is exactly what makes relocation possible.
  * **Bounded.** Relocation searches outward from the address's stated position
    and stops at a fixed candidate cap, so the cost of a lost anchor is
    bounded and predictable rather than proportional to file size.
  * **Versioned.** Both digests are domain-separated with a scheme version, so
    a future change to the derivation is a mismatch (caught, refused) rather
    than a silent reinterpretation.
"""

from __future__ import annotations

import hashlib
import re

#: Width of an anchor: 8 hex characters of sha256 over the span's content
#: digests. 2**32 buckets against a candidate set bounded by
#: ``MAX_RELOCATION_CANDIDATES`` — a false relocation is ~5e-6 at the cap, and
#: an address costs 9 characters to carry ("@" + 8).
ANCHOR_CHARS = 8

#: Width of a per-line tag. Two characters is the field's convention and the
#: right one: a tag disambiguates a line from its neighbours in a window the
#: model is looking at, it does not identify a line globally. Cheap enough to
#: render on every line of a slice.
LINE_TAG_CHARS = 2

#: Ceiling on windows examined while relocating a moved span. Search runs
#: nearest-first from the address's stated position, so the cap trades the tail
#: of very large moves for a bounded worst case (~40 MB hashed) on any file.
MAX_RELOCATION_CANDIDATES = 20_000

#: Domain separators. Versioned so a derivation change is detected, not
#: silently reinterpreted (an old anchor simply stops matching, which routes
#: through the same refusal path as any other lost anchor).
_LINE_SCHEME = b"ctx.linetag/v1\x00"
_ANCHOR_SCHEME = b"ctx.anchor/v1\x00"

#: ``A:B`` or ``A:B@anchor``. The anchor is optional everywhere it is accepted,
#: so every address that worked before this module existed still works.
SPAN_RE = re.compile(r"^(\d+):(\d+)(?:@([0-9a-f]{%d}))?$" % ANCHOR_CHARS)

#: Rendered per-line prefix, e.g. ``L40:a3| def beta():``.
_TAGGED_LINE = "L{n}:{tag}| {text}"


def _line_digest(line: str) -> bytes:
    """The full per-line digest anchors are built from.

    Eight bytes rather than the full 32: the anchor hashes a *sequence* of
    these, and relocation re-hashes that sequence once per candidate window, so
    the width here sets the cost of the scan. Eight bytes keeps a 240-line
    window at under 2 KB per candidate while leaving per-line collisions
    (2**-64) far below the anchor's own width.
    """
    return hashlib.sha256(_LINE_SCHEME + line.encode("utf-8", "surrogatepass")).digest()[:8]


def line_tag(line: str) -> str:
    """The two-character content tag for one line.

    Derived from the same digest as the anchor, so a line's tag and its
    contribution to a span's anchor can never disagree about what that line is.
    """
    return _line_digest(line).hex()[:LINE_TAG_CHARS]


def anchor(lines) -> str:
    """The content anchor for a span of lines.

    Position-free by construction: only the lines' own bytes reach the hash, in
    order. Two spans with equal content have equal anchors wherever they sit in
    whatever file — the property relocation is built on.

    An empty span anchors to the empty-sequence digest rather than to a
    sentinel, so it round-trips through the same code path as any other span.
    """
    h = hashlib.sha256(_ANCHOR_SCHEME)
    for line in lines:
        h.update(_line_digest(line))
    return h.hexdigest()[:ANCHOR_CHARS]


def parse_span(spec: str) -> tuple[int, int, str | None]:
    """Parse ``A:B`` or ``A:B@anchor`` into ``(a, b, anchor_or_None)``.

    Raises ``ValueError`` with the accepted grammar on anything else. The
    caller translates that into its own error type — this module stays free of
    the retrieval package's exception hierarchy so the hook's import graph
    (see ``substitute.py``) never pulls retrieval in behind it.
    """
    m = SPAN_RE.match(spec.strip())
    if not m:
        raise ValueError(f"invalid span {spec!r}; expected A:B or A:B@anchor")
    a, b = int(m.group(1)), int(m.group(2))
    if a < 1 or b < a:
        raise ValueError(f"invalid span {spec!r}: need 1 <= A <= B")
    return a, b, m.group(3)


def format_span(a: int, b: int, span_anchor: str | None) -> str:
    """Render a selector value, with the anchor when there is one.

    One function so every emission site spells an anchored address the same
    way and ``parse_span`` is guaranteed to accept what any of them minted.
    """
    return f"{a}:{b}@{span_anchor}" if span_anchor else f"{a}:{b}"


def _candidate_starts(total: int, span_len: int, near: int):
    """1-based window starts, nearest-first from ``near``, capped.

    Nearest-first is not a heuristic about which match is *correct* — every
    candidate that matches the anchor holds byte-identical content, so any of
    them answers the caller's question. It decides which of several identical
    answers gets reported, and the nearest one produces the smallest, most
    reviewable "moved from → to" note. It is also what makes the candidate cap
    safe: the windows most likely to hold the content are examined first.
    """
    last = total - span_len + 1
    if last < 1:
        return
    near = min(max(near, 1), last)
    yielded = 0
    for delta in range(0, last):
        for start in ((near - delta, near + delta) if delta else (near,)):
            if 1 <= start <= last:
                yield start
                yielded += 1
                if yielded >= MAX_RELOCATION_CANDIDATES:
                    return
        if near - delta < 1 and near + delta > last:
            return


def relocate(all_lines, want: str, span_len: int, near: int) -> int | None:
    """Find where anchored content moved to, or ``None`` if it is gone.

    Returns the 1-based start line of the nearest window whose content anchors
    to ``want``. ``None`` means the content is not in the file within the
    candidate cap — the caller must refuse, not guess.

    Digests are computed once per line and reused across every candidate
    window, so the scan costs one pass of hashing plus ``span_len * 8`` bytes
    per candidate examined.
    """
    total = len(all_lines)
    if span_len < 1 or span_len > total:
        return None
    digests = [_line_digest(ln) for ln in all_lines]
    for start in _candidate_starts(total, span_len, near):
        h = hashlib.sha256(_ANCHOR_SCHEME)
        for d in digests[start - 1 : start - 1 + span_len]:
            h.update(d)
        if h.hexdigest()[:ANCHOR_CHARS] == want:
            return start
    return None


def render_window(window, start: int, *, tagged: bool = False) -> list[str]:
    """Render already-sliced lines as display lines numbered from ``start``.

    ``tagged`` switches ``L40: text`` to ``L40:a3| text``. Both shapes are
    produced here rather than at the call sites so the untagged rendering — the
    one every existing digest, test, and receipt already depends on being
    byte-identical — cannot drift away from the tagged one.

    Takes the window rather than the whole file and a range, because the
    retrieval paths that seek into a blob only ever hold the window; making
    them materialize the file to render it would undo the seek.
    """
    if not tagged:
        return [f"L{start + i}: {ln}" for i, ln in enumerate(window)]
    return [
        _TAGGED_LINE.format(n=start + i, tag=line_tag(ln), text=ln)
        for i, ln in enumerate(window)
    ]
