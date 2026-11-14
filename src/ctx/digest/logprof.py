"""Drain-style log template mining profile (SPEC §9: structured logs).

Volatile tokens (numbers, hex, UUIDs, timestamps, durations, IPs,
digit-bearing identifiers) are masked to ``<*>``; the masked token sequence
is the template key. Rare templates (≤2 occurrences) are surfaced verbatim
as exceptional evidence, with an explicit count when capped — no silent
omission (SPEC §8).
"""

from __future__ import annotations

import functools
import re

from ctx.digest.base import DigestContext, Profile
from ctx.textutil import EVIDENCE_LINE_CHARS, clip_redacting, fmt_int

_LEVEL_RE = re.compile(r"\b(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|FATAL)\b")
_TS_PREFIX_RE = re.compile(r"^\[?(?:\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}:\d{2})")
_SALIENT_RE = re.compile(r"error|fail|exception|fatal|panic|timeout", re.IGNORECASE)

# Masking rules applied in fixed order: composite shapes first so a
# timestamp/UUID/IP collapses to a single <*> instead of several fragments.
_MASK_RES = (
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
    re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"),
    re.compile(r"\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?"),
    re.compile(r"\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?"),
    re.compile(r"\b\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h)\b"),
    re.compile(r"\b[0-9a-fA-F]{6,}\b"),
    re.compile(r"\d+(?:\.\d+)?"),
)
_COLLAPSE_RE = re.compile(r"<\*>(?:[-:.,/]<\*>)+")
_HAS_DIGIT_RE = re.compile(r"\d")


# Token-level memoization: real logs repeat the same volatile tokens (pids,
# ports, level words) across thousands of lines. The mask is a pure function
# of the token, so a bounded LRU changes nothing but speed. (S5 library-hunt
# ADOPT #2: a hand-rolled "stop caching when full" dict → stdlib lru_cache.)
@functools.lru_cache(maxsize=65536)
def _mask_token(tok: str) -> str:
    # Digit-free tokens are stable words; masking them would merge unrelated
    # templates (all-letter hex like "facade" stays literal by design).
    if _HAS_DIGIT_RE.search(tok) is None:
        return tok
    masked = tok
    for rx in _MASK_RES:
        masked = rx.sub("<*>", masked)
    return _COLLAPSE_RE.sub("<*>", masked)


def mask_line(raw: str) -> str:
    """Public template key for a line — shared by digesting and span zoom."""
    return " ".join(_mask_token(t) for t in raw.split())


_TEMPLATE_SOURCE_CAP = 8192


def display_template(raw: str, width: int = EVIDENCE_LINE_CHARS, policy=None, *,
                     in_key_block: bool = False) -> str:
    """The template of ``raw`` as displayed, built from the REDACTED line.

    Templates mask digit runs (``<*>``); masking first and redacting the
    template afterwards shows a token mangled and unmatched
    (``ghp_bA<*>J<*>PglOU…``). So the line is redacted first -- secrets
    judged in bounded context on its first 8 KiB -- each secret becomes its
    standard marker, and the template is built from that. Byte for byte the
    old rendering (``mask_line`` clipped redaction-aware) for a line without
    a secret. Only DISPLAY uses this; the grouping key stays ``mask_line``.
    """
    from ctx.textutil import (
        PRIVATE_KEY_LINE,
        active_patterns,
        merge_hits,
        secret_hits,
        secret_marker,
    )

    if in_key_block:
        # A private-key body line is a secret only with its BEGIN line, which
        # a single line does not carry: the caller located the block first.
        return clip_redacting(PRIVATE_KEY_LINE, width, policy)
    hits = []
    if active_patterns(policy):
        src = raw[:_TEMPLATE_SOURCE_CAP]
        hits = secret_hits(src, 0, len(src), policy=policy)
    if not hits:
        return clip_redacting(mask_line(raw), width, policy)
    merged = merge_hits(hits)
    pieces: list[str] = []
    markers: dict[str, str] = {}
    pos = 0
    for k, (a, b, h) in enumerate(merged):
        ph = chr(0xE000 + k)
        markers[ph] = secret_marker(h.name, src[h.whole_start:h.whole_end])
        pieces.append(src[pos:a])
        pieces.append(ph)
        pos = b
    pieces.append(raw[pos:])
    tpl = mask_line("".join(pieces))
    for ph, marker in markers.items():
        tpl = tpl.replace(ph, marker)
    return clip_redacting(tpl, width, policy)


def _private_key_line_ranges(text: str) -> list[tuple[int, int]]:
    from ctx.textutil import private_key_line_ranges

    return private_key_line_ranges(text)


def _in_ranges(n: int, ranges) -> bool:
    from ctx.textutil import in_line_ranges

    return in_line_ranges(n, ranges)


def mine_templates(
    lines: list[str], first_line_no: int = 1
) -> tuple[dict[str, list[int]], list[tuple[int, str, str]]]:
    """Mine (templates -> [count, first_line_no], mined records). Line
    numbers are absolute via ``first_line_no`` so span zoom can mine a
    region slice while preserving artifact coordinates."""
    templates: dict[str, list[int]] = {}
    mined: list[tuple[int, str, str]] = []
    for i, raw in enumerate(lines, start=first_line_no):
        if not raw.strip():
            continue
        tpl = mask_line(raw)
        rec = templates.get(tpl)
        if rec is None:
            templates[tpl] = [1, i]
        else:
            rec[0] += 1
        mined.append((i, tpl, raw))
    return templates, mined


class LogTemplateProfile(Profile):
    version = "logtemplate/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        non_empty = [ln for ln in ctx.stdout.text_lines if ln.strip()]
        if len(non_empty) < 80:
            return None
        sample = non_empty[:200]
        hits = sum(1 for ln in sample if _LEVEL_RE.search(ln) or _TS_PREFIX_RE.match(ln))
        if hits * 100 < 40 * len(sample):
            return None
        return (
            f"{hits}/{len(sample)} of first non-empty stdout lines carry a "
            f"level token or timestamp prefix ({hits * 100 // len(sample)}%)"
        )

    def render(self, ctx: DigestContext) -> str:
        all_lines = ctx.stdout.text_lines
        templates, mined = mine_templates(all_lines)
        pem_lines = _private_key_line_ranges(ctx.stdout.text)

        ranked = sorted(templates.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))
        top = ranked[:10]
        shown_lines = sum(rec[0] for _, rec in top)
        # mine_templates puts every mined line in exactly ONE template, so all
        # N templates always cover all M lines. The line used to read
        # "templates: N cover C/M" with C being only the DISPLAYED ten's
        # count, implying M-C lines had no identified template when in fact
        # every line had one -- an understatement of the tool's own coverage,
        # and a number that could never be reconciled with the rows beneath.
        body = [
            f"templates: {fmt_int(len(templates))} cover "
            f"{fmt_int(len(mined))}/{fmt_int(len(mined))} lines"
            + (
                f" · top {len(top)} shown ({fmt_int(shown_lines)} lines)"
                if len(ranked) > len(top)
                else ""
            )
        ]
        for tpl, (count, first) in top:
            shown_tpl = display_template(all_lines[first - 1], EVIDENCE_LINE_CHARS,
                                         in_key_block=_in_ranges(first, pem_lines))
            line = f"  {fmt_int(count)}× L{first}: {shown_tpl}"
            if count > 1:
                # Point-attached retrieval affordance: a deterministic span
                # token minted exactly at the omission site (SPEC §6.4).
                sid = ctx.mint_span(ctx.stdout, "template", template=tpl)
                if sid:
                    line += f" · span {sid}"
            body.append(line)
        shown = len(top)

        rare = [(i, raw) for i, tpl, raw in mined if templates[tpl][0] <= 2]
        salient = [e for e in rare if _SALIENT_RE.search(e[1])]
        plain = [e for e in rare if not _SALIENT_RE.search(e[1])]
        ordered = salient + plain
        shown_exc = ordered[:12]
        if ordered:
            body.append("exceptional:")
            for i, raw in shown_exc:
                body.append(f"  L{i}: {clip_redacting(raw, 180)}")
            if len(ordered) > len(shown_exc):
                body.append(f"  … +{fmt_int(len(ordered) - len(shown_exc))} more exceptional lines")
            shown += len(shown_exc)

        spans = ctx.focus_spans()
        if spans:
            body.append("focus:")
            for stream, a, b, sample in spans:
                body.append(f"  {stream}:L{a}-L{b}: {sample}")
            shown += len(spans)

        rid = "run:PENDING"
        if shown_exc:
            first_i, first_raw = shown_exc[0]
            probe = next(
                (t for t in first_raw.split() if _mask_token(t) == t and len(t) >= 3),
                "ERROR",
            ).replace("'", "")
            suggestions = [
                f"ctx search {rid} '{probe}' --context 3",
                f"ctx get {rid}#stdout --lines {first_i}:{min(first_i + 3, ctx.stdout.lines)}",
            ]
        else:
            suggestions = [f"ctx search {rid} 'ERROR' 'WARN' --context 3"]

        emap = self.evidence_map(ctx)
        if emap is not None:
            map_lines, map_next, r = emap
            body.extend(map_lines)
            shown += r.windows
            suggestions = map_next + suggestions[1:]

        return "\n".join(
            ctx.header_lines() + body + self.coverage_lines(ctx, shown) + self.next_lines(ctx, suggestions)
        )
