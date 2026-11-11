"""chunkmap/v1 — a navigable chunk table for large content handed to intake.

Applies ONLY to manifests created by :mod:`ctx.intake` (gated on
:func:`ctx.digest.base.from_intake`), and only when the payload is too large
for ``text/v1`` to inline. Command output never reaches this profile, so
every existing command digest stays byte-identical.

Decision the digest supports: *where in this paste is the part I need, and
how do I fetch exactly that part in one bounded call?* The loss that would
make it unsafe: rows whose addresses do not resolve, a silent cut of the
table or of its follow-ups, or a decisive error line nobody can see.

Shape::

    intake: <label>[ · <name>] · <lines> lines · <size> · est <N> tokens if inlined · looks like <flavor>
    decisive signal L<i>: <strongest error-like line, latest among equals>
    terminal signal L<j>: <last error-like line, when different>
    chunks: <N> (~<target> each) · shown <k> · error-like <s> (! rows show their signal line)
      #1 L1-L52 · 2.1 KiB · Install > Linux · span 0a1b2c3d4e
      #7 ! L301-L344 · 2.0 KiB · ValueError: bad input · span …
      … +<N-k> chunks omitted · list them: ctx intake --chunks <handle> --from <i>
    coverage: <k>/<N> chunks listed · map covers L1-L<lines> (<size>)
    next:
      ctx get <handle> --span <most decisive chunk>     (when there is one)
      ctx intake --chunks <handle> --from <i>
      ctx search <ref> '<pattern>' --context 3

Rows are fitted to the digest budget, measured on the text as it will be
emitted (control-stripped and redacted, with its redaction note), by dropping
the lowest-priority row first; first and last chunk are never dropped and the
``next:`` block is part of every candidate rendering, so the fit can never
cut it off.

Past the parse cap (``MAX_PARSE_BYTES``) only the parsed part is mapped, cut
back to its last whole line; every figure then names its scope (``total``,
``mapped``) and the unmapped tail is declared with a way to reach it.
"""

from __future__ import annotations

from ctx import bounds
from ctx.digest.base import MAX_PARSE_BYTES, DigestContext, Profile, from_intake
from ctx.digest.chunkmap import Chunk, chunk_map, clip_label, flavor
from ctx.textutil import EVIDENCE_LINE_CHARS, encode_exact, fmt_bytes, fmt_int, sanitize_for_model

# Room kept free in a span resolution for its own header lines
# (``[ctx get … span:…]`` and ``region: …``) so a chunk's cost bounds the
# whole retrieval, not only its body.
SPAN_HEADER_RESERVE = 256
# Room kept free in the digest for the ``[ctx run:… profile=…]`` header the
# registry prepends. Redaction is NOT reserved for: the fit measures the
# redacted text itself.
DIGEST_HEADER_RESERVE = 64
# Priority caps before budget fitting.
_MAX_SIGNAL_ROWS = 12
_MAX_FOCUS_ROWS = 6
_SPREAD_DEPTH = 4  # up to 15 evenly spaced rows before budget fitting
SIGNAL_ROW_CHARS = 72


def chunk_params(budgets, total_lines: int) -> dict[str, int]:
    """The cost parameters for :func:`chunk_map`, derived from the budgets.

    One chunk resolves through ``ctx get --span`` inside ``result_tokens``
    and inside ``max_inline_lines`` (above which a region span answers with a
    zoom digest instead of the exact lines). ``line_overhead`` is the
    ``L<n>: `` prefix that resolution puts on every line. The digest and the
    paged listing both call this, so they always describe the same map.
    """
    room = bounds.budget_bytes(budgets.result_tokens) - SPAN_HEADER_RESERVE
    max_cost = max(256, room)
    return {
        "target_cost": max_cost // 2,
        "max_cost": max_cost,
        "max_lines": max(1, bounds.count(budgets.max_inline_lines)),
        "line_overhead": len(f"L{max(1, total_lines)}: "),
    }


def mappable_text(text: str, parsed_fully: bool) -> str:
    """The part of a decoded stream the map covers: all of it, or -- past the
    parse cap -- the parsed prefix cut back to its last whole line, so no
    chunk claims a line or byte range it only partly saw."""
    if parsed_fully:
        return text
    cut = text.rfind("\n")
    if cut >= 0:
        return text[: cut + 1]
    # No whole line in the parsed prefix: map the part of the line that was
    # read, minus a trailing U+FFFD the cap may have made of a split UTF-8
    # sequence (its 3 encoded bytes would overstate what was mapped). The
    # rest of that line is the unmapped tail, reached by byte range.
    return text.rstrip("\ufffd")


def unmapped_tail(
    mapped: str, chunks: tuple[Chunk, ...], total_lines: int, total_bytes: int,
    handle: str, budgets,
) -> tuple[str, str]:
    """(description, command) for the stored bytes past the map.

    Addressed by BYTE range, never by line: the cap can land inside a line
    (a 40 MiB minified-JSON line, or a long line after short ones), and a
    ``--lines`` continuation then either names a line that does not exist or
    returns a whole multi-megabyte line through a clipped window. ``ctx get
    --bytes`` pages a byte range with its own continuation to the end, so
    following the printed commands reaches every byte.
    """
    mapped_bytes = chunks[-1].end_byte if chunks else 0
    first = mapped_bytes + 1
    window = max(1, bounds.budget_bytes(budgets.result_tokens) - SPAN_HEADER_RESERVE)
    last = min(total_bytes, first + window - 1)
    mid_line = bool(mapped) and not mapped.endswith("\n")
    start_line = chunks[-1].end_line if (chunks and mid_line) else (
        chunks[-1].end_line + 1 if chunks else 1)
    where = (
        f"rest of L{fmt_int(start_line)}" if mid_line and start_line >= total_lines
        else f"rest of L{fmt_int(start_line)} to L{fmt_int(total_lines)}" if mid_line
        else f"L{fmt_int(start_line)}-L{fmt_int(total_lines)}"
    )
    description = (
        f"NOT mapped: bytes {fmt_int(first)}-{fmt_int(total_bytes)} "
        f"({fmt_bytes(total_bytes - mapped_bytes)}, {where})"
    )
    return description, f"ctx get {handle} --bytes {first}:{last}"


def build_map(text: str, total_lines: int, budgets) -> tuple[Chunk, ...]:
    return chunk_map(text, **chunk_params(budgets, total_lines))


def mint_chunk_span(register, chunk: Chunk) -> str | None:
    """Mint the span that resolves ``chunk``: a line region, or a byte range
    for a piece of one oversized line. ``register(kind, a=, b=)`` is
    ``DigestContext.mint_span`` bound to a stream, or the store's own
    ``register_span`` bound to a blob."""
    if chunk.split == "bytes":
        return register("bytes", a=chunk.start_byte, b=chunk.end_byte)
    return register("region", a=chunk.start_line, b=chunk.end_line)


def chunk_row(chunk: Chunk, sid: str | None, *, focus_hit: bool = False) -> str:
    """One table row. A flagged chunk is labelled with its strongest signal
    line, not its heading or first line: that line is why it is flagged."""
    marks = ("!" if chunk.signal > 0 else "") + ("*" if focus_hit else "")
    where = (
        f"L{chunk.start_line} bytes {chunk.start_byte}:{chunk.end_byte}"
        if chunk.split == "bytes"
        else f"L{chunk.start_line}-L{chunk.end_line}"
    )
    label = chunk.label
    if chunk.signal > 0 and chunk.signal_text:
        label = clip_label(chunk.signal_text, SIGNAL_ROW_CHARS)
        if chunk.split == "lines" and chunk.signal_line != chunk.start_line:
            label = f"L{chunk.signal_line}: {label}"
    row = f"  #{chunk.index}{' ' + marks if marks else ''} {where} · {fmt_bytes(chunk.bytes)} · {label}"
    if sid:
        row += f" · span {sid}"
    return row


def emitted(text: str, policy) -> str:
    """``text`` exactly as the model will see it: control-stripped, redacted,
    with the redaction note the emitters append. Budgets are fitted on this."""
    out, redactions = sanitize_for_model(text, policy)
    if redactions:
        out += "\nredaction: applied [" + ", ".join(redactions) + "]"
    return out


def decisive_chunk(chunks: tuple[Chunk, ...]) -> Chunk | None:
    """The chunk holding the most decisive signal: highest rank, and among
    equals the LATEST -- conclusions (the exception a traceback ends in, the
    summary line of a failed run) come last."""
    flagged = [c for c in chunks if c.signal > 0]
    return max(flagged, key=lambda c: (c.signal, c.index)) if flagged else None


def _chunk_texts_lower(text: str, chunks: tuple[Chunk, ...]) -> list[str]:
    data = text.encode("utf-8")
    return [data[c.start_byte - 1:c.end_byte].decode("utf-8", "replace").lower() for c in chunks]


class ChunkMapProfile(Profile):
    version = "chunkmap/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        if not from_intake(ctx):
            return None
        view = ctx.stdout
        if ctx.stderr.bytes or not view.bytes or not view.text:
            return None
        if view.media_type.startswith("application/octet-stream"):
            return None
        # The same limit Profile.inline_body uses: below it text/v1 shows the
        # whole payload, which no table can beat.
        limit = ctx.ws.config.budgets.result_tokens * 3
        if view.bytes <= limit:
            return None
        return (
            f"intake payload of {fmt_bytes(view.bytes)} exceeds the "
            f"{fmt_bytes(limit)} inline limit"
        )

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        budgets = ctx.ws.config.budgets
        policy = ctx.ws.config.redaction
        text = mappable_text(view.text, view.parsed_fully)
        chunks = build_map(text, view.lines, budgets)
        n = len(chunks)
        rid = "run:PENDING"
        handle = f"{rid}#stdout"
        partial = not view.parsed_fully
        mapped_lines = chunks[-1].end_line if chunks else 0
        mapped_bytes = chunks[-1].end_byte if chunks else 0

        head = [ctx.intake_line(total=partial) + f" · looks like {flavor(text)}"]
        flagged = [c for c in chunks if c.signal > 0]
        signals = sorted(flagged, key=lambda c: (-c.signal, c.index))
        decisive = decisive_chunk(chunks)
        if decisive is not None:
            # Surfaced the way text/v1 surfaces its first and terminal
            # signal: the most decisive line (highest rank, latest among
            # equals) and, when it is a different line, the last one.
            head.append(f"decisive signal L{decisive.signal_line}: "
                        f"{decisive.signal_text[:EVIDENCE_LINE_CHARS]}")
            last = flagged[-1]
            if last.last_signal_line != decisive.signal_line:
                head.append(f"terminal signal L{last.last_signal_line}: "
                            f"{last.last_signal_text[:EVIDENCE_LINE_CHARS]}")

        focus_hits: set[int] = set()
        if ctx.focus_terms:
            for c, low in zip(chunks, _chunk_texts_lower(text, chunks)):
                if any(t in low for t in ctx.focus_terms):
                    focus_hits.add(c.index)

        # Priority order; budget fitting drops from the end.
        priority: list[int] = [1, n]
        if decisive is not None:
            priority.append(decisive.index)
        priority += sorted(focus_hits)[:_MAX_FOCUS_ROWS]
        priority += [c.index for c in signals[:_MAX_SIGNAL_ROWS]]
        # Even coverage that survives trimming from the end: bisection order
        # (1/2, then 1/4 and 3/4, then the eighths, …).
        for depth in range(1, _SPREAD_DEPTH + 1):
            for k in range(1, 2 ** depth, 2):
                priority.append(1 + round((n - 1) * k / 2 ** depth))
        seen: set[int] = set()
        priority = [i for i in priority if not (i in seen or seen.add(i))]

        sids: dict[int, str | None] = {}

        def sid_for(i: int) -> str | None:
            if i not in sids:
                sids[i] = mint_chunk_span(
                    lambda kind, **kw: ctx.mint_span(view, kind, **kw), chunks[i - 1]
                )
            return sids[i]

        legend = "! rows show their signal line" + (" · * matches focus" if focus_hits else "")
        target = fmt_bytes(chunk_params(budgets, view.lines)["target_cost"])
        scope = " in the mapped part" if partial else ""
        if partial:
            tail_text, tail_cmd = unmapped_tail(
                text, chunks, view.lines, view.bytes, handle, budgets)
            upto = "part of L1" if mapped_lines == 1 and not text.endswith("\n") else (
                f"L1-L{fmt_int(mapped_lines)}" + ("" if text.endswith("\n") else " (last one partly)"))
            coverage = (
                f"coverage: {{shown}}/{fmt_int(n)} chunks listed · mapped: bytes "
                f"1-{fmt_int(mapped_bytes)} = {upto} (parse cap "
                f"{fmt_bytes(MAX_PARSE_BYTES)}) · {tail_text}"
            )
        else:
            coverage = (
                f"coverage: {{shown}}/{fmt_int(n)} chunks listed · map covers "
                f"L1-L{fmt_int(mapped_lines)} ({fmt_bytes(mapped_bytes)}), all of it"
            )

        def assemble(k: int) -> str:
            shown = sorted(priority[:k])
            omitted = n - len(shown)
            first_hidden = next((i for i in range(1, n + 1) if i not in shown), None)
            lines = head + [
                f"chunks{scope}: {fmt_int(n)} (~{target} each) · shown {len(shown)} · "
                f"error-like {fmt_int(len(flagged))} ({legend})",
            ]
            lines += [chunk_row(chunks[i - 1], sid_for(i), focus_hit=i in focus_hits)
                      for i in shown]
            if omitted:
                lines.append(
                    f"  … +{fmt_int(omitted)} chunks omitted · list them: "
                    f"ctx intake --chunks {handle} --from {first_hidden}"
                )
            lines.append(coverage.format(shown=len(shown)))
            suggestions = []
            if decisive is not None and sid_for(decisive.index):
                suggestions.append(f"ctx get {handle} --span {sid_for(decisive.index)}")
            if partial:
                suggestions.append(tail_cmd)
            if first_hidden is not None:
                suggestions.append(f"ctx intake --chunks {handle} --from {first_hidden}")
            if decisive is None and shown and sid_for(shown[0]):
                suggestions.append(f"ctx get {handle} --span {sid_for(shown[0])}")
            suggestions.append(f"ctx search {rid} '<pattern>' --context 3")
            return "\n".join(lines + self.next_lines(ctx, suggestions))

        def size(body: str) -> int:
            # The run id replaces PENDING (12 hex chars for 7) on emission.
            return len(encode_exact(emitted(body, policy))) + 5 * body.count("run:PENDING")

        room = bounds.budget_bytes(budgets.digest_tokens) - DIGEST_HEADER_RESERVE
        k = len(priority)
        body = assemble(k)
        while size(body) > room and k > 2:
            k -= 1
            body = assemble(k)
        return body


__all__ = [
    "ChunkMapProfile", "build_map", "chunk_params", "chunk_row", "decisive_chunk",
    "emitted", "mappable_text", "mint_chunk_span", "unmapped_tail",
]
