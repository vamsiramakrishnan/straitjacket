"""markup/v1 — structural outline for HTML, SVG, RSS/Atom, and XML documents.

Strips ``<script>``, ``<style>``, ``<noscript>``, and inline base64 ``data:``
URIs completely so web pages, diagrams, feeds, and XML configs collapse to a
clean, line-addressed structural map instead of thousands of lines of CSS/JS
noise. Pure stdlib (``html.parser`` + linear regex scanning; zero XXE risk).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from ctx import bounds
from ctx.digest.base import MAX_PARSE_BYTES, DigestContext, Profile, from_intake
from ctx.digest.intakeprof import mappable_text
from ctx.digest.shapeutil import Quoter, Windows, fit, inline_limit
from ctx.textutil import fmt_bytes, fmt_int

_HTML_START_RE = re.compile(r"^\s*(?:<!doctype\s+html\b|<html\b)", re.I)
_XML_DECL_RE = re.compile(r"^\s*<\?xml\b", re.I)
_SVG_START_RE = re.compile(r"^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!doctype\s+svg[^>]*>\s*)?<svg\b", re.I)
_FEED_START_RE = re.compile(
    r"^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--.*?-->\s*)*<(?:rss|feed|rdf:RDF)\b",
    re.I | re.S,
)
_ROOT_TAG_RE = re.compile(r"^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--.*?-->\s*)*<([A-Za-z_][\w:.-]*)\b")
_CODE_PREFIX_RE = re.compile(
    r"^\s*(?:import\s+|export\s+|function\s+|const\s+|let\s+|var\s+|class\s+|def\s+|package\s+)"
)
_BASE64_URI_RE = re.compile(r"data:[a-zA-Z0-9/+.-]+;base64,[A-Za-z0-9+/=]{32,}")
_HTML_STRUCT_TAGS = frozenset({
    "html", "head", "body", "main", "article", "section", "nav", "header",
    "footer", "table", "form", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "script", "style", "meta", "title",
})


@dataclass(frozen=True)
class MarkupItem:
    level: int
    kind: str
    title: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class MarkupDoc:
    kind: str  # html | svg | feed | xml
    title: str
    root_tag: str
    items: tuple[MarkupItem, ...]
    tag_counts: tuple[tuple[str, int], ...]
    tables: int
    forms: int
    links: int
    scripts: int
    styles: int
    stripped_bytes: int
    base64_uris: int
    base64_bytes: int
    viewbox: str
    parsed_lines: int


class _HTMLScanner(HTMLParser):
    def __init__(self, total_lines: int) -> None:
        super().__init__(convert_charrefs=True)
        self.total_lines = total_lines
        self.title = ""
        self._in_title = False
        self._title_parts: list[str] = []
        self._skip_tag: str = ""
        self._skip_bytes = 0
        self._heading_level = 0
        self._heading_line = 0
        self._heading_parts: list[str] = []
        self.headings: list[tuple[int, str, int]] = []
        self.tag_counts: dict[str, int] = {}
        self.tables = 0
        self.forms = 0
        self.links = 0
        self.scripts = 0
        self.styles = 0
        self.root_tag = ""
        self.viewbox = ""
        self._in_svg_text = False
        self._svg_text_line = 0
        self._svg_text_parts: list[str] = []
        self.svg_texts: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        line = self.getpos()[0]
        if not self.root_tag:
            self.root_tag = t
        self.tag_counts[t] = self.tag_counts.get(t, 0) + 1
        if t == "svg" and not self.viewbox:
            amap = {k.lower(): (v or "") for k, v in attrs}
            vb = amap.get("viewbox", "")
            w, h = amap.get("width", ""), amap.get("height", "")
            self.viewbox = vb or (f"{w}x{h}" if w and h else "")
        if t in ("script", "style", "noscript"):
            if t == "script":
                self.scripts += 1
            elif t == "style":
                self.styles += 1
            if not self._skip_tag:
                self._skip_tag = t
            return
        if self._skip_tag:
            return
        if t == "title" and not self.title:
            self._in_title = True
            self._title_parts = []
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6") and len(self.headings) < 300:
            self._heading_level = int(t[1])
            self._heading_line = line
            self._heading_parts = []
        elif t == "text" and len(self.svg_texts) < 120:
            self._in_svg_text = True
            self._svg_text_line = line
            self._svg_text_parts = []
        elif t == "table":
            self.tables += 1
        elif t == "form":
            self.forms += 1
        elif t == "a":
            self.links += 1

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if self._skip_tag:
            if t == self._skip_tag:
                self._skip_tag = ""
            return
        if t == "title" and self._in_title:
            self._in_title = False
            self.title = " ".join("".join(self._title_parts).split())
        elif self._heading_level and t == f"h{self._heading_level}":
            txt = " ".join("".join(self._heading_parts).split())
            if txt:
                self.headings.append((self._heading_level, txt, self._heading_line))
            self._heading_level = 0
        elif t == "text" and self._in_svg_text:
            self._in_svg_text = False
            txt = " ".join("".join(self._svg_text_parts).split())
            if txt:
                self.svg_texts.append((txt, self._svg_text_line))

    def handle_data(self, data: str) -> None:
        if self._skip_tag:
            self._skip_bytes += len(data.encode("utf-8", "replace"))
            return
        if self._in_title and len(self._title_parts) < 32:
            self._title_parts.append(data)
        if self._heading_level and len(self._heading_parts) < 32:
            self._heading_parts.append(data)
        if self._in_svg_text and len(self._svg_text_parts) < 32:
            self._svg_text_parts.append(data)


_XML_OPEN_RE = re.compile(r"<([A-Za-z_][\w:.-]*)(?:\s+([^>]{0,240}))?(/?)>")
_XML_ATTR_NAME_RE = re.compile(r'\b(?:id|name|key|title|type)\s*=\s*["\']([^"\']{1,80})["\']')
_FEED_ITEM_RE = re.compile(r"<(?:item|entry)\b", re.I)
_FEED_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.I | re.S)


def _scan_xml_or_feed(
    text: str, kind: str, root_tag: str, total_lines: int, b64_c: int, b64_b: int
) -> MarkupDoc:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    tag_counts: dict[str, int] = {}
    items: list[MarkupItem] = []
    title = ""

    if kind == "feed":
        tm = _FEED_TITLE_RE.search(text)
        if tm:
            title = " ".join(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", tm.group(1), flags=re.S).split())
        item_starts: list[int] = []
        for idx, row in enumerate(lines, 1):
            if _FEED_ITEM_RE.search(row):
                item_starts.append(idx)
            for m in _XML_OPEN_RE.finditer(row):
                t = m.group(1)
                tag_counts[t] = tag_counts.get(t, 0) + 1
        for i, st in enumerate(item_starts[:200]):
            en = item_starts[i + 1] - 1 if i + 1 < len(item_starts) else total_lines
            chunk = "\n".join(lines[st - 1 : min(en, st + 12)])
            itm = _FEED_TITLE_RE.search(chunk)
            ititle = (
                " ".join(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", itm.group(1), flags=re.S).split())
                if itm
                else f"item #{i + 1}"
            )
            items.append(MarkupItem(1, "item", ititle, st, en if en >= st else st))
    else:
        # Generic XML: capture top-level children (depth 1) with their line spans.
        depth = 0
        open_child: tuple[str, str, int] | None = None
        for idx, row in enumerate(lines, 1):
            # Ignore comments / declarations on line scan
            if "<!--" in row or "<?" in row:
                continue
            pos = 0
            while pos < len(row):
                lt = row.find("<", pos)
                if lt < 0:
                    break
                gt = row.find(">", lt + 1)
                if gt < 0:
                    break
                token = row[lt + 1 : gt].strip()
                pos = gt + 1
                if not token or token.startswith(("!", "?")):
                    continue
                if token.startswith("/"):
                    cname = token[1:].split(None, 1)[0]
                    if depth > 0:
                        depth -= 1
                    if depth == 1 and open_child is not None and open_child[0] == cname:
                        if len(items) < 200:
                            items.append(MarkupItem(1, open_child[0], open_child[1], open_child[2], idx))
                        open_child = None
                    continue
                self_close = token.endswith("/")
                tname = token.rstrip("/").split(None, 1)[0]
                tag_counts[tname] = tag_counts.get(tname, 0) + 1
                if depth == 1 and open_child is None and len(items) < 200:
                    am = _XML_ATTR_NAME_RE.search(token)
                    label = f"<{tname} {am.group(1)}>" if am else f"<{tname}>"
                    if self_close:
                        items.append(MarkupItem(1, tname, label, idx, idx))
                    else:
                        open_child = (tname, label, idx)
                if not self_close:
                    depth += 1
        if open_child is not None and len(items) < 200:
            items.append(MarkupItem(1, open_child[0], open_child[1], open_child[2], total_lines))

    top_tags = tuple(sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12])
    return MarkupDoc(
        kind=kind,
        title=title,
        root_tag=root_tag,
        items=tuple(items),
        tag_counts=top_tags,
        tables=tag_counts.get("table", 0),
        forms=tag_counts.get("form", 0),
        links=tag_counts.get("a", 0),
        scripts=0,
        styles=0,
        stripped_bytes=0,
        base64_uris=b64_c,
        base64_bytes=b64_b,
        viewbox="",
        parsed_lines=total_lines,
    )


def parse_markup(text: str) -> MarkupDoc | None:
    """Detect and structurally parse HTML, SVG, RSS/Atom, or XML."""
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped.startswith("<") or _CODE_PREFIX_RE.match(stripped):
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total == 0:
        return None

    b64_c = 0
    b64_b = 0
    if ";base64," in text:
        for m in _BASE64_URI_RE.finditer(text):
            b64_c += 1
            b64_b += m.end() - m.start()

    if _SVG_START_RE.match(stripped):
        scanner = _HTMLScanner(total)
        try:
            scanner.feed(text)
        except Exception:
            return None
        items = tuple(
            MarkupItem(1, "text", txt, ln, ln) for txt, ln in scanner.svg_texts
        )
        top_tags = tuple(sorted(scanner.tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10])
        return MarkupDoc(
            kind="svg",
            title=scanner.title,
            root_tag="svg",
            items=items,
            tag_counts=top_tags,
            tables=0,
            forms=0,
            links=scanner.links,
            scripts=scanner.scripts,
            styles=scanner.styles,
            stripped_bytes=scanner._skip_bytes,
            base64_uris=b64_c,
            base64_bytes=b64_b,
            viewbox=scanner.viewbox,
            parsed_lines=total,
        )

    if _FEED_START_RE.match(stripped):
        rm = _ROOT_TAG_RE.match(stripped)
        return _scan_xml_or_feed(text, "feed", rm.group(1) if rm else "rss", total, b64_c, b64_b)

    if _HTML_START_RE.match(stripped):
        scanner = _HTMLScanner(total)
        try:
            scanner.feed(text)
        except Exception:
            return None
        raw_h = scanner.headings
        items_list: list[MarkupItem] = []
        for i, (lvl, title, st) in enumerate(raw_h):
            en = raw_h[i + 1][2] - 1 if i + 1 < len(raw_h) else total
            if en < st:
                en = st
            items_list.append(MarkupItem(lvl, f"h{lvl}", title, st, en))
        top_tags = tuple(sorted(scanner.tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10])
        return MarkupDoc(
            kind="html",
            title=scanner.title,
            root_tag=scanner.root_tag or "html",
            items=tuple(items_list),
            tag_counts=top_tags,
            tables=scanner.tables,
            forms=scanner.forms,
            links=scanner.links,
            scripts=scanner.scripts,
            styles=scanner.styles,
            stripped_bytes=scanner._skip_bytes,
            base64_uris=b64_c,
            base64_bytes=b64_b,
            viewbox="",
            parsed_lines=total,
        )

    # Check HTML fragment (without <!DOCTYPE html>) or XML document.
    rm = _ROOT_TAG_RE.match(stripped)
    if not rm:
        return None
    root_tag = rm.group(1)
    if _XML_DECL_RE.match(stripped) or f"</{root_tag}>" in text:
        # Distinguish HTML fragment vs XML
        if root_tag.lower() in ("div", "main", "article", "section", "body", "table") and not _XML_DECL_RE.match(stripped):
            scanner = _HTMLScanner(total)
            try:
                scanner.feed(text)
            except Exception:
                return None
            distinct_html = len(set(scanner.tag_counts) & _HTML_STRUCT_TAGS)
            if distinct_html >= 3 and sum(scanner.tag_counts.values()) >= 8:
                raw_h = scanner.headings
                items_list = []
                for i, (lvl, title, st) in enumerate(raw_h):
                    en = raw_h[i + 1][2] - 1 if i + 1 < len(raw_h) else total
                    items_list.append(MarkupItem(lvl, f"h{lvl}", title, st, en if en >= st else st))
                top_tags = tuple(sorted(scanner.tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10])
                return MarkupDoc(
                    kind="html",
                    title=scanner.title,
                    root_tag=root_tag.lower(),
                    items=tuple(items_list),
                    tag_counts=top_tags,
                    tables=scanner.tables,
                    forms=scanner.forms,
                    links=scanner.links,
                    scripts=scanner.scripts,
                    styles=scanner.styles,
                    stripped_bytes=scanner._skip_bytes,
                    base64_uris=b64_c,
                    base64_bytes=b64_b,
                    viewbox="",
                    parsed_lines=total,
                )
            return None
        doc = _scan_xml_or_feed(text, "xml", root_tag, total, b64_c, b64_b)
        if sum(c for _, c in doc.tag_counts) < 4:
            return None
        return doc
    return None


def _plural(n: int, word: str) -> str:
    return f"{fmt_int(n)} {word}" if n == 1 else f"{fmt_int(n)} {word}s"


def _render_markup_summary(doc: MarkupDoc, q: Quoter, width: int, max_items: int) -> list[str]:
    lines: list[str] = []
    bits = [doc.kind]
    if doc.root_tag:
        bits.append(f"root=<{doc.root_tag}>")
    if doc.viewbox:
        bits.append(f"viewBox={q(doc.viewbox, 32)}")
    if doc.title:
        bits.append(f"title={q(doc.title, min(width, 48))}")
    lines.append("markup: " + " · ".join(bits))

    stats: list[str] = []
    if doc.tables:
        stats.append(_plural(doc.tables, "table"))
    if doc.forms:
        stats.append(_plural(doc.forms, "form"))
    if doc.links:
        stats.append(_plural(doc.links, "link"))
    if doc.scripts or doc.styles:
        stats.append(
            f"stripped {_plural(doc.scripts, 'script')}/{_plural(doc.styles, 'style')} "
            f"({fmt_bytes(doc.stripped_bytes)})"
        )
    if doc.base64_uris:
        stats.append(f"{_plural(doc.base64_uris, 'base64 URI')} ({fmt_bytes(doc.base64_bytes)})")
    if stats:
        lines.append("signals: " + " · ".join(stats))

    if doc.tag_counts:
        tags_s = ", ".join(f"<{t}>×{fmt_int(c)}" for t, c in doc.tag_counts[:8])
        lines.append(f"elements: {tags_s}")

    if doc.items:
        shown = doc.items[:max_items]
        label = (
            "headings" if doc.kind == "html"
            else "text labels" if doc.kind == "svg"
            else "feed items" if doc.kind == "feed"
            else "blocks"
        )
        lines.append(f"{label} ({len(shown)} of {fmt_int(len(doc.items))} shown):")
        for it in shown:
            if doc.kind == "html":
                prefix = "#" * it.level
                lines.append(f"  L{it.start_line}-L{it.end_line} {prefix} {q(it.title, width)}")
            else:
                lines.append(f"  L{it.start_line}-L{it.end_line} {q(it.title, width)}")
        if len(doc.items) > len(shown):
            lines.append(f"  … +{fmt_int(len(doc.items) - len(shown))} more {label} omitted")
    return lines


def outline_file(
    path_str: str,
    policy: Any,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    max_items: int = 36,
) -> str | None:
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
    doc = parse_markup(mappable_text(data.decode("utf-8", "replace"), not partial))
    if doc is None:
        return None
    q = Quoter(68, policy)
    lines = _render_markup_summary(doc, q, 68, bounds.count(max_items))
    lines.extend(q.note())
    return "\n".join(lines)


class MarkupProfile(Profile):
    version = "markup/v1"

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
        doc = parse_markup(mappable_text(view.text, view.parsed_fully))
        if doc is None:
            return None
        return f"structured {doc.kind} document (<{doc.root_tag}>)"

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        partial = not view.parsed_fully
        text = mappable_text(view.text, view.parsed_fully)
        doc = parse_markup(text)
        if doc is None:
            return "\n".join(list(ctx.header_lines()) + ["markup: unreadable"])
        handle = "run:PENDING#stdout"
        rid = "run:PENDING"
        total_lines = view.lines
        policy = ctx.ws.config.redaction
        windows = Windows(ctx, text, total_lines)

        def build(level: int) -> str:
            max_items, width = ((26, 70), (16, 58), (10, 46), (6, 36), (3, 28))[level]
            q = Quoter(width, policy)
            out = (
                [ctx.intake_line(total=partial) + f" · {doc.kind}"]
                if from_intake(ctx)
                else list(ctx.header_lines())
            )
            out.extend(_render_markup_summary(doc, q, width, max_items))
            if partial:
                out.append(
                    f"coverage: parsed L1-L{fmt_int(doc.parsed_lines)} of {fmt_int(total_lines)} "
                    f"lines (parse cap {fmt_bytes(MAX_PARSE_BYTES)})"
                )
            else:
                out.append(f"coverage: all {fmt_int(total_lines)} lines parsed")

            sugg: list[str] = []
            if doc.items:
                it0 = doc.items[0]
                end0 = windows.fit_end(it0.start_line, it0.end_line)
                sugg.append(f"ctx get {handle} --lines {it0.start_line}:{end0}")
                if len(doc.items) > 1:
                    it1 = doc.items[1]
                    end1 = windows.fit_end(it1.start_line, it1.end_line)
                    cmd1 = f"ctx get {handle} --lines {it1.start_line}:{end1}"
                    if cmd1 not in sugg:
                        sugg.append(cmd1)
            else:
                end0 = windows.fit_end(1, total_lines)
                sugg.append(f"ctx get {handle} --lines 1:{end0}")
            sugg.append(f"ctx search {rid} '<pattern>' --context 2")
            return "\n".join(out + self.next_lines(ctx, sugg) + q.note())

        return fit(ctx, build, 5)


__all__ = ["MarkupDoc", "MarkupItem", "MarkupProfile", "outline_file", "parse_markup"]
