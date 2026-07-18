"""Search digest profile: grep/rg output rendered as structured results.

When a `grep`/`rg` command is wrapped through `ctx run` (the transparent
steering rewrite), its raw `file:line:content` output — often hundreds of
matches — otherwise falls to the generic text profile, which reports byte
counts and throws away the search structure. The model then re-greps to
narrow: a digest that *costs* a turn.

This profile digests the same output AS search results — total matches,
a per-file histogram, the top matches with their coordinates, and a span
to the full set — so the digest *saves* the turn instead. Sibling of
`lint/v1`; the two share the `file:line` shape.
"""

from __future__ import annotations

import re
from collections import Counter

from ctx.digest.base import DigestContext, Profile
from ctx.textutil import fmt_int

# grep -n / rg: "path:line:content"  (also "path:line:col:content" from some
# tools — the col is folded into content, harmless for the census).
_MATCH_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<content>.*)$")
_MIN_MATCHES = 12  # below this the text profile / inline path is fine


def _parse(lines: list[str]) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for ln in lines:
        m = _MATCH_RE.match(ln)
        if m:
            out.append((m.group("file"), int(m.group("line")), m.group("content")))
    return out


class SearchProfile(Profile):
    version = "search/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        # Anchored on the command being an actual grep/rg (the transparent
        # `ctx run -- grep …` rewrite always is). A pure content-ratio trigger
        # was tried and dropped: it stole log lines that happen to look like
        # ``file:line: text``.
        argv = ctx.manifest.get("argv") or []
        progs = {p.rsplit("/", 1)[-1] for p in argv[:1]} | {
            a for a in argv if a in ("grep", "rg", "egrep", "fgrep", "ack", "ag")
        }
        joined = " ".join(argv)
        looks_like_grep = bool(progs & {"grep", "rg", "egrep", "fgrep", "ack", "ag"}) or (
            "grep -" in joined or "rg " in joined
        )
        # The emission gate synthesizes argv=[tool_name]; recognize the native
        # Grep tool and MCP grep-shaped faucets so their file:line output reaches
        # search/v1 too. Narrow (exact / suffix), never substring, to preserve
        # the log-line theft guard the comment above warns about.
        if not looks_like_grep and len(argv) == 1:
            name = str(argv[0])
            leaf = name.rsplit("__", 1)[-1]
            looks_like_grep = name == "Grep" or leaf.endswith("search_code") or "grep" in leaf
        if not looks_like_grep:
            return None
        matches = _parse(ctx.stdout.text_lines[:6000])
        if len(matches) < _MIN_MATCHES:
            return None
        self._matches = matches
        return f"{len(matches)} file:line:content matches (grep/rg shape)"

    def render(self, ctx: DigestContext) -> str:
        matches = self._matches
        by_file = Counter(f for f, _, _ in matches)
        body = [
            "summary:",
            f"  matches (exact): {fmt_int(len(matches))} across "
            f"{fmt_int(len(by_file))} files",
        ]

        def _short(p: str) -> str:
            parts = p.replace("\\", "/").split("/")
            return "/".join(parts[-2:]) if len(parts) > 2 else p

        body.append(
            "  by file (exact): "
            + " · ".join(f"{_short(f)}×{n}" for f, n in by_file.most_common(8))
        )
        if len(by_file) > 8:
            body.append(f"  … +{fmt_int(len(by_file) - 8)} more files")

        # Top matches verbatim with coordinates, then a span to the rest.
        shown = 0
        body.append("top matches:")
        for f, line, content in matches[:8]:
            body.append(f"  {_short(f)}:{line}: {content.strip()[:120]}")
            shown += 1
        if len(matches) > 8:
            sid = ctx.mint_span(ctx.stdout, "region", a=9, b=min(len(matches), 9 + 200))
            tag = f" · span {sid}" if sid else ""
            body.append(
                f"  … +{fmt_int(len(matches) - 8)} more matches{tag}"
            )

        rid = "run:PENDING"
        # Suggest narrowing by the most-hit file, or the full slice.
        top_file = by_file.most_common(1)[0][0]
        suggestions = [
            f"ctx get {rid}#stdout --lines 1:{min(len(matches), 60)}",
            f"ctx search {rid} '<narrower>' --glob '{_short(top_file)}'",
        ]
        return "\n".join(
            ctx.header_lines()
            + body
            + self.coverage_lines(ctx, shown)
            + self.next_lines(ctx, suggestions)
        )
