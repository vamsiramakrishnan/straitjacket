"""pytest digest profile: counts, failure signatures, and evidence spans.

Elapsed times from pytest's summary line are deliberately dropped — digests
must not carry timing noise (SPEC §8).
"""

from __future__ import annotations

import re
from collections import Counter

from ctx.digest.base import DigestContext, Profile
from ctx.textutil import fmt_int

_SESSION_RE = re.compile(r"=+ test session starts =+")
_SUMMARY_RE = re.compile(
    r"=+ (?P<body>[^=]*?(?:passed|failed|error|skipped|xfailed|xpassed|deselected|warnings?)[^=]*?) =+"
)
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR) (?P<nodeid>\S+)(?: - (?P<msg>.*))?$")
_FAIL_HEADER_RE = re.compile(r"^_{3,} (?P<nodeid>.+?) _{3,}$")


class PytestProfile(Profile):
    version = "pytest/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        argv = ctx.manifest["argv"]
        joined = " ".join(argv)
        if "pytest" in joined or "py.test" in joined:
            return "argv invokes pytest"
        if _SESSION_RE.search(ctx.stdout.text[:4000]):
            return "stdout contains pytest session banner"
        return None

    def render(self, ctx: DigestContext) -> str:
        out_lines = ctx.stdout.text_lines
        counts: dict[str, int] = {}
        for m in _SUMMARY_RE.finditer(ctx.stdout.text):
            for n, kind in _COUNT_RE.findall(m.group("body")):
                kind = kind.rstrip("s") if kind in ("errors",) else kind
                counts[kind] = int(n)

        failed_tests: list[tuple[str, str]] = []
        for ln in out_lines:
            fm = _FAILED_LINE_RE.match(ln.strip())
            if fm:
                failed_tests.append((fm.group("nodeid"), fm.group("msg") or ""))

        # Locate failure detail blocks for line coordinates.
        fail_spans: list[tuple[str, int]] = []  # (nodeid, start line)
        for i, ln in enumerate(out_lines, start=1):
            hm = _FAIL_HEADER_RE.match(ln.strip())
            if hm:
                fail_spans.append((hm.group("nodeid").strip(), i))

        total = sum(v for k, v in counts.items() if k in ("passed", "failed", "skipped", "error", "xfailed", "xpassed"))
        summary = ["summary:"]
        if counts:
            parts = [f"passed {fmt_int(counts.get('passed', 0))}"]
            for key in ("failed", "error", "skipped", "xfailed", "xpassed"):
                if counts.get(key):
                    parts.append(f"{key} {fmt_int(counts[key])}")
            summary.append(f"  tests: {fmt_int(total)} · " + " · ".join(parts))
        else:
            summary.append("  tests: summary line not found (session may have crashed)")

        shown = 0
        if fail_spans:
            nodeid, start = fail_spans[0]
            end = min(start + 12, ctx.stdout.lines)
            sid = ctx.mint_span(ctx.stdout, "region", a=start, b=end)
            tag = f" · span {sid}" if sid else ""
            summary.append(f"  first failure stdout:L{start}-L{end}: {nodeid}{tag}")
            shown += 1
            if len(fail_spans) > 1:
                nodeid, start = fail_spans[-1]
                end = min(start + 12, ctx.stdout.lines)
                sid = ctx.mint_span(ctx.stdout, "region", a=start, b=end)
                tag = f" · span {sid}" if sid else ""
                summary.append(f"  terminal failure stdout:L{start}-L{end}: {nodeid}{tag}")
                shown += 1
        elif failed_tests:
            summary.append(f"  first failure: {failed_tests[0][0]} - {failed_tests[0][1][:120]}")
            shown += 1

        # Modal failure signature: exception class frequencies.
        sig = Counter()
        for ln in out_lines:
            m = re.match(r"^E?\s*([A-Z][A-Za-z0-9_.]*(?:Error|Exception|Timeout|Failure))\b", ln.strip())
            if m:
                sig[m.group(1)] += 1
        if sig:
            summary.append("  failure signatures:")
            for name, n in sorted(sig.most_common(4)):
                summary.append(f"    {name}  {n}")

        if ctx.stderr.lines:
            err_first = ctx.stderr.text_lines[0].strip()[:140] if ctx.stderr.text_lines else ""
            summary.append(f"  stderr head stderr:L1: {err_first}")
            shown += 1

        for stream, a, b, sample in ctx.focus_spans(max_spans=2):
            summary.append(f"  focus {stream}:L{a}-L{b}: {sample}")
            shown += 1

        rid = "run:PENDING"
        suggestions = []
        if failed_tests:
            first_word = re.sub(r"[^\w.-]", " ", failed_tests[0][1]).split()
            probe = first_word[0] if first_word else failed_tests[0][0].split("::")[-1]
            suggestions.append(f"ctx search {rid} '{probe}' 'FAILED' --context 3")
        if fail_spans:
            start = fail_spans[0][1]
            suggestions.append(f"ctx get {rid}#stdout --lines {start}:{min(start + 12, ctx.stdout.lines)}")
        if not suggestions:
            suggestions.append(f"ctx search {rid} 'failed' 'error' --context 3")

        return "\n".join(
            ctx.header_lines() + summary + self.coverage_lines(ctx, shown or 1) + self.next_lines(ctx, suggestions)
        )
