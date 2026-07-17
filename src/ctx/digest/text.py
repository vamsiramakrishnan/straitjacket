"""Generic text profile — the universal deterministic fallback."""

from __future__ import annotations

import re

from ctx.digest.base import DigestContext, Profile

# Deterministic salience: error-ish lines first, then head/tail anchors.
_ERROR_RE = re.compile(
    r"\b(error|failed|failure|exception|traceback|fatal|panic|denied|refused|timeout|timed out|cannot|unable)\b",
    re.IGNORECASE,
)


class TextProfile(Profile):
    version = "text/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        return "generic text fallback"

    def render(self, ctx: DigestContext) -> str:
        lines = ctx.header_lines()

        inline = self.inline_body(ctx)
        if inline is not None:
            return "\n".join(lines + inline)

        shown = 0
        summary: list[str] = ["summary:"]

        binary = (
            ctx.stdout.media_type.startswith("application/octet-stream")
            or ctx.stderr.media_type.startswith("application/octet-stream")
        )
        if binary:
            summary.append("  binary stream · raw bytes preserved in artifact store")

        spans = ctx.focus_spans()
        if spans:
            for stream, a, b, sample in spans:
                summary.append(f"  focus {stream}:L{a}-L{b}: {sample}")
                shown += 1
        else:
            # First and last error lines across stderr then stdout.
            for view in (ctx.stderr, ctx.stdout):
                hits = [
                    (i, ln.strip())
                    for i, ln in enumerate(view.text_lines, start=1)
                    if _ERROR_RE.search(ln)
                ]
                if hits:
                    i, ln = hits[0]
                    summary.append(f"  first signal {view.name}:L{i}: {ln[:160]}")
                    shown += 1
                    if len(hits) > 1:
                        j, lj = hits[-1]
                        summary.append(f"  terminal signal {view.name}:L{j}: {lj[:160]}")
                        shown += 1
                    break
        if shown == 0 and (ctx.stdout.lines or ctx.stderr.lines):
            view = ctx.stdout if ctx.stdout.lines else ctx.stderr
            first = view.text_lines[0].strip()[:160] if view.text_lines else ""
            summary.append(f"  head {view.name}:L1: {first}")
            shown += 1
        if len(summary) == 1:
            summary.append("  no output")

        rid = "run:PENDING"
        suggestions = [
            f"ctx search {rid} '<pattern>' --context 3",
            f"ctx get {rid}#stdout --lines 1:{min(ctx.stdout.lines, 40) or 1}",
        ]
        return "\n".join(
            lines + summary + self.coverage_lines(ctx, shown) + self.next_lines(ctx, suggestions)
        )
