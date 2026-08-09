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
from ctx.textutil import fmt_int, short_path

# grep -n / rg: "path:line:content"  (also "path:line:col:content" from some
# tools — the col is folded into content, harmless for the census).
_MATCH_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<content>.*)$")
_MIN_MATCHES = 12  # below this the text profile / inline path is fine

# A hit that IS a declaration, in the languages the repo map understands.
_DEF_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|export|pub|async)\s+)*"
    r"(?:def|class|func|function|fn|type|struct|interface|trait|impl)\s+"
    r"[A-Za-z_]\w*"
)
# Matches smeared this widely are a question about shape, not about a line.
_DISPERSED_FILES = 12


def _parse(lines: list[str]) -> list[tuple[str, int, str, int]]:
    """(file, file_line, content, STDOUT_LINE).

    The stdout index is the fourth field because the span for "the rest of
    the matches" has to be minted from it. It used to be minted from the
    match ORDINAL, which is the same number only when every stdout line is a
    match -- false the moment grep prints -A/-B/-C context, and then the
    span covered a fraction of what it claimed.
    """
    out: list[tuple[str, int, str, int]] = []
    for idx, ln in enumerate(lines, start=1):
        m = _MATCH_RE.match(ln)
        if m:
            out.append((m.group("file"), int(m.group("line")), m.group("content"), idx))
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
        by_file = Counter(f for f, _, _, _ in matches)
        body = [
            "summary:",
            f"  matches (exact): {fmt_int(len(matches))} across "
            f"{fmt_int(len(by_file))} files",
        ]

        body.append(
            "  by file (exact): "
            + " · ".join(f"{short_path(f)}×{n}" for f, n in by_file.most_common(8))
        )
        if len(by_file) > 8:
            body.append(f"  … +{fmt_int(len(by_file) - 8)} more files")

        # Top matches verbatim with coordinates, then a span to the rest.
        shown = 0
        body.append("top matches:")
        for f, line, content, _stdout_line in matches[:8]:
            body.append(f"  {short_path(f)}:{line}: {content.strip()[:120]}")
            shown += 1
        if len(matches) > 8:
            # Real stdout coordinates, covering EVERY remaining match. The
            # old window stopped 200 matches in while the text beside it
            # claimed all of them -- the count and the address disagreeing
            # about the same set. A span is a coordinate range, not a
            # payload: widening it costs nothing to mint, and `ctx get
            # --span` bounds its own emission.
            sid = ctx.mint_span(
                ctx.stdout, "region", a=matches[8][3], b=matches[-1][3]
            )
            tag = f" · span {sid}" if sid else ""
            body.append(
                f"  … +{fmt_int(len(matches) - 8)} more matches{tag}"
            )

        rid = "run:PENDING"
        # Suggest narrowing by the most-hit file, or the full slice.
        top_file = by_file.most_common(1)[0][0]
        suggestions = [
            f"ctx get {rid}#stdout --lines 1:{min(len(matches), 60)}",
            f"ctx search {rid} '<narrower>' --glob '{short_path(top_file)}'",
        ]

        # Route to the structural index when the RESULTS say the question was
        # about shape rather than about a line: the hits are declarations, or
        # they are smeared across more files than anyone reads one at a time.
        # Narrowing such a search answers a different question than the one
        # posed, and it is the answer both existing suggestions give. Decided
        # from the result set, not from the query string, so it works
        # identically for a shell grep and for a host search tool whose pattern
        # never reaches this profile.
        defs = sum(1 for _, _, content, _ in matches if _DEF_RE.match(content))
        structural = defs >= max(3, len(matches) // 2)
        if structural or len(by_file) >= _DISPERSED_FILES:
            # First: `next_lines` caps at three, and this is the one that can
            # end the search rather than iterate it.
            suggestions.insert(0, "ctx map")
        return "\n".join(
            ctx.header_lines()
            + body
            + self.coverage_lines(ctx, shown)
            + self.next_lines(ctx, suggestions)
        )
