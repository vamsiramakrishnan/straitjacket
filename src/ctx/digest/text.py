"""Generic text profile — the universal deterministic fallback."""

from __future__ import annotations

import hashlib
import re

from ctx.digest.base import DigestContext, Profile, StreamView
from ctx.textutil import EVIDENCE_LINE_CHARS, fmt_int

# Deterministic salience: error-ish lines first, then head/tail anchors.
_ERROR_RE = re.compile(
    r"\b(error|failed|failure|exception|traceback|fatal|panic|denied|refused|timeout|timed out|cannot|unable)\b",
    re.IGNORECASE,
)

# ---- layered signal ranking -------------------------------------------------
# Translated from the reviewed AlphaEvolve evidence-selection winner
# (evals/alphaevolve/2026-08-24-evidence-selection-holdout.md): one blunt
# keyword regex both missed real signals ("HTTP/1.1 502 Bad Gateway" carries
# no keyword) and fired on benign traps ("no errors detected"). Lines are
# ranked in tiers; only positive tiers may become a digest signal line.
_BENIGN_RE = re.compile(
    r"\bno (?:errors?|failures?|warnings?)\b|\b0 (?:errors?|failed|failures?|warnings?)\b"
    r'|\bzero (?:errors?|failures?)\b|["\'][^"\']*?\b0 errors?\b[^"\']*?["\']',
    re.IGNORECASE,
)
_COORDINATE_RE = re.compile(
    r'\bfile "[^"]+", line \d+'
    r"|\b(?:[a-zA-Z]:[\\/])?[\w.\-/\\]+\.\w+:\d+(?::\d+)?\b"
    r"|\b--> \S+:\d+"
    r"|\bat \S+\(\w+\.java:\d+\)"
    r"|\b\S+\.(?:go|py|rs|ts|js|java):\d+",
)
_HTTP_STATUS_RE = re.compile(r"HTTP/[12]\.\d+\s+[45]\d\d|\bstatus(?:_code)?\s*[:=]\s*[45]\d\d\b")
_EXIT_CODE_RE = re.compile(
    r"\b(?:exit(?:ed)?(?: with)?(?: code| status)?\s*[:=]?\s*[1-9]\d*|code\s+[1-9]\d*\s*\((?:OOMKilled|Error)\))\b",
    re.IGNORECASE,
)
_REAL_FAILURES_RE = re.compile(
    r"\b(?:failures?|errors?|failed|failing)\s*[:=]?\s*[1-9]\d*\b"
    r"|\b[1-9]\d*\s+(?:failed|failures?|errors?)\b"
    r"|--- FAIL: |\bFAIL\b|\bgyp ERR!|\bpsql:.*ERROR|\bELIFECYCLE\b|\bBack-off restarting failed\b"
    r"|\bOOMKilled\b|\bconnection refused\b",
)
_STRONG_ERR_RE = re.compile(
    r"\b(FATAL|CRITICAL|Traceback|Exception|AssertionError|PERMISSION_DENIED|PERMISSION denied)\b"
    r"|^Error: |\bERR! ",
)
_WEAK_ERR_RE = _ERROR_RE
_QUOTA_RE = re.compile(r"\b(quota exceeded|exceed quota|will exceed quota)\b", re.IGNORECASE)


def _signal_rank(line: str) -> int:
    """Tier of evidence salience for one output line.

    -1 benign trap · 0 no signal · 1 weak keyword · 2 coordinate-bearing ·
    3 decisive failure evidence. Higher wins; earliest line breaks ties.
    """
    if _BENIGN_RE.search(line):
        return -1
    rank = 0
    if _WEAK_ERR_RE.search(line):
        rank = 1
    if _COORDINATE_RE.search(line):
        rank = max(rank, 2)
    if (
        _REAL_FAILURES_RE.search(line)
        or _HTTP_STATUS_RE.search(line)
        or _EXIT_CODE_RE.search(line)
        or _STRONG_ERR_RE.search(line)
        or _QUOTA_RE.search(line)
    ):
        rank = 3
    return rank


def _focused_lines(ctx: DigestContext) -> tuple[list[tuple[str, int, str]], int, int]:
    """Select at most four exact lines; two slots protect non-query signals.

    Query coverage outranks repetition. Stream/line order breaks ties. This
    policy only sees parsed text; coverage reporting owns parse-cap disclosure.
    """
    terms = tuple(sorted(set(ctx.focus_terms)))
    rows = []
    for view in (ctx.stderr, ctx.stdout):
        for number, line in enumerate(view.text_lines, 1):
            relevance = sum(term in line.lower() for term in terms)
            rows.append((view.name, number, line, relevance, _signal_rank(line)))
    signals = [row for row in rows if row[4] > 0]
    protected = sorted(signals, key=lambda row: -row[4])[:1]
    if signals and signals[-1] not in protected:
        protected.append(signals[-1])
    matches = [row for row in rows if row[3] > 0]
    ranked = sorted(matches, key=lambda row: (-row[3], -row[4]))
    chosen = list(protected)
    for row in ranked:
        if len(chosen) >= 4:
            break
        if row not in chosen:
            chosen.append(row)
    return [(row[0], row[1], row[2]) for row in chosen], len(rows), len(matches)


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
            # Provenance must pay for itself. The command echo is the model's
            # own input coming back — free when it annotates a large capture,
            # pure overhead when the output is a line or two. Measured before
            # this rule: an 11-byte output rendered as a 140-byte digest
            # (12.7x), which is why replaying our own sessions showed the
            # harness *costing* tokens on short calls. Dropped when it is not
            # smaller than the content it describes; the run handle on the
            # header line still addresses the capture either way.
            command_line = f"command: {ctx.command_line()}"
            content_bytes = sum(
                v.bytes for v in (ctx.stdout, ctx.stderr) if v.bytes
            )
            slim = []
            if len(command_line.encode("utf-8")) < content_bytes:
                slim.append(command_line)
            slim.append(f"{status} · output (complete):")
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

        spans = []
        if ctx.focus_terms:
            # Focus is an opt-in ranking hint, never permission to hide failures.
            ctx.meta_profile_version = "text/focus-v2"
            selected, scanned, matches = _focused_lines(ctx)
            if selected:
                query_id = hashlib.sha256(
                    "\0".join(sorted(set(ctx.focus_terms))).encode("utf-8")
                ).hexdigest()[:12]
                summary.append(
                    f"  selection: focus/v2 · query={query_id} · "
                    f"scanned={scanned} · matches={matches} · selected={len(selected)}"
                )
                # Preserve local structure when all selected evidence fits; retain
                # addressed line fallback for giant paragraphs or tiny budgets.
                from ctx.context_compression import compress_context
                streams = [view for view in (ctx.stderr, ctx.stdout)
                           if any(row[0] == view.name for row in selected)]
                block_budget = min(2048, ctx.ws.config.budgets.digest_tokens * 2)
                expanded = []
                for view in streams:
                    required = tuple(number for name, number, _ in selected if name == view.name)
                    result = compress_context(view.text, " ".join(ctx.focus_terms),
                                              block_budget // len(streams), required)
                    if result["status"] != "selected":
                        expanded = []
                        break
                    expanded.append((view, result))
                if expanded:
                    ctx.meta_profile_version = "text/focus-v3"
                    summary.append("  structure: complete blocks with heading dependencies")
                    suggestions = []
                    shown_lines = 0
                    for view, result in expanded:
                        for span in result["kept"]:
                            a, b = span["start"], span["end"]
                            summary.append(f"  evidence {view.name}:L{a}-L{b}:")
                            summary.extend("    " + line for line in view.text_lines[a - 1:b])
                            shown_lines += b - a + 1
                            suggestions.append(f"ctx get run:PENDING#{view.name} --lines {a}:{b}")
                    return "\n".join(lines + summary + self.coverage_lines(ctx, shown_lines)
                                     + self.next_lines(ctx, suggestions))
                for stream, number, sample in selected:
                    summary.append(f"  evidence {stream}:L{number}: {sample[:EVIDENCE_LINE_CHARS]}")
                suggestions = [
                    f"ctx get run:PENDING#{stream} --lines {max(1, number - 1)}:{number}"
                    for stream, number, _ in selected
                ]
                return "\n".join(
                    lines + summary + self.coverage_lines(ctx, len(selected))
                    + self.next_lines(ctx, suggestions)
                )
            spans = []
        if spans:
            for stream, a, b, sample in spans:
                summary.append(f"  focus {stream}:L{a}-L{b}: {sample}")
                shown += 1
        else:
            # First and last evidence lines across stderr then stdout, ranked
            # by the layered signal model: benign traps never qualify, and a
            # decisive tier-3 line outranks an earlier weak-keyword line.
            for view in (ctx.stderr, ctx.stdout):
                hits = [
                    (i, ln.strip(), _signal_rank(ln))
                    for i, ln in enumerate(view.text_lines, start=1)
                ]
                hits = [h for h in hits if h[2] > 0]
                if hits:
                    i, ln, rank = max(hits, key=lambda h: (h[2], -h[0]))
                    summary.append(f"  first signal {view.name}:L{i}: {ln[:EVIDENCE_LINE_CHARS]}")
                    shown += 1
                    j, lj, _ = hits[-1]
                    if j != i:
                        summary.append(f"  terminal signal {view.name}:L{j}: {lj[:EVIDENCE_LINE_CHARS]}")
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
            f"  head {view.name}:L{i}: {text_lines[i - 1].strip()[:EVIDENCE_LINE_CHARS]}"
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
            out.append(f"  tail {view.name}:L{i}: {text_lines[i - 1].strip()[:EVIDENCE_LINE_CHARS]}")
            shown += 1
        return out, shown, mid
