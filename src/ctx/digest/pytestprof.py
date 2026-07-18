"""pytest digest profile: counts, failing-test census, and evidence spans.

Elapsed times from pytest's summary line are deliberately dropped — digests
must not carry timing noise (SPEC §8).

The failing-test CENSUS (debt 74db82e027, evals/spec3-haiku-2026-07-18.md):
a digest that names one failure where the task needs all of them converts
directly into suite re-runs — the measured spec3 loop (haiku re-ran pytest
8x with slicers and hit the 32-turn cap). Same budget, structured (the
rtk-corpus "structure, not compression" lesson lint/v1 already applies):
ONE line per failing test — node id, stdout coordinates, region span over
its traceback block — ranked ABOVE the inline first-failure detail. A tight
budget drops the inline detail before census rows; a census overflow ends
in a declared "… +N more failures" with a continuation handle, never a
silent cut.

Dense mode (``ctx.dense``, plumbed by the reflex layer when starvation is
detected): each census row additionally carries its short assertion/error
line inline, so a starved model gets the full evidence without a retrieval
hop. Reading is via getattr so rendering works with or without the flag.
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
# pytest -q (and piped/quiet output) ends with a BARE summary line — no `=`
# banner: "8 failed, 1 passed in 0.05s". The spec3 re-run loop hit exactly
# this shape ("summary line not found" on every `| head`-sliced re-run).
_BARE_SUMMARY_RE = re.compile(r"^(?P<body>\d+ [a-z]+(?:, \d+ [a-z]+)*) in [\d.]+s\b.*$")
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR) (?P<nodeid>\S+)(?: - (?P<msg>.*))?$")
# Verbose-mode progress rows ("path::test FAILED [ 22%]") — the census
# fallback when output was truncated before the short-summary section.
_VERBOSE_PROG_RE = re.compile(r"^(?P<nodeid>\S+::\S+) (?:FAILED|ERROR)\b")
_FAIL_HEADER_RE = re.compile(r"^_{3,} (?P<nodeid>.+?) _{3,}$")
_SHORT_SUMMARY_BANNER_RE = re.compile(r"=+ short test summary info =+")
_FAILURES_BANNER_RE = re.compile(r"^=+ (?:FAILURES|ERRORS) =+", re.MULTILINE)


def _is_banner(line: str) -> bool:
    return line.startswith("=") and line.rstrip().endswith("=")


def _short_nodeid(nid: str) -> str:
    """Shorten the file-path part to its last two components (lint/v1 house
    style); the ::-qualified test name always rides in full."""
    if "::" not in nid:
        return nid
    path, rest = nid.split("::", 1)
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 2:
        path = "/".join(parts[-2:])
    return path + "::" + rest


class PytestProfile(Profile):
    version = "pytest/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        argv = ctx.manifest["argv"]
        joined = " ".join(argv)
        if "pytest" in joined or "py.test" in joined:
            return "argv invokes pytest"
        text = ctx.stdout.text
        if _SESSION_RE.search(text[:4000]):
            return "stdout contains pytest session banner"
        # Quiet (-q) and pipe-truncated pytest output has no session banner;
        # the FAILURES / short-summary sections are still unambiguous.
        if _SHORT_SUMMARY_BANNER_RE.search(text):
            return "stdout contains pytest short-summary section"
        if _FAILURES_BANNER_RE.search(text):
            return "stdout contains pytest FAILURES section"
        return None

    # ------------------------------------------------------------- parsing
    def _parse_counts(self, text: str, out_lines: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in _SUMMARY_RE.finditer(text):
            for n, kind in _COUNT_RE.findall(m.group("body")):
                kind = kind.rstrip("s") if kind in ("errors",) else kind
                counts[kind] = int(n)
        if not counts:
            # Bare -q summary: scan the terminal lines only (deterministic;
            # the summary is always last when present at all).
            for ln in reversed(out_lines[-40:]):
                m = _BARE_SUMMARY_RE.match(ln.strip())
                if not m:
                    continue
                found = _COUNT_RE.findall(m.group("body"))
                if not found:
                    continue
                for n, kind in found:
                    kind = kind.rstrip("s") if kind in ("errors",) else kind
                    counts[kind] = int(n)
                break
        return counts

    def render(self, ctx: DigestContext) -> str:
        out_lines = ctx.stdout.text_lines
        counts = self._parse_counts(ctx.stdout.text, out_lines)

        # Failing node ids: short-summary FAILED/ERROR lines, else (truncated
        # output) verbose progress rows. (line_no, nodeid, msg).
        failed_tests: list[tuple[int, str, str]] = []
        for i, ln in enumerate(out_lines, start=1):
            fm = _FAILED_LINE_RE.match(ln.strip())
            if fm:
                failed_tests.append((i, fm.group("nodeid"), fm.group("msg") or ""))
        if not failed_tests:
            for i, ln in enumerate(out_lines, start=1):
                vm = _VERBOSE_PROG_RE.match(ln.strip())
                if vm:
                    failed_tests.append((i, vm.group("nodeid"), ""))

        # Traceback blocks: each `___ name ___` header owns the lines up to
        # the next header or `=` banner (trailing blanks trimmed) — real
        # block extents, so census spans resolve to whole tracebacks.
        headers: list[tuple[str, int]] = []
        boundaries: list[int] = []
        for i, ln in enumerate(out_lines, start=1):
            hm = _FAIL_HEADER_RE.match(ln.strip())
            if hm:
                headers.append((hm.group("nodeid").strip(), i))
                boundaries.append(i)
            elif _is_banner(ln):
                boundaries.append(i)
        blocks: list[tuple[str, int, int]] = []  # (header name, start, end)
        n_lines = len(out_lines)
        for name, start in headers:
            nxt = [b for b in boundaries if b > start]
            end = (min(nxt) - 1) if nxt else n_lines
            while end > start and not out_lines[end - 1].strip():
                end -= 1
            blocks.append((name, start, end))

        # Census entries: blocks matched to full node ids by name (headers
        # carry only "test_x" / "TestC.test_x"); leftover summary-only rows
        # (e.g. --tb=line/--tb=no output has no blocks) keep their own
        # FAILED-line coordinate. dict: id, a, b (None = no block), msg.
        used: set[int] = set()

        def _match(name: str) -> tuple[str, str] | None:
            suffixes = ("::" + name, "::" + name.replace(".", "::"))
            for idx, (_, nid, msg) in enumerate(failed_tests):
                if idx in used:
                    continue
                if nid == name or nid.endswith(suffixes[0]) or nid.endswith(suffixes[1]):
                    used.add(idx)
                    return nid, msg
            return None

        entries: list[dict] = []
        for name, start, end in blocks:
            hit = _match(name)
            entries.append(
                {
                    "id": hit[0] if hit else name,
                    "a": start,
                    "b": end,
                    "msg": hit[1] if hit else "",
                }
            )
        for idx, (line_no, nid, msg) in enumerate(failed_tests):
            if idx not in used:
                entries.append({"id": nid, "a": line_no, "b": None, "msg": msg})

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
        budgets = ctx.ws.config.budgets
        dense = bool(getattr(ctx, "dense", False))

        # ------------------------------------------------- failing-test census
        # Priority over the inline detail: census rows render first, so the
        # downstream bounded() emitter (which cuts from the bottom) and this
        # explicit cap both starve the detail before the census.
        if entries:
            census_budget = int(budgets.result_tokens * budgets.failure_budget_factor) * 2  # bytes
            summary.append("  failing tests (census):")
            spent = 0
            shown_entries = 0
            for ent in entries:
                row = "    " + _short_nodeid(ent["id"])[:160]
                if ent["b"] is not None:
                    sid = ctx.mint_span(ctx.stdout, "region", a=ent["a"], b=ent["b"])
                    row += f"  stdout:L{ent['a']}-L{ent['b']}"
                    if sid:
                        row += f" · span {sid}"
                else:
                    row += f"  stdout:L{ent['a']}"
                chunk = [row]
                if dense:
                    err = self._error_line(ent, out_lines)
                    if err:
                        chunk.append("      " + err[:160])
                cost = sum(len(c.encode("utf-8")) + 1 for c in chunk)
                if shown_entries and spent + cost > census_budget:
                    break
                summary.extend(chunk)
                spent += cost
                shown_entries += 1
            omitted_entries = entries[shown_entries:]
            if omitted_entries:
                rest_blocks = [e for e in omitted_entries if e["b"] is not None]
                if rest_blocks:
                    a = min(e["a"] for e in rest_blocks)
                    b = max(e["b"] for e in rest_blocks)
                    sid = ctx.mint_span(ctx.stdout, "region", a=a, b=b)
                    tail = f"stdout:L{a}-L{b}" + (f" · span {sid}" if sid else "")
                else:
                    tail = "ctx search run:PENDING 'FAILED' --context 0"
                summary.append(f"    … +{len(omitted_entries)} more failures · {tail}")
            expected = counts.get("failed", 0) + counts.get("error", 0)
            if expected > len(entries):
                summary.append(
                    f"    … census: {len(entries)} of {expected} failure blocks parsed (output truncated?)"
                )
            shown += shown_entries

        # ------------------------------------------- first-failure detail
        if blocks:
            name, start, end = blocks[0]
            sid = ctx.mint_span(ctx.stdout, "region", a=start, b=end)
            tag = f" · span {sid}" if sid else ""
            summary.append(f"  first failure stdout:L{start}-L{end}: {name}{tag}")
            shown += 1
            # Anticipatory inlining (mechanism E): the first failure region
            # is the one slice the model asks for next in almost every
            # failing-test loop, and a retrieval hop costs a full turn of
            # ttfb + suffix cache write. Inline it when the budget allows —
            # deterministic: pure function of bytes + committed budget.
            if budgets.result_tokens >= 600:
                for raw in out_lines[start - 1 : min(end, start + 12)]:
                    # Stop at the next section: separator banners carry
                    # elapsed-time noise and belong to other regions.
                    if _is_banner(raw):
                        break
                    summary.append(f"    | {raw[:160]}")
        elif failed_tests:
            summary.append(f"  first failure: {failed_tests[0][1]} - {failed_tests[0][2][:120]}")
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
            first_word = re.sub(r"[^\w.-]", " ", failed_tests[0][2]).split()
            probe = first_word[0] if first_word else failed_tests[0][1].split("::")[-1]
            suggestions.append(f"ctx search {rid} '{probe}' 'FAILED' --context 3")
        if blocks:
            start, end = blocks[0][1], blocks[0][2]
            suggestions.append(f"ctx get {rid}#stdout --lines {start}:{end}")
        if not suggestions:
            suggestions.append(f"ctx search {rid} 'failed' 'error' --context 3")

        return "\n".join(
            ctx.header_lines() + summary + self.coverage_lines(ctx, shown or 1) + self.next_lines(ctx, suggestions)
        )

    # ------------------------------------------------------------ dense mode
    def _error_line(self, ent: dict, out_lines: list[str]) -> str:
        """The one line that names an entry's failure: the block's first
        `E ...` assertion/exception line, else the short-summary message,
        else the block's last non-blank line (the `file:line: Error` locus)."""
        if ent["b"] is not None:
            block = out_lines[ent["a"] - 1 : ent["b"]]
            for raw in block:
                if raw.startswith("E "):
                    return raw.strip()
            if ent["msg"]:
                return ent["msg"].strip()
            for raw in reversed(block):
                if raw.strip():
                    return raw.strip()
        return ent["msg"].strip()
