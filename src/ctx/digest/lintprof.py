"""Diagnostics digest profile: linter/compiler output, structured not scrolled.

Measured motivation (evals/rtk-corpus-2026-07-18.md): the text fallback
already compresses eslint/ruff 92x — but its digest of 526 violations names
none of them. Same budget, structured: totals by severity, the dominant
rules, the worst files, and a span into the first diagnostic region. That
is what turns "run the linter again with grep" round-trips into decisions.

Covers, via three shapes (all real-corpus tested):
  file:line:col  — ruff, go, mypy, golangci, clang-style
  eslint stylish — indented "line:col  severity  message  rule" under a
                   bare file-path line
  tsc            — file(line,col): severity TSxxxx: message
  rustc/cargo    — severity[Exxxx]: message / --> file:line:col
"""

from __future__ import annotations

import re
from collections import Counter

from ctx.digest.base import DigestContext, Profile
from ctx.textutil import fmt_int

_COLON_RE = re.compile(
    r"^\s*(?P<file>[^\s:][^:]*\.\w{1,12}):(?P<line>\d+):(?:\d+:?)?\s*"
    r"(?P<sev>error|warning|note|[EWF]\d{2,4}|[A-Z]{1,3}\d{3,4})\b",
    re.IGNORECASE,
)
_ESLINT_LINE_RE = re.compile(
    r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+(?P<msg>.*?)\s\s+(?P<rule>[\w@./-]+)$"
)
_ESLINT_FILE_RE = re.compile(r"^\S*[/\\][^\s:]+\.\w{1,12}$|^[^\s:]+\.\w{1,12}$")
_TSC_RE = re.compile(
    r"^(?P<file>[^\s(]+)\((?P<line>\d+),\d+\):\s+(?P<sev>error|warning)\s+(?P<rule>TS\d+)"
)
_RUST_HEAD_RE = re.compile(r"^(?P<sev>error|warning)(\[(?P<rule>[EW]\d{3,4})\])?[:[]")
# ruff >= 0.9 emits rustc-style too: "F401 [*] `os` imported but unused"
_RULE_HEAD_RE = re.compile(r"^(?P<rule>[A-Z]{1,4}\d{2,4})\b(\s+\[\*\])?\s+\S")
_RUST_LOC_RE = re.compile(r"^\s*-->\s+(?P<file>\S+?):(?P<line>\d+):\d+")

_MIN_DIAGS = 8  # fewer than this and the text profile's signals suffice


def _parse(lines: list[str]):
    """-> list of (lineno_in_output, file, sev, rule). Deterministic."""
    diags: list[tuple[int, str, str, str]] = []
    current_file = ""
    pending_rust: tuple[int, str, str] | None = None
    for i, ln in enumerate(lines, start=1):
        m = _TSC_RE.match(ln)
        if m:
            diags.append((i, m.group("file"), m.group("sev").lower(), m.group("rule")))
            continue
        m = _COLON_RE.match(ln)
        if m:
            sev_raw = m.group("sev")
            sev = (
                "error"
                if sev_raw.lower().startswith(("e", "f")) or sev_raw.lower() == "error"
                else "warning" if sev_raw.lower().startswith("w") else sev_raw.lower()
            )
            rule = sev_raw if re.match(r"^[A-Z]", sev_raw) and any(c.isdigit() for c in sev_raw) else ""
            diags.append((i, m.group("file"), sev, rule))
            continue
        m = _ESLINT_LINE_RE.match(ln)
        if m and current_file:
            diags.append((i, current_file, m.group("sev"), m.group("rule")))
            continue
        m = _RUST_HEAD_RE.match(ln)
        if m and not ln.startswith(("error: aborting", "warning: unused")):
            pending_rust = (i, m.group("sev"), m.group("rule") or "")
            continue
        m = _RULE_HEAD_RE.match(ln)
        if m:
            rule = m.group("rule")
            sev = "warning" if rule.startswith("W") else "error"
            pending_rust = (i, sev, rule)
            continue
        m = _RUST_LOC_RE.match(ln)
        if m and pending_rust:
            diags.append((pending_rust[0], m.group("file"), pending_rust[1], pending_rust[2]))
            pending_rust = None
            continue
        if _ESLINT_FILE_RE.match(ln.strip()) and not ln.startswith(" "):
            current_file = ln.strip()
    return diags


class LintProfile(Profile):
    version = "lint/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        text_lines = (ctx.stdout.text + "\n" + ctx.stderr.text).splitlines()
        diags = _parse(text_lines[:4000])
        if len(diags) < _MIN_DIAGS:
            return None
        self._diags = diags
        self._lines = text_lines
        return f"{len(diags)} diagnostic lines in linter/compiler shape"

    def render(self, ctx: DigestContext) -> str:
        diags = self._diags
        by_sev = Counter(sev for _, _, sev, _ in diags)
        by_rule = Counter(rule for _, _, _, rule in diags if rule)
        by_file = Counter(f for _, f, _, _ in diags)

        body = [
            "summary:",
            "  diagnostics (exact): "
            + fmt_int(len(diags))
            + " · "
            + " · ".join(f"{sev} {fmt_int(n)}" for sev, n in sorted(by_sev.items())),
        ]
        if by_rule:
            body.append(
                "  by rule (exact): "
                + " · ".join(f"{r}×{n}" for r, n in sorted(by_rule.most_common(8)))
            )
        def _short(path: str) -> str:
            parts = path.replace("\\", "/").split("/")
            return "/".join(parts[-2:]) if len(parts) > 2 else path

        # Repair-mode affordance (measured: in the live lint-fix benchmark
        # the census alone LOST to naive — for bulk repair the full list is
        # the work queue). Each file's diagnostic block gets its own span,
        # so fixing file-by-file is one retrieval per file, not per question.
        stream = ctx.stdout if ctx.stdout.lines else ctx.stderr
        file_lines: dict[str, list[int]] = {}
        for lineno, f, _, _ in diags:
            file_lines.setdefault(f, []).append(lineno)
        file_bits = []
        for f, n in sorted(by_file.most_common(8)):
            span_lines = file_lines[f]
            fsid = ctx.mint_span(
                stream, "region", a=min(span_lines), b=max(span_lines)
            )
            tag = f" span {fsid}" if fsid else ""
            file_bits.append(f"{_short(f)}×{n}{tag}")
        body.append("  by file (exact): " + " · ".join(file_bits))
        first_line = diags[0][0]
        end = min(first_line + 6, len(self._lines))
        stream = ctx.stdout if ctx.stdout.lines >= end else ctx.stderr
        sid = ctx.mint_span(stream, "region", a=first_line, b=end)
        tag = f" · span {sid}" if sid else ""
        body.append(f"  first diagnostic L{first_line}-L{end}:{tag}")
        for raw in self._lines[first_line - 1 : end]:
            body.append(f"    | {raw[:160]}")

        rid = "run:PENDING"
        top_rule = by_rule.most_common(1)[0][0] if by_rule else "error"
        suggestions = [
            f"ctx search {rid} '{top_rule}' --context 1",
            f"ctx get {rid}#stdout --lines {first_line}:{end}",
        ]
        shown = 1
        return "\n".join(
            ctx.header_lines()
            + body
            + self.coverage_lines(ctx, shown)
            + self.next_lines(ctx, suggestions)
        )
