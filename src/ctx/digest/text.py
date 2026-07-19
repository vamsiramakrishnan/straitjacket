"""Generic text profile — the universal deterministic fallback."""

from __future__ import annotations

import re

from ctx.digest.base import DigestContext, Profile, StreamView
from ctx.textutil import fmt_int

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
            # Scaffold-slim emission (measured: full headers + indentation
            # inflated small runs to 0.5-0.9x of raw — the digest cost MORE
            # than the output). Provenance keeps command + exit; the content
            # rides unindented. rtk lesson inverted: our tax was overhead,
            # not under-compression.
            r = ctx.manifest["result"]
            status = (
                f"exit {r['exitCode']}" if r["exitCode"] is not None else f"signal {r['signal']}"
            )
            if r["timedOut"]:
                status += " · timed out"
            slim = [
                f"command: {ctx.command_line()}",
                f"{status} · output (complete):",
            ]
            for view in (ctx.stdout, ctx.stderr):
                if view.bytes:
                    if view is ctx.stderr and ctx.stdout.bytes:
                        slim.append("--- stderr ---")
                    slim.extend(view.text_lines)
            return "\n".join(slim)

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
        rid = "run:PENDING"
        if shown == 0 and (ctx.stdout.lines or ctx.stderr.lines):
            # HEAD/TAIL evidence window (eval-collapse-2026-07-18 S-C): CLIs
            # put conclusions at the END (test summaries, exit reports,
            # "SLOWEST: ..."), so the old single "head :L1" line omitted the
            # very evidence the run existed to produce. Show the first H and
            # last T lines; the middle is declared-omitted with an address.
            view = ctx.stdout if ctx.stdout.lines else ctx.stderr
            budgets = ctx.ws.config.budgets
            head_n = max(1, budgets.digest_head_lines)
            # The true tail is unknowable past the parse cap — head only then.
            tail_n = max(0, budgets.digest_tail_lines) if view.parsed_fully else 0
            budget_bytes = budgets.digest_tokens * 4

            def assemble(h: int, t: int) -> str:
                window, w_shown, mid = self._window_lines(ctx, view, h, t)
                sugg = [f"ctx search {rid} '<pattern>' --context 3"]
                if mid is not None:
                    sugg.append(f"ctx get {rid}#{view.name} --lines {mid[0]}:{mid[1]}")
                else:
                    sugg.append(f"ctx get {rid}#stdout --lines 1:{min(ctx.stdout.lines, 40) or 1}")
                return "\n".join(
                    lines
                    + summary
                    + window
                    + self.coverage_lines(ctx, w_shown)
                    + self.next_lines(ctx, sugg)
                )

            # Budget fitting, deterministic: shrink the tail first, then the
            # head (never below one line) until the digest fits.
            text = assemble(head_n, tail_n)
            while len(text.encode("utf-8")) > budget_bytes and tail_n > 0:
                tail_n -= 1
                text = assemble(head_n, tail_n)
            while len(text.encode("utf-8")) > budget_bytes and head_n > 1:
                head_n -= 1
                text = assemble(head_n, tail_n)
            return text
        if len(summary) == 1:
            summary.append("  no output")

        suggestions = [
            f"ctx search {rid} '<pattern>' --context 3",
            f"ctx get {rid}#stdout --lines 1:{min(ctx.stdout.lines, 40) or 1}",
        ]
        return "\n".join(
            lines + summary + self.coverage_lines(ctx, shown) + self.next_lines(ctx, suggestions)
        )

    def _window_lines(
        self, ctx: DigestContext, view: StreamView, head_n: int, tail_n: int
    ) -> tuple[list[str], int, tuple[int, int] | None]:
        """HEAD/TAIL window over one stream: first ``head_n`` and last
        ``tail_n`` lines, each labeled with its real 1-indexed line number and
        clipped exactly like the historical head line. Returns
        (summary_lines, shown_line_count, omitted_middle_range_or_None)."""
        text_lines = view.text_lines
        n = len(text_lines)
        if n == 0:
            # Binary (or otherwise textless) stream: keep the historical
            # placeholder shape rather than inventing coordinates.
            return [f"  head {view.name}:L1: "], 1, None
        # Past the parse cap the decoded view ends before the stream does;
        # the omission then runs to the stream's true last line.
        end_total = n if view.parsed_fully else max(view.lines, n)
        h = min(head_n, n)
        t_start = max(h + 1, n - tail_n + 1) if tail_n > 0 else end_total + 1
        out = [
            f"  head {view.name}:L{i}: {text_lines[i - 1].strip()[:160]}"
            for i in range(1, h + 1)
        ]
        shown = h
        mid: tuple[int, int] | None = None
        if t_start > h + 1:
            a, b = h + 1, t_start - 1
            mid = (a, b)
            marker = f"  … omitted {view.name}:L{a}-L{b} ({fmt_int(b - a + 1)} lines)"
            sid = ctx.mint_span(view, "region", a=a, b=b)
            if sid:
                marker += f" · span {sid}"
            out.append(marker)
        for i in range(t_start, n + 1):
            out.append(f"  tail {view.name}:L{i}: {text_lines[i - 1].strip()[:160]}")
            shown += 1
        return out, shown, mid
