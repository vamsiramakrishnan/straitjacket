"""Shared digest profile machinery.

A digest is a pure function of artifact bytes + normalized invocation
metadata + profile version + policy version + normalized focus. It must not
contain timestamps, absolute paths, elapsed-time noise, random samples,
locale-dependent formatting, or ANSI sequences (SPEC §8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ctx.store import Store
from ctx.textutil import estimate_tokens, fmt_bytes, fmt_int
from ctx.workspace import Workspace

# Cap how much of a stream a profile parses in memory. Coverage reporting
# makes any partial parse explicit.
MAX_PARSE_BYTES = 32 * 1024 * 1024


@dataclass
class StreamView:
    name: str
    bytes: int
    lines: int
    media_type: str
    text: str  # decoded (possibly lossy) view, capped at MAX_PARSE_BYTES
    parsed_fully: bool

    @property
    def text_lines(self) -> list[str]:
        return self.text.splitlines()


@dataclass
class DigestContext:
    ws: Workspace
    manifest: dict[str, Any]
    stdout: StreamView
    stderr: StreamView
    focus_terms: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def load(
        cls, store: Store, ws: Workspace, manifest: dict[str, Any], *, focus: str | None
    ) -> "DigestContext":
        views: dict[str, StreamView] = {}
        for name in ("stdout", "stderr"):
            meta = manifest["streams"][name]
            blob_hash = str(meta["blob"]).removeprefix("sha256:")
            size = int(meta["bytes"])
            if size == 0:
                views[name] = StreamView(name, 0, 0, meta["mediaType"], "", True)
                continue
            data = store.get_blob(blob_hash)
            parsed_fully = len(data) <= MAX_PARSE_BYTES
            if not parsed_fully:
                data = data[:MAX_PARSE_BYTES]
            if str(meta["mediaType"]).startswith("application/octet-stream"):
                text = ""
            else:
                text = data.decode("utf-8", "replace")
            views[name] = StreamView(
                name, size, int(meta["lines"]), meta["mediaType"], text, parsed_fully
            )
        terms = tuple(t for t in re.split(r"\W+", (focus or "").lower()) if len(t) >= 2)
        return cls(ws=ws, manifest=manifest, stdout=views["stdout"], stderr=views["stderr"], focus_terms=terms)

    # ------------------------------------------------------------- helpers
    def command_line(self) -> str:
        argv = self.manifest["argv"]
        if self.manifest.get("shell"):
            return argv[0]
        return " ".join(argv)

    def header_lines(self) -> list[str]:
        r = self.manifest["result"]
        status = "exit " + str(r["exitCode"]) if r["exitCode"] is not None else f"signal {r['signal']}"
        if r["timedOut"]:
            status += " · timed out"
        out, err = self.stdout, self.stderr
        lines = [
            f"cwd: {self.manifest['cwd']}",
            f"command: {self.command_line()}",
            f"exit: {status.removeprefix('exit ')}" if status.startswith("exit ") else f"status: {status}",
            f"stdout: {fmt_int(out.lines)} lines · {fmt_bytes(out.bytes)} · est {fmt_int(estimate_tokens(out.bytes))} tokens",
            f"stderr: {fmt_int(err.lines)} lines · {fmt_bytes(err.bytes)}",
        ]
        return lines

    def focus_spans(self, max_spans: int = 3, context: int = 2) -> list[tuple[str, int, int, str]]:
        """Deterministic focus evidence: first lines matching any focus term,
        in stream+line order. Returns (stream, start_line, end_line, sample)."""
        if not self.focus_terms:
            return []
        spans: list[tuple[str, int, int, str]] = []
        for view in (self.stderr, self.stdout):
            for i, line in enumerate(view.text_lines, start=1):
                low = line.lower()
                if any(t in low for t in self.focus_terms):
                    spans.append(
                        (view.name, max(1, i - context), i + context, line.strip()[:160])
                    )
                    if len(spans) >= max_spans:
                        return spans
        return spans


class Profile:
    """Base profile: subclasses set ``version`` and implement detect/render."""

    version = "base/v0"

    def detect(self, ctx: DigestContext) -> str | None:
        raise NotImplementedError

    def render(self, ctx: DigestContext) -> str:
        raise NotImplementedError

    # Shared closing sections -------------------------------------------------
    def coverage_lines(self, ctx: DigestContext, shown_spans: int, omitted: int | None = None) -> list[str]:
        total = ctx.stdout.lines + ctx.stderr.lines
        parsed = total if (ctx.stdout.parsed_fully and ctx.stderr.parsed_fully) else "partial"
        lines = ["coverage:"]
        if parsed == "partial":
            lines.append(f"  parsed: partial (streams exceed {fmt_bytes(MAX_PARSE_BYTES)}) of {fmt_int(total)} lines")
        else:
            lines.append(f"  parsed: {fmt_int(total)}/{fmt_int(total)} lines")
        omitted_n = omitted if omitted is not None else max(0, total - shown_spans)
        lines.append(f"  shown: {shown_spans} spans · omitted: {fmt_int(omitted_n)} lines")
        return lines

    def next_lines(self, ctx: DigestContext, suggestions: list[str]) -> list[str]:
        return ["next:"] + [f"  {s}" for s in suggestions[:3]] if suggestions else []

    def inline_body(self, ctx: DigestContext) -> list[str] | None:
        """Zero-hop path: when the complete output fits well inside the
        digest budget, include it verbatim (deterministic given bytes) so no
        retrieval round-trip is needed. Returns None when too large."""
        total = ctx.stdout.bytes + ctx.stderr.bytes
        limit = ctx.ws.config.budgets.digest_tokens * 3  # ~75% of budget in bytes/4 terms
        if total == 0 or total > limit:
            return None
        if ctx.stdout.media_type.startswith("application/octet-stream") or (
            ctx.stderr.media_type.startswith("application/octet-stream")
        ):
            return None
        lines = ["output (complete):"]
        for view in (ctx.stdout, ctx.stderr):
            if view.bytes:
                if view is ctx.stderr and ctx.stdout.bytes:
                    lines.append(f"--- {view.name} ---")
                lines.extend("  " + ln for ln in view.text_lines)
        return lines
