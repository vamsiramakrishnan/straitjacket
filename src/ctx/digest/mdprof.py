"""markdown/v1 — structural heading outline for large Markdown documents.

Applies to Markdown documents from tool results (file reads, doc exports, MCP
tools), ``ctx run`` captures, and ``ctx intake`` payloads when too large to
inline verbatim.

Decision the digest supports: *which sections exist in this document, what
lines does each section span, and which exact slice should I retrieve next?*
Unlike ``text/v1``, it never attaches a crash-log ``evidence_map`` (prose
discussing errors or failure modes is documentation, not a failing log).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ctx import bounds
from ctx.digest.base import MAX_PARSE_BYTES, DigestContext, Profile, from_intake
from ctx.digest.intakeprof import mappable_text
from ctx.digest.shapeutil import (
    LINE_EXAMINE_CAP,
    Quoter,
    Windows,
    fit,
    inline_limit,
)
from ctx.textutil import fmt_bytes, fmt_int

# Maximum frontmatter lines scanned before concluding the opening `---` is a
# horizontal rule rather than YAML frontmatter.
_FRONTMATTER_MAX_LINES = 80
_FRONTMATTER_KEYS = ("name", "title", "description", "status", "author", "owner", "date")

_ATX_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]{0,31}):[ \t]*(.*)$")
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$")
_MD_LINK_RE = re.compile(r"\[[^\]\n]{1,80}\]\([^)\n]{1,120}\)")
_MD_LIST_RE = re.compile(r"^[ \t]{0,4}(?:[-*+][ \t]+\S|\d{1,3}\.[ \t]+\S|>[ \t]+\S)")
_CODE_LINE_RE = re.compile(
    r"^(?:def |class |import |from [A-Za-z_.\w]+ import |return |elif |try:|except\b|"
    r"package |func |const |let |var |export |function |public |private |protected |"
    r"fn |pub |struct |impl |use [A-Za-z_]|if \[\[|fi$|done$|esac$|set -[euo])"
)


@dataclass(frozen=True)
class Section:
    index: int
    level: int
    title: str
    start_line: int
    end_line: int
    tables: int = 0
    code_blocks: int = 0


@dataclass(frozen=True)
class MarkdownDoc:
    frontmatter: tuple[tuple[str, str], ...]
    frontmatter_end: int
    sections: tuple[Section, ...]
    tables: int
    code_blocks: int
    md_signals: int
    code_like: int
    parsed_lines: int
    shebang: bool

    @property
    def h2_plus(self) -> int:
        return sum(1 for s in self.sections if s.level >= 2)

    @property
    def distinct_levels(self) -> set[int]:
        return {s.level for s in self.sections}


def parse_markdown(text: str) -> MarkdownDoc:
    """Single-pass linear-time structural parse of a Markdown document."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total == 0:
        return MarkdownDoc((), 0, (), 0, 0, 0, 0, 0, False)

    first = lines[0].lstrip("\ufeff")
    if first.startswith("#!"):
        return MarkdownDoc((), 0, (), 0, 0, 0, 0, total, True)

    frontmatter: list[tuple[str, str]] = []
    fm_end = 0
    idx = 0
    while idx < total and not lines[idx].strip():
        idx += 1

    if idx < total and lines[idx].lstrip("\ufeff").strip() == "---":
        kv: list[tuple[str, str]] = []
        limit = min(total, idx + _FRONTMATTER_MAX_LINES)
        for j in range(idx + 1, limit):
            s = lines[j][:LINE_EXAMINE_CAP].strip()
            if s in ("---", "..."):
                if kv:
                    frontmatter = kv
                    fm_end = j + 1
                    idx = fm_end
                break
            m = _FM_KEY_RE.match(lines[j][:LINE_EXAMINE_CAP])
            if m:
                k = m.group(1).lower()
                v = m.group(2).strip().strip("\"'")
                if k in _FRONTMATTER_KEYS and v:
                    kv.append((k, v))

    fence_char = ""
    fence_len = 0
    raw_headings: list[tuple[int, str, int]] = []  # (level, title, start_line)
    sec_tables: list[int] = [0]
    sec_codes: list[int] = [0]
    total_tables = 0
    total_codes = 0
    md_signals = 0
    code_like = 0
    in_table = False

    while idx < total:
        line = lines[idx]
        head = line[:LINE_EXAMINE_CAP]
        fm = _FENCE_RE.match(head)
        if fm:
            marker = fm.group(1)
            ch, length = marker[0], len(marker)
            if not fence_char:
                fence_char, fence_len = ch, length
                total_codes += 1
                sec_codes[-1] += 1
                md_signals += 1
                in_table = False
            elif ch == fence_char and length >= fence_len and head[fm.end():].strip() == "":
                fence_char, fence_len = "", 0
            idx += 1
            continue

        if fence_char:
            idx += 1
            continue

        hm = _ATX_RE.match(head)
        if hm:
            title = line[hm.start(2):].rstrip()
            if title.endswith("#"):
                stripped = title.rstrip("#").rstrip(" \t")
                if stripped:
                    title = stripped
            if any(c.isalnum() for c in title):
                raw_headings.append((len(hm.group(1)), title, idx + 1))
                sec_tables.append(0)
                sec_codes.append(0)
                in_table = False
            idx += 1
            continue

        stripped = head.strip()
        if not stripped:
            in_table = False
            idx += 1
            continue

        if _TABLE_SEP_RE.match(head):
            if not in_table:
                total_tables += 1
                sec_tables[-1] += 1
                md_signals += 1
                in_table = True
            idx += 1
            continue

        if "|" not in head:
            in_table = False

        if _CODE_LINE_RE.match(stripped):
            code_like += 1
        else:
            if _MD_LIST_RE.match(head):
                md_signals += 1
            elif "**" in head or "`" in head or _MD_LINK_RE.search(head):
                md_signals += 1

        idx += 1

    sections: list[Section] = []
    n_h = len(raw_headings)
    for k, (level, title, start_line) in enumerate(raw_headings):
        end_line = raw_headings[k + 1][2] - 1 if k + 1 < n_h else total
        if end_line < start_line:
            end_line = start_line
        sections.append(
            Section(
                index=k + 1,
                level=level,
                title=title,
                start_line=start_line,
                end_line=end_line,
                tables=sec_tables[k + 1],
                code_blocks=sec_codes[k + 1],
            )
        )

    return MarkdownDoc(
        frontmatter=tuple(frontmatter),
        frontmatter_end=fm_end,
        sections=tuple(sections),
        tables=total_tables,
        code_blocks=total_codes,
        md_signals=md_signals,
        code_like=code_like,
        parsed_lines=total,
        shebang=False,
    )


def looks_like_markdown(doc: MarkdownDoc) -> bool:
    """Strict structural predicate that excludes source files with `#` comments
    and minimal synthetic `# Section N` fixtures."""
    if doc.shebang or not doc.sections:
        return False
    n = len(doc.sections)
    if doc.code_like >= 3 and doc.code_like > n:
        return False
    if doc.frontmatter and n >= 1:
        return True
    if n >= 2 and doc.h2_plus >= 1 and (len(doc.distinct_levels) >= 2 or doc.md_signals >= 1):
        return True
    if n >= 3 and doc.h2_plus >= 2:
        return True
    return False


def _plural(n: int, word: str) -> str:
    return f"{fmt_int(n)} {word}" if n == 1 else f"{fmt_int(n)} {word}s"


def _section_priority(sections: tuple[Section, ...], focus_hits: set[int]) -> list[int]:
    """Deterministic priority order (1-based section indices); budget fitting
    drops from the end."""
    n = len(sections)
    if n == 0:
        return []
    order: list[int] = [1]
    if n > 1:
        order.append(n)
    order.extend(sorted(focus_hits))
    for lvl in range(1, 7):
        order.extend(s.index for s in sections if s.level == lvl)
    seen: set[int] = set()
    return [i for i in order if not (i in seen or seen.add(i))]


def outline_file(
    path_str: str,
    policy,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    max_sections: int = 40,
) -> str | None:
    """Bounded ATX heading outline for a whole-file read of a large Markdown
    file, or None when the file does not parse as structured Markdown."""
    try:
        with open(path_str, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return None
    if not data or b"\x00" in data[:4096]:
        return None
    partial = len(data) > max_bytes
    if partial:
        data = data[:max_bytes]
    text = mappable_text(data.decode("utf-8", "replace"), not partial)
    doc = parse_markdown(text)
    if not looks_like_markdown(doc):
        return None
    q = Quoter(72, policy)
    lines: list[str] = []
    if doc.frontmatter:
        fm_bits = [f"{k}={q(v, 48)}" for k, v in doc.frontmatter[:4]]
        lines.append("frontmatter: " + " · ".join(fm_bits))
    bits = [_plural(len(doc.sections), "heading")]
    if doc.tables:
        bits.append(_plural(doc.tables, "table"))
    if doc.code_blocks:
        bits.append(_plural(doc.code_blocks, "code block"))
    bits.append(_plural(doc.parsed_lines, "line") + (" parsed" if partial else ""))
    lines.append("markdown: " + " · ".join(bits))
    cap = bounds.count(max_sections)
    kept_ids = sorted(_section_priority(doc.sections, set())[:cap])
    lines.append(f"sections ({len(kept_ids)} of {fmt_int(len(doc.sections))} shown):")
    for idx in kept_ids:
        s = doc.sections[idx - 1]
        prefix = "#" * s.level
        extras = []
        if s.tables:
            extras.append(_plural(s.tables, "table"))
        if s.code_blocks:
            extras.append(_plural(s.code_blocks, "code"))
        extra_txt = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"  L{s.start_line}-L{s.end_line} {prefix} {q(s.title, 72)}{extra_txt}")
    omitted = len(doc.sections) - len(kept_ids)
    if omitted > 0:
        lines.append(f"  … +{fmt_int(omitted)} more headings omitted")
    lines.extend(q.note())
    return "\n".join(lines)


class MarkdownProfile(Profile):
    version = "markdown/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        view = ctx.stdout
        if ctx.stderr.bytes or not view.bytes or not view.text:
            return None
        if view.media_type.startswith("application/octet-stream"):
            return None
        if not from_intake(ctx):
            code = ctx.manifest.get("result", {}).get("exitCode")
            if code not in (0, None):
                return None
        if view.bytes <= inline_limit(ctx):
            return None
        doc = parse_markdown(mappable_text(view.text, view.parsed_fully))
        if not looks_like_markdown(doc):
            return None
        levels = sorted(doc.distinct_levels)
        lvl_txt = f"H{levels[0]}-H{levels[-1]}" if len(levels) > 1 else f"H{levels[0]}"
        return f"markdown document: {len(doc.sections)} headings ({lvl_txt})"

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        partial = not view.parsed_fully
        text = mappable_text(view.text, view.parsed_fully)
        doc = parse_markdown(text)
        handle = "run:PENDING#stdout"
        rid = "run:PENDING"
        total_lines = view.lines
        policy = ctx.ws.config.redaction
        windows = Windows(ctx, text, total_lines)

        focus_hits: set[int] = set()
        if ctx.focus_terms:
            raw_lines = text.split("\n")
            for s in doc.sections:
                chunk = "\n".join(raw_lines[s.start_line - 1 : s.end_line]).lower()
                if any(t in chunk for t in ctx.focus_terms):
                    focus_hits.add(s.index)

        priority = _section_priority(doc.sections, focus_hits)
        spans: dict[tuple[int, int], str | None] = {}

        def span(a: int, b: int) -> str | None:
            if not windows.exact(a, b):
                return None
            if (a, b) not in spans:
                spans[(a, b)] = ctx.mint_span(view, "region", a=a, b=b)
            return spans[(a, b)]

        levels_seen = sorted(doc.distinct_levels)
        lvl_txt = (
            f"H{levels_seen[0]}-H{levels_seen[-1]}"
            if len(levels_seen) > 1
            else (f"H{levels_seen[0]}" if levels_seen else "none")
        )

        def build(level: int) -> str:
            max_secs, title_cap, fm_cap, show_spans = (
                (28, 72, 4, True),
                (16, 60, 3, True),
                (10, 48, 2, True),
                (6, 36, 1, False),
                (3, 28, 0, False),
            )[level]
            q = Quoter(title_cap, policy)
            out = (
                [ctx.intake_line(total=partial) + " · markdown"]
                if from_intake(ctx)
                else list(ctx.header_lines())
            )
            if doc.frontmatter and fm_cap:
                fm_bits = [f"{k}={q(v, min(title_cap, 48))}" for k, v in doc.frontmatter[:fm_cap]]
                out.append("frontmatter: " + " · ".join(fm_bits))
            summary_bits = [f"{_plural(len(doc.sections), 'heading')} ({lvl_txt})"]
            if doc.tables:
                summary_bits.append(_plural(doc.tables, "table"))
            if doc.code_blocks:
                summary_bits.append(_plural(doc.code_blocks, "code block"))
            out.append("markdown: " + " · ".join(summary_bits))

            kept = sorted(priority[:max_secs])
            focus_note = " (* matches focus)" if focus_hits else ""
            out.append(
                f"sections ({len(kept)} of {fmt_int(len(doc.sections))} shown){focus_note}:"
            )
            for idx in kept:
                s = doc.sections[idx - 1]
                mark = "* " if s.index in focus_hits else ""
                prefix = "#" * s.level
                n_lines = s.end_line - s.start_line + 1
                extras = [f"{fmt_int(n_lines)}L"]
                if s.tables:
                    extras.append(_plural(s.tables, "table"))
                if s.code_blocks:
                    extras.append(_plural(s.code_blocks, "code"))
                sid = span(s.start_line, s.end_line) if show_spans else None
                span_txt = f" · span {sid}" if sid else ""
                out.append(
                    f"  {mark}L{s.start_line}-L{s.end_line} {prefix} "
                    f"{q(s.title, title_cap)} ({', '.join(extras)}){span_txt}"
                )
            omitted = len(doc.sections) - len(kept)
            if omitted > 0:
                out.append(f"  … +{fmt_int(omitted)} more headings omitted")

            if partial:
                tail_from = doc.parsed_lines + 1
                out.append(
                    f"coverage: parsed L1-L{fmt_int(doc.parsed_lines)} of {fmt_int(total_lines)} "
                    f"lines ({fmt_bytes(len(text.encode('utf-8')))} of {fmt_bytes(view.bytes)}; "
                    f"parse cap {fmt_bytes(MAX_PARSE_BYTES)}), NOT parsed: "
                    f"L{fmt_int(tail_from)}-L{fmt_int(total_lines)}"
                )
            else:
                out.append(
                    f"coverage: all {fmt_int(total_lines)} lines parsed · "
                    f"{len(kept)}/{fmt_int(len(doc.sections))} headings shown"
                )

            sugg: list[str] = []
            if doc.sections:
                first_sec = doc.sections[sorted(focus_hits)[0] - 1] if focus_hits else doc.sections[0]
                end = windows.fit_end(first_sec.start_line, first_sec.end_line)
                sugg.append(f"ctx get {handle} --lines {first_sec.start_line}:{end}")
            if len(doc.sections) > 1:
                second = doc.sections[1]
                end2 = windows.fit_end(second.start_line, second.end_line)
                cmd2 = f"ctx get {handle} --lines {second.start_line}:{end2}"
                if cmd2 not in sugg:
                    sugg.append(cmd2)
            sugg.append(f"ctx search {rid} '<pattern>' --context 3")
            return "\n".join(out + self.next_lines(ctx, sugg) + q.note())

        return fit(ctx, build, 5)


__all__ = ["MarkdownDoc", "MarkdownProfile", "Section", "looks_like_markdown", "outline_file", "parse_markdown"]
