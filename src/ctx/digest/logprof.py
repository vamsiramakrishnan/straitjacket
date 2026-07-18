"""Drain-style log template mining profile (SPEC §9: structured logs).

Volatile tokens (numbers, hex, UUIDs, timestamps, durations, IPs,
digit-bearing identifiers) are masked to ``<*>``; the masked token sequence
is the template key. Rare templates (≤2 occurrences) are surfaced verbatim
as exceptional evidence, with an explicit count when capped — no silent
omission (SPEC §8).
"""

from __future__ import annotations

import re

from ctx.digest.base import DigestContext, Profile
from ctx.textutil import fmt_int

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

# Token-level memoization: real logs repeat the same volatile tokens
# (pids, ports, level words) across thousands of lines. The mask is a pure
# function of the token, so a bounded cache changes nothing but speed.
_MASK_CACHE: dict[str, str] = {}
_MASK_CACHE_MAX = 65536


def _mask_token(tok: str) -> str:
    # Digit-free tokens are stable words; masking them would merge unrelated
    # templates (all-letter hex like "facade" stays literal by design).
    if _HAS_DIGIT_RE.search(tok) is None:
        return tok
    hit = _MASK_CACHE.get(tok)
    if hit is not None:
        return hit
    masked = tok
    for rx in _MASK_RES:
        masked = rx.sub("<*>", masked)
    masked = _COLLAPSE_RE.sub("<*>", masked)
    if len(_MASK_CACHE) < _MASK_CACHE_MAX:
        _MASK_CACHE[tok] = masked
    return masked


def mask_line(raw: str) -> str:
    """Public template key for a line — shared by digesting and span zoom."""
    return " ".join(_mask_token(t) for t in raw.split())


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
        templates, mined = mine_templates(ctx.stdout.text_lines)

        ranked = sorted(templates.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))
        top = ranked[:10]
        covered = sum(rec[0] for _, rec in top)
        body = [
            f"templates: {fmt_int(len(templates))} cover "
            f"{fmt_int(covered)}/{fmt_int(len(mined))} lines"
        ]
        for tpl, (count, first) in top:
            line = f"  {fmt_int(count)}× L{first}: {tpl[:160]}"
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
                body.append(f"  L{i}: {raw[:180]}")
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

        return "\n".join(
            ctx.header_lines() + body + self.coverage_lines(ctx, shown) + self.next_lines(ctx, suggestions)
        )
