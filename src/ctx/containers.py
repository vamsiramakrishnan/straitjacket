"""Stdlib structural inspection and bounded part extraction for binary
containers (OOXML, ODF, EPUB, SQLite, ZIP/WHL/JAR, TAR, GZIP).

Pure Python standard library only (``zipfile``, ``tarfile``, ``gzip``,
``sqlite3``, ``xml.etree.ElementTree``, ``html.parser``). Every operation is
strictly bounded against zip bombs, XML entity expansion (XXE / billion
laughs), path-traversal member names, huge SQLite tables, and secret leakage:

* Decompression is read through a bounded stream cap (``MAX_MEMBER_DECOMPRESS``
  per member, ``MAX_TOTAL_DECOMPRESS`` across an inspection) — never an
  unbounded ``ZipFile.read()``.
* XML payloads containing ``<!ENTITY`` or DTD internal subsets / external
  identifiers are rejected before Expat parses them.
* SQLite databases are inspected strictly read-only (``mode=ro&immutable=1``
  plus ``PRAGMA query_only = ON`` and a progress-handler step limit) via a
  private temporary copy when given raw bytes, never creating ``-wal``/``-shm``
  files in the workspace.
* Every model-visible string passes through :class:`ctx.digest.shapeutil.Quoter`
  so secrets inside documents, sheets, slides, database cells, or archive names
  are redacted before clipping and cannot forge digest structure.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import sqlite3
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from html.parser import HTMLParser
from typing import Any

from ctx import bounds
from ctx.textutil import estimate_tokens, fmt_bytes, fmt_int, one_line

OOXML_FORMATS = frozenset({"docx", "xlsx", "xlsm", "pptx", "odt", "ods", "odp", "epub"})
ARCHIVE_FORMATS = frozenset({"zip", "whl", "jar", "tar", "tar.gz", "gzip"})
SQLITE_FORMATS = frozenset({"sqlite"})
CONTAINER_FORMATS = OOXML_FORMATS | ARCHIVE_FORMATS | SQLITE_FORMATS

# Hard security and latency ceilings.
MAX_MEMBER_DECOMPRESS = 2 * 1024 * 1024
MAX_TOTAL_DECOMPRESS = 6 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_SLIDES_INSPECT = 200
MAX_SHEETS_INSPECT = 64
MAX_DOC_PARAGRAPHS = 5_000
BOMB_MIN_UNCOMPRESSED = 10 * 1024 * 1024
BOMB_RATIO = 100
SQLITE_COUNT_CAP = 100_000
SQLITE_PROGRESS_STEPS = 250_000
SQLITE_MAX_TABLES = 200
GZIP_PEEK_BYTES = 128 * 1024

# Block XXE and billion-laughs entity expansion before Expat sees a byte.
_UNSAFE_XML_RE = re.compile(
    rb"<!ENTITY\b|<!DOCTYPE[^>]*(?:\[|\bSYSTEM\b|\bPUBLIC\b)",
    re.IGNORECASE,
)
_BENIGN_DOCTYPE_RE = re.compile(rb"<!DOCTYPE\s+html\s*>", re.IGNORECASE)
_CELL_COL_RE = re.compile(r"^([A-Za-z]+)")
_HEADING_STYLE_RE = re.compile(r"^(?:heading|titre|überschrift|title)\s*([1-6])?$", re.I)

_ODF_MIMETYPES = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/epub+zip": "epub",
}


class ContainerError(ValueError):
    """Raised on an invalid or unresolvable ``--part`` request."""


def _plural(n: int, word: str) -> str:
    return f"{fmt_int(n)} {word}" if n == 1 else f"{fmt_int(n)} {word}s"


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _read_zip_member(
    zf: zipfile.ZipFile,
    name: str,
    budget: list[int],
    cap: int = MAX_MEMBER_DECOMPRESS,
) -> bytes | None:
    """Bounded read of one ZIP member; enforces per-member and total caps."""
    if budget[0] <= 0:
        return None
    allow = min(cap, budget[0])
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.flag_bits & 0x1:  # encrypted
        return None
    if info.compress_size > 0 and info.file_size > BOMB_MIN_UNCOMPRESSED:
        if info.file_size // info.compress_size > BOMB_RATIO:
            return None
    try:
        with zf.open(info, "r") as fh:
            chunk = fh.read(allow + 1)
    except (zipfile.BadZipFile, OSError, zlib.error, RuntimeError, ValueError):
        return None
    if len(chunk) > allow:
        chunk = chunk[:allow]
    budget[0] -= len(chunk)
    return chunk


def _parse_xml_safe(data: bytes | None) -> ET.Element | None:
    if not data or _UNSAFE_XML_RE.search(data):
        return None
    cleaned = _BENIGN_DOCTYPE_RE.sub(b"", data)
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError:
        return None


def _is_tar_header(buf: bytes) -> bool:
    if len(buf) < 265:
        return False
    magic = buf[257:263]
    return magic in (b"ustar\x00", b"ustar ")


def _gzip_peek(data: bytes, limit: int = GZIP_PEEK_BYTES) -> tuple[bytes | None, bool]:
    """Bounded decompression peek of gzip bytes: ``(decompressed, truncated)``."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gz:
            chunk = gz.read(limit + 1)
    except (OSError, EOFError, zlib.error, ValueError):
        return None, False
    if len(chunk) > limit:
        return chunk[:limit], True
    return chunk, False


def sniff_container(data: bytes) -> str | None:
    """Return refined container format for valid binary containers, else None.

    Truncated or corrupt header-only stubs return None so ``binary/v1`` handles
    them unchanged.
    """
    if len(data) < 16:
        return None
    if data.startswith(b"SQLite format 3\x00") and len(data) >= 100:
        return "sqlite"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                names = zf.namelist()[:2000]
                name_set = set(names)
                if "word/document.xml" in name_set:
                    return "docx"
                if "xl/workbook.xml" in name_set:
                    return "xlsm" if "xl/vbaProject.bin" in name_set else "xlsx"
                if "ppt/presentation.xml" in name_set:
                    return "pptx"
                if "mimetype" in name_set:
                    mt_bytes = _read_zip_member(zf, "mimetype", [256], cap=256)
                    if mt_bytes:
                        mt = mt_bytes.decode("ascii", "ignore").strip()
                        if mt in _ODF_MIMETYPES:
                            return _ODF_MIMETYPES[mt]
                if "META-INF/container.xml" in name_set:
                    return "epub"
                if any(n.endswith(".dist-info/WHEEL") for n in names):
                    return "whl"
                if "META-INF/MANIFEST.MF" in name_set:
                    return "jar"
                return "zip"
        except (zipfile.BadZipFile, OSError, zlib.error, RuntimeError, ValueError):
            return None
    if data.startswith(b"\x1f\x8b"):
        peek, _ = _gzip_peek(data, 512)
        if peek is None:
            return None
        if _is_tar_header(peek):
            return "tar.gz"
        return "gzip"
    if _is_tar_header(data):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
                tf.next()
            return "tar"
        except (tarfile.TarError, OSError, EOFError, ValueError):
            return None
    return None


def profile_for_format(fmt: str) -> str | None:
    if fmt in OOXML_FORMATS:
        return "ooxml/v1"
    if fmt in SQLITE_FORMATS:
        return "sqlite/v1"
    if fmt in ARCHIVE_FORMATS:
        return "archive/v1"
    return None


# ---------------------------------------------------------- HTML text strip
class _PlainTextHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript"):
            self._skip += 1
        elif t in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data.strip():
            self.parts.append(data)


def _html_to_text(raw_bytes: bytes) -> str:
    parser = _PlainTextHTML()
    try:
        parser.feed(raw_bytes.decode("utf-8", "replace"))
    except Exception:
        return ""
    lines = [" ".join(chunk.split()) for chunk in "".join(parser.parts).splitlines()]
    return "\n".join(x for x in lines if x)


# ---------------------------------------------------------- OOXML / ODF / EPUB
def _core_props(zf: zipfile.ZipFile, budget: list[int]) -> dict[str, str]:
    root = _parse_xml_safe(_read_zip_member(zf, "docProps/core.xml", budget, cap=65536))
    if root is None:
        return {}
    meta: dict[str, str] = {}
    for elem in root:
        tag = _local(elem.tag).lower()
        val = (elem.text or "").strip()
        if tag in ("title", "creator", "subject", "modified", "created") and val:
            meta[tag] = val
    return meta


def _docx_paragraphs(root: ET.Element) -> tuple[list[dict[str, Any]], int, int, int]:
    """Return ``(paragraphs, table_count, insertions, deletions)``."""
    paragraphs: list[dict[str, Any]] = []
    tables = 0
    ins_count = 0
    del_count = 0
    body = None
    for child in root:
        if _local(child.tag) == "body":
            body = child
            break
    if body is None:
        return [], 0, 0, 0
    for elem in body:
        tag = _local(elem.tag)
        if tag == "tbl":
            tables += 1
            continue
        if tag != "p":
            continue
        if len(paragraphs) >= MAX_DOC_PARAGRAPHS:
            continue
        style = ""
        outline_lvl: int | None = None
        texts: list[str] = []
        for sub in elem.iter():
            st = _local(sub.tag)
            if st == "pStyle":
                for k, v in sub.attrib.items():
                    if _local(k) == "val":
                        style = v
            elif st == "outlineLvl":
                for k, v in sub.attrib.items():
                    if _local(k) == "val" and v.isdigit():
                        outline_lvl = int(v) + 1
            elif st == "ins":
                ins_count += 1
            elif st == "del":
                del_count += 1
            elif st == "t" and sub.text:
                texts.append(sub.text)
            elif st == "tab":
                texts.append("\t")
            elif st == "br":
                texts.append(" ")
        joined = "".join(texts).strip()
        level: int | None = None
        m = _HEADING_STYLE_RE.match(style.strip())
        if m:
            level = int(m.group(1)) if m.group(1) else 1
        elif outline_lvl is not None and 1 <= outline_lvl <= 6:
            level = outline_lvl
        if joined or level is not None:
            paragraphs.append({
                "idx": len(paragraphs) + 1,
                "level": level,
                "style": style,
                "text": joined,
            })
    return paragraphs, tables, ins_count, del_count


def _inspect_docx(zf: zipfile.ZipFile, data: bytes) -> dict[str, Any]:
    budget = [MAX_TOTAL_DECOMPRESS]
    meta = _core_props(zf, budget)
    doc_root = _parse_xml_safe(_read_zip_member(zf, "word/document.xml", budget))
    if doc_root is None:
        return {"format": "docx", "bytes": len(data), "error": "unreadable word/document.xml"}
    paragraphs, tables, ins_c, del_c = _docx_paragraphs(doc_root)
    comments = 0
    if "word/comments.xml" in zf.namelist():
        c_root = _parse_xml_safe(_read_zip_member(zf, "word/comments.xml", budget, cap=512 * 1024))
        if c_root is not None:
            comments = sum(1 for e in c_root if _local(e.tag) == "comment")
    sections: list[dict[str, Any]] = []
    heading_positions = [i for i, p in enumerate(paragraphs) if p["level"] is not None and p["text"]]
    for pos_i, p_idx in enumerate(heading_positions):
        start_p = paragraphs[p_idx]["idx"]
        end_p = (
            paragraphs[heading_positions[pos_i + 1]]["idx"] - 1
            if pos_i + 1 < len(heading_positions)
            else (paragraphs[-1]["idx"] if paragraphs else start_p)
        )
        sections.append({
            "index": pos_i + 1,
            "level": paragraphs[p_idx]["level"],
            "title": paragraphs[p_idx]["text"],
            "start_p": start_p,
            "end_p": end_p if end_p >= start_p else start_p,
        })
    return {
        "format": "docx",
        "bytes": len(data),
        "meta": meta,
        "paragraphs": len(paragraphs),
        "tables": tables,
        "comments": comments,
        "insertions": ins_c,
        "deletions": del_c,
        "sections": sections,
        "sample_paragraphs": [p["text"] for p in paragraphs[:3] if p["text"]],
    }


def _xlsx_shared_strings(zf: zipfile.ZipFile, budget: list[int]) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = _parse_xml_safe(_read_zip_member(zf, "xl/sharedStrings.xml", budget))
    if root is None:
        return []
    strings: list[str] = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        parts = [t.text for t in si.iter() if _local(t.tag) == "t" and t.text]
        strings.append("".join(parts))
    return strings


def _col_to_idx(ref: str) -> int:
    m = _CELL_COL_RE.match(ref)
    if not m:
        return 0
    val = 0
    for ch in m.group(1).upper():
        val = val * 26 + (ord(ch) - ord("A") + 1)
    return val


def _xlsx_sheet_entries(zf: zipfile.ZipFile, budget: list[int]) -> list[dict[str, Any]]:
    wb = _parse_xml_safe(_read_zip_member(zf, "xl/workbook.xml", budget, cap=256 * 1024))
    if wb is None:
        return []
    rels_map: dict[str, str] = {}
    rels = _parse_xml_safe(_read_zip_member(zf, "xl/_rels/workbook.xml.rels", budget, cap=256 * 1024))
    if rels is not None:
        for rel in rels:
            if _local(rel.tag) == "Relationship":
                rid = rel.attrib.get("Id", "")
                target = rel.attrib.get("Target", "").lstrip("/")
                if rid and target:
                    if not target.startswith("xl/"):
                        target = "xl/" + target
                    rels_map[rid] = target
    sheets: list[dict[str, Any]] = []
    for elem in wb.iter():
        if _local(elem.tag) != "sheet":
            continue
        name = elem.attrib.get("name", "")
        state = elem.attrib.get("state", "visible")
        rid = ""
        for k, v in elem.attrib.items():
            if _local(k) == "id":
                rid = v
                break
        target = rels_map.get(rid, "")
        if not target:
            cand = f"xl/worksheets/sheet{len(sheets) + 1}.xml"
            if cand in zf.namelist():
                target = cand
        sheets.append({
            "index": len(sheets) + 1,
            "name": name or f"Sheet{len(sheets) + 1}",
            "hidden": state != "visible",
            "target": target,
        })
        if len(sheets) >= MAX_SHEETS_INSPECT:
            break
    return sheets


def _xlsx_cell_value(cell: ET.Element, shared: list[str]) -> tuple[str, bool]:
    ctype = cell.attrib.get("t", "")
    has_formula = False
    val = ""
    inline_parts: list[str] = []
    for child in cell:
        ct = _local(child.tag)
        if ct == "f":
            has_formula = True
        elif ct == "v" and child.text is not None:
            val = child.text
        elif ct == "is":
            for t in child.iter():
                if _local(t.tag) == "t" and t.text:
                    inline_parts.append(t.text)
    if ctype == "s":
        if val.isdigit():
            idx = int(val)
            return (shared[idx] if 0 <= idx < len(shared) else ""), has_formula
        return "", has_formula
    if ctype == "inlineStr":
        return "".join(inline_parts), has_formula
    if ctype == "b":
        return ("TRUE" if val == "1" else "FALSE"), has_formula
    return val, has_formula


def _xlsx_parse_sheet_rows(
    root: ET.Element,
    shared: list[str],
    *,
    max_rows: int | None = None,
) -> tuple[list[list[str]], int, int, int]:
    """Return ``(rows, total_rows, max_cols, formula_count)``."""
    rows: list[list[str]] = []
    total_rows = 0
    max_cols = 0
    formulas = 0
    for elem in root.iter():
        if _local(elem.tag) != "row":
            continue
        total_rows += 1
        row_cells: dict[int, str] = {}
        col_cursor = 0
        for c in elem:
            if _local(c.tag) != "c":
                continue
            ref = c.attrib.get("r", "")
            col_idx = _col_to_idx(ref) if ref else (col_cursor + 1)
            col_cursor = col_idx
            if col_idx > max_cols:
                max_cols = col_idx
            val, has_f = _xlsx_cell_value(c, shared)
            if has_f:
                formulas += 1
            if max_rows is None or len(rows) < max_rows:
                if col_idx <= 128:
                    row_cells[col_idx] = val
        if max_rows is None or len(rows) < max_rows:
            width = min(max(row_cells) if row_cells else 0, 128)
            rows.append([row_cells.get(i, "") for i in range(1, width + 1)])
    return rows, total_rows, max_cols, formulas


def _inspect_xlsx(zf: zipfile.ZipFile, data: bytes, fmt: str) -> dict[str, Any]:
    budget = [MAX_TOTAL_DECOMPRESS]
    meta = _core_props(zf, budget)
    shared = _xlsx_shared_strings(zf, budget)
    sheets = _xlsx_sheet_entries(zf, budget)
    sheet_summaries: list[dict[str, Any]] = []
    for sh in sheets:
        sroot = _parse_xml_safe(_read_zip_member(zf, sh["target"], budget)) if sh["target"] else None
        if sroot is None:
            sheet_summaries.append({
                "index": sh["index"],
                "name": sh["name"],
                "hidden": sh["hidden"],
                "rows": 0,
                "cols": 0,
                "formulas": 0,
                "headers": [],
            })
            continue
        preview_rows, total_rows, max_cols, formulas = _xlsx_parse_sheet_rows(sroot, shared, max_rows=2)
        headers = [h for h in (preview_rows[0] if preview_rows else []) if h][:12]
        sheet_summaries.append({
            "index": sh["index"],
            "name": sh["name"],
            "hidden": sh["hidden"],
            "rows": total_rows,
            "cols": max_cols,
            "formulas": formulas,
            "headers": headers,
        })
    return {
        "format": fmt,
        "bytes": len(data),
        "meta": meta,
        "has_macros": fmt == "xlsm" or "xl/vbaProject.bin" in zf.namelist(),
        "sheets": sheet_summaries,
    }


def _pptx_slide_targets(zf: zipfile.ZipFile, budget: list[int]) -> list[str]:
    names = zf.namelist()
    pres = _parse_xml_safe(_read_zip_member(zf, "ppt/presentation.xml", budget, cap=256 * 1024))
    rels = _parse_xml_safe(_read_zip_member(zf, "ppt/_rels/presentation.xml.rels", budget, cap=256 * 1024))
    rels_map: dict[str, str] = {}
    if rels is not None:
        for rel in rels:
            if _local(rel.tag) == "Relationship":
                rid = rel.attrib.get("Id", "")
                target = rel.attrib.get("Target", "").lstrip("/")
                if rid and target:
                    if not target.startswith("ppt/"):
                        target = "ppt/" + target
                    rels_map[rid] = target
    ordered: list[str] = []
    if pres is not None and rels_map:
        for elem in pres.iter():
            if _local(elem.tag) == "sldId":
                for k, v in elem.attrib.items():
                    if _local(k) == "id" and v in rels_map:
                        ordered.append(rels_map[v])
                        break
    if not ordered:
        slide_re = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
        found = []
        for n in names:
            m = slide_re.match(n)
            if m:
                found.append((int(m.group(1)), n))
        ordered = [n for _, n in sorted(found)]
    return ordered[:MAX_SLIDES_INSPECT]


def _pptx_slide_detail(zf: zipfile.ZipFile, slide_path: str, budget: list[int]) -> dict[str, Any]:
    root = _parse_xml_safe(_read_zip_member(zf, slide_path, budget, cap=512 * 1024))
    if root is None:
        return {"title": "", "bullets": [], "tables": 0, "images": 0, "notes": ""}
    title = ""
    bullets: list[str] = []
    tables = 0
    images = 0
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "tbl":
            tables += 1
        elif tag == "pic":
            images += 1
        elif tag == "sp":
            is_title = False
            for sub in elem.iter():
                if _local(sub.tag) == "ph" and sub.attrib.get("type", "") in ("title", "ctrTitle"):
                    is_title = True
                    break
            paras: list[str] = []
            for p in elem.iter():
                if _local(p.tag) != "p":
                    continue
                txt = "".join(t.text for t in p.iter() if _local(t.tag) == "t" and t.text).strip()
                if txt:
                    paras.append(txt)
            if is_title and paras and not title:
                title = " ".join(paras)
            else:
                bullets.extend(paras)
    if not title and bullets:
        title = bullets[0]
        bullets = bullets[1:]
    # Check speaker notes via slide rels
    notes = ""
    base = slide_path.rsplit("/", 1)[-1]
    rel_path = f"ppt/slides/_rels/{base}.rels"
    if rel_path in zf.namelist():
        rroot = _parse_xml_safe(_read_zip_member(zf, rel_path, budget, cap=65536))
        if rroot is not None:
            for rel in rroot:
                target = rel.attrib.get("Target", "")
                if "notesSlide" in target:
                    npath = os.path.normpath(os.path.join("ppt/slides", target)).replace("\\", "/")
                    nroot = _parse_xml_safe(_read_zip_member(zf, npath, budget, cap=128 * 1024))
                    if nroot is not None:
                        ntxts = [
                            t.text.strip()
                            for t in nroot.iter()
                            if _local(t.tag) == "t" and t.text and t.text.strip()
                        ]
                        notes = " ".join(ntxts)
                    break
    return {
        "title": title,
        "bullets": bullets,
        "tables": tables,
        "images": images,
        "notes": notes,
    }


def _inspect_pptx(zf: zipfile.ZipFile, data: bytes) -> dict[str, Any]:
    budget = [MAX_TOTAL_DECOMPRESS]
    meta = _core_props(zf, budget)
    targets = _pptx_slide_targets(zf, budget)
    slides: list[dict[str, Any]] = []
    for idx, spath in enumerate(targets, 1):
        det = _pptx_slide_detail(zf, spath, budget)
        slides.append({
            "index": idx,
            "title": det["title"],
            "bullets": len(det["bullets"]),
            "tables": det["tables"],
            "images": det["images"],
            "has_notes": bool(det["notes"]),
        })
    return {
        "format": "pptx",
        "bytes": len(data),
        "meta": meta,
        "slides": slides,
    }


def _inspect_odf(zf: zipfile.ZipFile, data: bytes, fmt: str) -> dict[str, Any]:
    budget = [MAX_TOTAL_DECOMPRESS]
    root = _parse_xml_safe(_read_zip_member(zf, "content.xml", budget))
    if root is None:
        return {"format": fmt, "bytes": len(data), "error": "unreadable content.xml"}
    if fmt == "odt":
        sections: list[dict[str, Any]] = []
        para_idx = 0
        tables = 0
        for elem in root.iter():
            tag = _local(elem.tag)
            if tag == "table":
                tables += 1
            elif tag == "h":
                para_idx += 1
                txt = "".join(elem.itertext()).strip()
                lvl = 1
                for k, v in elem.attrib.items():
                    if _local(k) == "outline-level" and v.isdigit():
                        lvl = min(6, int(v))
                if txt:
                    sections.append({
                        "index": len(sections) + 1,
                        "level": lvl,
                        "title": txt,
                        "start_p": para_idx,
                        "end_p": para_idx,
                    })
            elif tag == "p":
                if "".join(elem.itertext()).strip():
                    para_idx += 1
        for i, sec in enumerate(sections):
            sec["end_p"] = sections[i + 1]["start_p"] - 1 if i + 1 < len(sections) else para_idx
            if sec["end_p"] < sec["start_p"]:
                sec["end_p"] = sec["start_p"]
        return {
            "format": "odt",
            "bytes": len(data),
            "meta": {},
            "paragraphs": para_idx,
            "tables": tables,
            "comments": 0,
            "insertions": 0,
            "deletions": 0,
            "sections": sections,
            "sample_paragraphs": [],
        }
    if fmt == "ods":
        sheets: list[dict[str, Any]] = []
        for elem in root.iter():
            if _local(elem.tag) != "table":
                continue
            name = ""
            for k, v in elem.attrib.items():
                if _local(k) == "name":
                    name = v
                    break
            row_count = 0
            max_cols = 0
            headers: list[str] = []
            for row in elem:
                if _local(row.tag) != "table-row":
                    continue
                cells = ["".join(c.itertext()).strip() for c in row if _local(c.tag) == "table-cell"]
                if any(cells):
                    row_count += 1
                    if len(cells) > max_cols:
                        max_cols = len(cells)
                    if not headers:
                        headers = [c for c in cells if c][:12]
            sheets.append({
                "index": len(sheets) + 1,
                "name": name or f"Sheet{len(sheets) + 1}",
                "hidden": False,
                "rows": row_count,
                "cols": max_cols,
                "formulas": 0,
                "headers": headers,
            })
            if len(sheets) >= MAX_SHEETS_INSPECT:
                break
        return {"format": "ods", "bytes": len(data), "meta": {}, "has_macros": False, "sheets": sheets}
    # odp
    slides: list[dict[str, Any]] = []
    for elem in root.iter():
        if _local(elem.tag) != "page":
            continue
        texts = [
            "".join(p.itertext()).strip()
            for p in elem.iter()
            if _local(p.tag) == "p" and "".join(p.itertext()).strip()
        ]
        slides.append({
            "index": len(slides) + 1,
            "title": texts[0] if texts else "",
            "bullets": len(texts) - 1 if len(texts) > 1 else 0,
            "tables": sum(1 for c in elem.iter() if _local(c.tag) == "table"),
            "images": sum(1 for c in elem.iter() if _local(c.tag) == "image"),
            "has_notes": any(_local(c.tag) == "notes" for c in elem),
        })
        if len(slides) >= MAX_SLIDES_INSPECT:
            break
    return {"format": "odp", "bytes": len(data), "meta": {}, "slides": slides}


def _inspect_epub(zf: zipfile.ZipFile, data: bytes) -> dict[str, Any]:
    budget = [MAX_TOTAL_DECOMPRESS]
    croot = _parse_xml_safe(_read_zip_member(zf, "META-INF/container.xml", budget, cap=65536))
    opf_path = ""
    if croot is not None:
        for elem in croot.iter():
            if _local(elem.tag) == "rootfile" and elem.attrib.get("full-path"):
                opf_path = elem.attrib["full-path"]
                break
    meta: dict[str, str] = {}
    chapters: list[dict[str, Any]] = []
    if opf_path and opf_path in zf.namelist():
        opf = _parse_xml_safe(_read_zip_member(zf, opf_path, budget, cap=256 * 1024))
        opf_dir = os.path.dirname(opf_path)
        manifest_map: dict[str, str] = {}
        if opf is not None:
            for elem in opf.iter():
                tag = _local(elem.tag).lower()
                if tag in ("title", "creator", "language") and elem.text and elem.text.strip():
                    meta.setdefault(tag, elem.text.strip())
                elif tag == "item":
                    iid = elem.attrib.get("id", "")
                    href = elem.attrib.get("href", "")
                    if iid and href:
                        full = os.path.normpath(os.path.join(opf_dir, href)).replace("\\", "/")
                        manifest_map[iid] = full
            for elem in opf.iter():
                if _local(elem.tag) == "itemref":
                    idref = elem.attrib.get("idref", "")
                    member = manifest_map.get(idref)
                    if member and member in zf.namelist():
                        info = zf.getinfo(member)
                        chapters.append({
                            "index": len(chapters) + 1,
                            "member": member,
                            "bytes": info.file_size,
                        })
    return {
        "format": "epub",
        "bytes": len(data),
        "meta": meta,
        "chapters": chapters,
    }


# ---------------------------------------------------------- Archives
def _inspect_zip_archive(zf: zipfile.ZipFile, data: bytes, fmt: str) -> dict[str, Any]:
    infos = zf.infolist()
    total_entries = len(infos)
    scanned = infos[:MAX_ENTRIES]
    total_uncomp = 0
    total_comp = 0
    dirs = 0
    ext_counts: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    bomb_risk = False
    for info in scanned:
        if info.is_dir():
            dirs += 1
            continue
        total_uncomp += info.file_size
        total_comp += info.compress_size
        if info.compress_size > 0 and info.file_size > BOMB_MIN_UNCOMPRESSED:
            if info.file_size // info.compress_size > BOMB_RATIO:
                bomb_risk = True
        name = info.filename
        dot = name.rfind(".")
        ext = name[dot:].lower() if dot >= 0 and "/" not in name[dot:] and len(name) - dot <= 8 else "(none)"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        entries.append({"name": name, "size": info.file_size, "compressed": info.compress_size})
    if total_comp > 0 and total_uncomp > BOMB_MIN_UNCOMPRESSED and total_uncomp // total_comp > BOMB_RATIO:
        bomb_risk = True
    entries.sort(key=lambda e: (-e["size"], e["name"]))
    top_exts = sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    return {
        "format": fmt,
        "bytes": len(data),
        "total_entries": total_entries,
        "files": len(entries),
        "dirs": dirs,
        "uncompressed": total_uncomp,
        "compressed": total_comp,
        "bomb_risk": bomb_risk,
        "top_exts": top_exts,
        "largest": entries[:30],
    }


def _inspect_tar(data: bytes, fmt: str) -> dict[str, Any]:
    mode = "r:gz" if fmt == "tar.gz" else "r:"
    entries: list[dict[str, Any]] = []
    ext_counts: dict[str, int] = {}
    total_uncomp = 0
    dirs = 0
    truncated = False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
            for member in tf:
                if len(entries) + dirs >= MAX_ENTRIES:
                    truncated = True
                    break
                if member.isdir():
                    dirs += 1
                    continue
                size = member.size if member.size > 0 else 0
                total_uncomp += size
                name = member.name
                dot = name.rfind(".")
                ext = name[dot:].lower() if dot >= 0 and "/" not in name[dot:] and len(name) - dot <= 8 else "(none)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                entries.append({"name": name, "size": size})
    except (tarfile.TarError, OSError, EOFError, zlib.error, ValueError):
        return {"format": fmt, "bytes": len(data), "error": "unreadable tar archive"}
    bomb_risk = (
        len(data) > 0
        and total_uncomp > BOMB_MIN_UNCOMPRESSED
        and total_uncomp // len(data) > BOMB_RATIO
    )
    entries.sort(key=lambda e: (-e["size"], e["name"]))
    top_exts = sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    return {
        "format": fmt,
        "bytes": len(data),
        "total_entries": len(entries) + dirs,
        "files": len(entries),
        "dirs": dirs,
        "uncompressed": total_uncomp,
        "compressed": len(data),
        "bomb_risk": bomb_risk,
        "truncated": truncated,
        "top_exts": top_exts,
        "largest": entries[:30],
    }


def _inspect_gzip_single(data: bytes) -> dict[str, Any]:
    peek, truncated = _gzip_peek(data, GZIP_PEEK_BYTES)
    if peek is None:
        return {"format": "gzip", "bytes": len(data), "error": "unreadable gzip stream"}
    is_binary = b"\x00" in peek[:4096]
    preview_lines: list[str] = []
    line_count = 0
    if not is_binary:
        decoded = peek.decode("utf-8", "replace")
        all_ln = decoded.splitlines()
        line_count = len(all_ln)
        preview_lines = all_ln[:5]
    return {
        "format": "gzip",
        "bytes": len(data),
        "peek_bytes": len(peek),
        "peek_truncated": truncated,
        "is_binary": is_binary,
        "peek_lines": line_count,
        "preview": preview_lines,
    }


# ---------------------------------------------------------- SQLite
def _open_sqlite_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.execute("PRAGMA query_only = ON")
    steps = [0]

    def _guard() -> int:
        steps[0] += 1000
        return 1 if steps[0] > SQLITE_PROGRESS_STEPS else 0

    conn.set_progress_handler(_guard, 1000)
    return conn


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _inspect_sqlite_conn(conn: sqlite3.Connection, size: int) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name LIMIT ?",
        (SQLITE_MAX_TABLES,),
    )
    objects = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")
    idx_row = cur.fetchone()
    index_count = int(idx_row[0]) if idx_row else 0
    tables: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    for otype, name in objects:
        qname = _quote_ident(name)
        cols: list[str] = []
        try:
            cur.execute(f"PRAGMA table_info({qname})")
            for row in cur.fetchall()[:24]:
                cname = str(row[1])
                ctype = str(row[2] or "").strip()
                cols.append(f"{cname}:{ctype}" if ctype else cname)
        except sqlite3.Error:
            pass
        if otype == "view":
            views.append({"name": name, "cols": cols})
            continue
        row_count: int | None = None
        capped = False
        try:
            cur.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {qname} LIMIT ?)", (SQLITE_COUNT_CAP + 1,))
            rc = int(cur.fetchone()[0])
            if rc > SQLITE_COUNT_CAP:
                row_count = SQLITE_COUNT_CAP
                capped = True
            else:
                row_count = rc
        except sqlite3.Error:
            row_count = None
        tables.append({
            "name": name,
            "rows": row_count,
            "capped": capped,
            "cols": cols,
        })
    return {
        "format": "sqlite",
        "bytes": size,
        "tables": tables,
        "views": views,
        "indexes": index_count,
    }


def _inspect_sqlite(data: bytes) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ctx-sqlite-") as tmpdir:
        db_path = os.path.join(tmpdir, "ro.sqlite")
        with open(db_path, "wb") as fh:
            fh.write(data)
        conn: sqlite3.Connection | None = None
        try:
            conn = _open_sqlite_ro(db_path)
            return _inspect_sqlite_conn(conn, len(data))
        except sqlite3.Error:
            return {"format": "sqlite", "bytes": len(data), "error": "unreadable sqlite database"}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass


def inspect_container(data: bytes, fmt: str | None = None) -> dict[str, Any] | None:
    """Inspect a binary container into a bounded metadata dictionary."""
    kind = fmt or sniff_container(data)
    if kind is None:
        return None
    if kind == "sqlite":
        return _inspect_sqlite(data)
    if kind in ("tar", "tar.gz"):
        return _inspect_tar(data, kind)
    if kind == "gzip":
        return _inspect_gzip_single(data)
    if kind in OOXML_FORMATS or kind in ("zip", "whl", "jar"):
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                if kind == "docx":
                    return _inspect_docx(zf, data)
                if kind in ("xlsx", "xlsm"):
                    return _inspect_xlsx(zf, data, kind)
                if kind == "pptx":
                    return _inspect_pptx(zf, data)
                if kind in ("odt", "ods", "odp"):
                    return _inspect_odf(zf, data, kind)
                if kind == "epub":
                    return _inspect_epub(zf, data)
                return _inspect_zip_archive(zf, data, kind)
        except (zipfile.BadZipFile, OSError, zlib.error, RuntimeError, ValueError):
            return None
    return None


# ---------------------------------------------------------- Digest rendering
def _shell_part_arg(spec: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+,#=-]+", spec):
        return spec
    return "'" + spec.replace("'", "'\"'\"'") + "'"


def render_container_body(
    info: dict[str, Any],
    *,
    address: str,
    policy: Any,
    header: list[str],
    level: int = 0,
    focus_terms: tuple[str, ...] = (),
) -> tuple[list[str], list[str], list[str]]:
    """Build ``(body_lines, suggested_commands, redaction_notes)`` for one
    detail level (0 = richest, 4 = tightest)."""
    from ctx.digest.shapeutil import Quoter

    max_items, width = ((24, 72), (14, 60), (8, 48), (5, 36), (3, 28))[min(level, 4)]
    q = Quoter(width, policy)
    fmt = str(info.get("format", "binary"))
    size = int(info.get("bytes", 0))
    out = list(header)
    out.append(
        f"container: {fmt} · {fmt_bytes(size)} ({size:,} bytes) · "
        f"~{estimate_tokens(size):,} tok if raw"
    )
    if info.get("error"):
        out.append(f"note: {one_line(str(info['error']))}")
        return out, [f"ctx get {address} --bytes 1:128"], q.note()

    meta = info.get("meta") or {}
    if meta and level <= 2:
        meta_bits = [f"{k}={q(v, min(width, 44))}" for k, v in list(meta.items())[:3]]
        out.append("metadata: " + " · ".join(meta_bits))

    sugg: list[str] = []

    if fmt in ("docx", "odt"):
        bits = [_plural(info.get("paragraphs", 0), "paragraph")]
        if info.get("tables"):
            bits.append(_plural(info["tables"], "table"))
        if info.get("comments"):
            bits.append(_plural(info["comments"], "comment"))
        ins_c, del_c = info.get("insertions", 0), info.get("deletions", 0)
        if ins_c or del_c:
            bits.append(f"tracked changes: +{fmt_int(ins_c)}/-{fmt_int(del_c)}")
        out.append("document: " + " · ".join(bits))
        sections = info.get("sections") or []
        if sections:
            shown = sections[:max_items]
            out.append(f"sections ({len(shown)} of {fmt_int(len(sections))} shown):")
            for sec in shown:
                prefix = "#" * min(6, int(sec.get("level", 1)))
                out.append(
                    f"  #{sec['index']} P{sec['start_p']}-P{sec['end_p']} "
                    f"{prefix} {q(sec['title'], width)}"
                )
            if len(sections) > len(shown):
                out.append(f"  … +{fmt_int(len(sections) - len(shown))} more sections omitted")
            sugg.append(f"ctx get {address} --part section:1")
        else:
            samples = info.get("sample_paragraphs") or []
            if samples:
                out.append("preview:")
                for idx, txt in enumerate(samples[: min(3, max_items)], 1):
                    out.append(f"  P{idx}: {q(txt, width)}")
            sugg.append(f"ctx get {address} --part paragraphs:1:40")
        if info.get("tables"):
            sugg.append(f"ctx get {address} --part tables")
        if info.get("comments"):
            sugg.append(f"ctx get {address} --part comments")

    elif fmt in ("xlsx", "xlsm", "ods"):
        sheets = info.get("sheets") or []
        note_bits = [_plural(len(sheets), "sheet")]
        if info.get("has_macros"):
            note_bits.append("contains VBA macros (not executed)")
        out.append("workbook: " + " · ".join(note_bits))
        shown = sheets[:max_items]
        out.append(f"sheets ({len(shown)} of {fmt_int(len(sheets))} shown):")
        for sh in shown:
            flags = []
            if sh.get("formulas"):
                flags.append(_plural(sh["formulas"], "formula"))
            if sh.get("hidden"):
                flags.append("hidden")
            flag_s = f" · {', '.join(flags)}" if flags else ""
            hdrs = sh.get("headers") or []
            hdr_s = ""
            if hdrs and level <= 2:
                hdr_s = " · cols: " + ", ".join(q(h, 24) for h in hdrs[:5])
            out.append(
                f"  #{sh['index']} {q(sh['name'], 32)} "
                f"({fmt_int(sh['rows'])}×{fmt_int(sh['cols'])}{flag_s}){hdr_s}"
            )
        if len(sheets) > len(shown):
            out.append(f"  … +{fmt_int(len(sheets) - len(shown))} more sheets omitted")
        if sheets:
            first_part = _shell_part_arg(f"sheet:#{sheets[0]['index']}:1:50")
            sugg.append(f"ctx get {address} --part {first_part}")
            if len(sheets) > 1:
                second_part = _shell_part_arg(f"sheet:#{sheets[1]['index']}:1:50")
                sugg.append(f"ctx get {address} --part {second_part}")

    elif fmt in ("pptx", "odp"):
        slides = info.get("slides") or []
        total_notes = sum(1 for s in slides if s.get("has_notes"))
        bits = [_plural(len(slides), "slide")]
        if total_notes:
            bits.append(f"{fmt_int(total_notes)} with speaker notes")
        out.append("deck: " + " · ".join(bits))
        shown = slides[:max_items]
        out.append(f"slides ({len(shown)} of {fmt_int(len(slides))} shown):")
        for sl in shown:
            extras = []
            if sl.get("bullets"):
                extras.append(_plural(sl["bullets"], "bullet"))
            if sl.get("tables"):
                extras.append(_plural(sl["tables"], "table"))
            if sl.get("images"):
                extras.append(_plural(sl["images"], "img"))
            if sl.get("has_notes"):
                extras.append("notes")
            extra_s = f" ({', '.join(extras)})" if extras else ""
            heading = q(sl["title"], width) if sl.get("title") else "(untitled)"
            out.append(f"  #{sl['index']} {heading}{extra_s}")
        if len(slides) > len(shown):
            out.append(f"  … +{fmt_int(len(slides) - len(shown))} more slides omitted")
        if slides:
            sugg.append(f"ctx get {address} --part slide:1")
            if len(slides) > 1:
                sugg.append(f"ctx get {address} --part slides:1:{min(5, len(slides))}")

    elif fmt == "epub":
        chapters = info.get("chapters") or []
        out.append(f"book: {_plural(len(chapters), 'spine chapter')}")
        shown = chapters[:max_items]
        out.append(f"chapters ({len(shown)} of {fmt_int(len(chapters))} shown):")
        for ch in shown:
            out.append(f"  #{ch['index']} {q(ch['member'], width, tail=True)} ({fmt_bytes(ch['bytes'])})")
        if len(chapters) > len(shown):
            out.append(f"  … +{fmt_int(len(chapters) - len(shown))} more chapters omitted")
        if chapters:
            sugg.append(f"ctx get {address} --part chapter:1")

    elif fmt == "sqlite":
        tables = info.get("tables") or []
        views = info.get("views") or []
        bits = [_plural(len(tables), "table")]
        if views:
            bits.append(_plural(len(views), "view"))
        if info.get("indexes"):
            bits.append(_plural(info["indexes"], "index"))
        out.append("sqlite: " + " · ".join(bits))
        shown = tables[:max_items]
        if shown:
            out.append(f"tables ({len(shown)} of {fmt_int(len(tables))} shown):")
            for tb in shown:
                if tb["rows"] is None:
                    rc_s = "? rows"
                elif tb["capped"]:
                    rc_s = f"{fmt_int(tb['rows'])}+ rows"
                else:
                    rc_s = _plural(tb["rows"], "row")
                cols = tb.get("cols") or []
                col_s = ""
                if cols and level <= 2:
                    col_s = " · cols: " + ", ".join(q(c, 28) for c in cols[:6])
                    if len(cols) > 6:
                        col_s += f" (+{len(cols) - 6})"
                out.append(f"  {q(tb['name'], 32)} ({rc_s}){col_s}")
            if len(tables) > len(shown):
                out.append(f"  … +{fmt_int(len(tables) - len(shown))} more tables omitted")
        if views and level <= 2:
            vnames = ", ".join(q(v["name"], 28) for v in views[:6])
            out.append(f"views: {vnames}")
        if tables:
            t0 = tables[0]["name"]
            sugg.append(f"ctx get {address} --part {_shell_part_arg('schema:' + t0)}")
            sugg.append(f"ctx get {address} --part {_shell_part_arg('table:' + t0 + ':1:50')}")

    elif fmt == "gzip":
        peek_b = int(info.get("peek_bytes", 0))
        trunc = " (peek capped)" if info.get("peek_truncated") else ""
        if info.get("is_binary"):
            out.append(f"gzip: decompressed payload is binary ({fmt_bytes(peek_b)} peeked{trunc})")
            sugg.append(f"ctx get {address} --bytes 1:128")
        else:
            out.append(
                f"gzip: text payload ({fmt_bytes(peek_b)} / {_plural(info.get('peek_lines', 0), 'line')} "
                f"peeked{trunc})"
            )
            preview = info.get("preview") or []
            if preview and level <= 2:
                out.append("preview:")
                for idx, ln in enumerate(preview[: min(4, max_items)], 1):
                    out.append(f"  L{idx}: {q(ln, width)}")
            sugg.append(f"ctx get {address} --part raw:1:100")

    else:
        # zip / whl / jar / tar / tar.gz
        bits = [
            _plural(info.get("files", 0), "file"),
            _plural(info.get("dirs", 0), "dir"),
            f"uncompressed {fmt_bytes(int(info.get('uncompressed', 0)))}",
        ]
        if info.get("bomb_risk"):
            bits.append("WARNING: extreme compression ratio (possible zip bomb — extraction capped)")
        out.append("archive: " + " · ".join(bits))
        top_exts = info.get("top_exts") or []
        if top_exts and level <= 2:
            ext_s = ", ".join(f"{e}={fmt_int(c)}" for e, c in top_exts[:6])
            out.append(f"types: {ext_s}")
        largest = info.get("largest") or []
        shown = largest[:max_items]
        if shown:
            out.append(f"largest members ({len(shown)} of {fmt_int(info.get('files', len(shown)))} files):")
            for ent in shown:
                out.append(f"  {q(ent['name'], width, tail=True)} ({fmt_bytes(ent['size'])})")
            if info.get("files", 0) > len(shown):
                out.append(f"  … +{fmt_int(info['files'] - len(shown))} more files omitted")
            first_m = shown[0]["name"]
            sugg.append(f"ctx get {address} --part {_shell_part_arg('member:' + first_m + ':1:100')}")

    return out, sugg, q.note()


# ---------------------------------------------------------- --part extraction
def _parse_range_suffix(spec: str, default_len: int) -> tuple[str, int, int]:
    """Split optional trailing ``:A:B`` from ``spec``."""
    m = re.match(r"^(.*):(\d+):(\d+)$", spec)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    return spec, 1, default_len


def _slice_lines(
    rendered_lines: list[str],
    a: int,
    b: int,
    part_prefix: str,
    max_lines: int,
    max_bytes: int,
) -> tuple[str, str, str | None]:
    total = len(rendered_lines)
    if total == 0:
        return f"{part_prefix} (empty)", "(empty)", None
    win = bounds.span(a, b, total)
    if win is None:
        raise ContainerError(
            f"--part {part_prefix}:{a}:{b} selects nothing (has {fmt_int(total)} items); "
            f"use --part {part_prefix}:1:{min(total, 50)}"
        )
    a, b = win
    cap_lines = bounds.count(max_lines)
    if cap_lines > 0 and b - a + 1 > cap_lines:
        b = a + cap_lines - 1
    chosen: list[str] = []
    used = 0
    for item in rendered_lines[a - 1 : b]:
        cost = len(item.encode("utf-8", "replace")) + 1
        if chosen and used + cost > max_bytes:
            break
        chosen.append(item)
        used += cost
    actual_b = a + len(chosen) - 1
    cont = None
    if actual_b < total:
        span = actual_b - a + 1
        nxt_a = actual_b + 1
        nxt_b = min(total, actual_b + (span if span > 0 else 50))
        cont = f"{part_prefix}:{nxt_a}:{nxt_b}"
    label = f"{part_prefix}:{a}:{actual_b} of {fmt_int(total)}"
    return label, "\n".join(chosen), cont


def _extract_docx_part(zf: zipfile.ZipFile, part: str, max_lines: int, max_bytes: int) -> tuple[str, str, str | None]:
    budget = [MAX_TOTAL_DECOMPRESS]
    if part == "comments":
        if "word/comments.xml" not in zf.namelist():
            return "comments (0)", "(no comments)", None
        croot = _parse_xml_safe(_read_zip_member(zf, "word/comments.xml", budget))
        if croot is None:
            raise ContainerError("word/comments.xml could not be parsed safely")
        rows = []
        for c in croot:
            if _local(c.tag) != "comment":
                continue
            author = ""
            date = ""
            for k, v in c.attrib.items():
                lk = _local(k)
                if lk == "author":
                    author = v
                elif lk == "date":
                    date = v
            txt = " ".join(t.text.strip() for t in c.iter() if _local(t.tag) == "t" and t.text and t.text.strip())
            rows.append(f"[{author or 'unknown'}{' · ' + date if date else ''}] {txt}")
        return _slice_lines(rows, 1, len(rows) or 1, "comments", max_lines, max_bytes)

    doc_root = _parse_xml_safe(_read_zip_member(zf, "word/document.xml", budget))
    if doc_root is None:
        raise ContainerError("word/document.xml could not be parsed safely")

    if part == "tables" or part.startswith("table:"):
        table_lines: list[str] = []
        t_idx = 0
        want_idx = int(part.split(":", 1)[1]) if part.startswith("table:") and part.split(":", 1)[1].isdigit() else None
        for elem in doc_root.iter():
            if _local(elem.tag) != "tbl":
                continue
            t_idx += 1
            if want_idx is not None and t_idx != want_idx:
                continue
            table_lines.append(f"--- table #{t_idx} ---")
            for tr in elem:
                if _local(tr.tag) != "tr":
                    continue
                cells = []
                for tc in tr:
                    if _local(tc.tag) != "tc":
                        continue
                    ct = " ".join(t.text.strip() for t in tc.iter() if _local(t.tag) == "t" and t.text and t.text.strip())
                    cells.append(ct)
                table_lines.append(" | ".join(cells))
        if not table_lines:
            raise ContainerError(f"no matching table for --part {part}")
        return _slice_lines(table_lines, 1, len(table_lines), part, max_lines, max_bytes)

    if part == "changes":
        ch_lines: list[str] = []
        for elem in doc_root.iter():
            tag = _local(elem.tag)
            if tag in ("ins", "del"):
                author = next((v for k, v in elem.attrib.items() if _local(k) == "author"), "")
                txt = "".join(t.text for t in elem.iter() if _local(t.tag) in ("t", "delText") and t.text).strip()
                prefix = "+" if tag == "ins" else "-"
                ch_lines.append(f"{prefix} [{author or 'unknown'}] {txt}")
        if not ch_lines:
            return "changes (0)", "(no tracked changes)", None
        return _slice_lines(ch_lines, 1, len(ch_lines), "changes", max_lines, max_bytes)

    paragraphs, _, _, _ = _docx_paragraphs(doc_root)
    formatted = [
        f"P{p['idx']}: {'#' * p['level'] + ' ' if p['level'] else ''}{p['text']}"
        for p in paragraphs
    ]
    if part.startswith("section:"):
        sec_str = part.split(":", 1)[1]
        if not sec_str.isdigit():
            raise ContainerError(f"invalid section selector {part!r}; use section:<N>")
        want = int(sec_str)
        headings = [i for i, p in enumerate(paragraphs) if p["level"] is not None and p["text"]]
        if want < 1 or want > len(headings):
            raise ContainerError(f"section {want} out of range (1..{len(headings)})")
        start_p = paragraphs[headings[want - 1]]["idx"]
        end_p = (
            paragraphs[headings[want]]["idx"] - 1
            if want < len(headings)
            else paragraphs[-1]["idx"]
        )
        return _slice_lines(formatted, start_p, end_p, "paragraphs", max_lines, max_bytes)

    if part.startswith("paragraphs"):
        _, a, b = _parse_range_suffix(part, 40)
        return _slice_lines(formatted, a, b, "paragraphs", max_lines, max_bytes)

    raise ContainerError(
        f"unknown docx --part {part!r}; use section:<N>, paragraphs:<A>:<B>, tables, comments, or changes"
    )


def _extract_xlsx_part(zf: zipfile.ZipFile, part: str, max_lines: int, max_bytes: int) -> tuple[str, str, str | None]:
    if not part.startswith("sheet:"):
        raise ContainerError(f"unknown workbook --part {part!r}; use sheet:<name>[:A:B] or sheet:#<N>[:A:B]")
    budget = [MAX_TOTAL_DECOMPRESS]
    shared = _xlsx_shared_strings(zf, budget)
    sheets = _xlsx_sheet_entries(zf, budget)
    rest = part[len("sheet:"):]
    target_key, a, b = _parse_range_suffix(rest, 50)
    chosen = None
    if target_key.startswith("#") and target_key[1:].isdigit():
        idx = int(target_key[1:])
        if 1 <= idx <= len(sheets):
            chosen = sheets[idx - 1]
    else:
        for sh in sheets:
            if sh["name"] == target_key or sh["name"].lower() == target_key.lower():
                chosen = sh
                break
    if chosen is None:
        avail = ", ".join(f"#{s['index']}:{s['name']}" for s in sheets[:8])
        raise ContainerError(f"sheet {target_key!r} not found (available: {avail})")
    sroot = _parse_xml_safe(_read_zip_member(zf, chosen["target"], budget))
    if sroot is None:
        raise ContainerError(f"sheet {chosen['name']!r} could not be parsed safely")
    rows, _, _, _ = _xlsx_parse_sheet_rows(sroot, shared, max_rows=None)
    formatted = [f"R{i}: " + "\t".join(r) for i, r in enumerate(rows, 1)]
    return _slice_lines(formatted, a, b, f"sheet:#{chosen['index']}", max_lines, max_bytes)


def _extract_pptx_part(zf: zipfile.ZipFile, part: str, max_lines: int, max_bytes: int) -> tuple[str, str, str | None]:
    budget = [MAX_TOTAL_DECOMPRESS]
    targets = _pptx_slide_targets(zf, budget)
    if part.startswith("slide:"):
        s = part.split(":", 1)[1]
        if not s.isdigit():
            raise ContainerError(f"invalid slide selector {part!r}; use slide:<N> or slides:<A>:<B>")
        a = b = int(s)
    elif part.startswith("slides:"):
        _, a, b = _parse_range_suffix(part, 5)
    else:
        raise ContainerError(f"unknown presentation --part {part!r}; use slide:<N> or slides:<A>:<B>")
    win = bounds.span(a, b, len(targets))
    if win is None:
        raise ContainerError(f"slide range {a}:{b} out of range (deck has {len(targets)} slides)")
    a, b = win
    lines: list[str] = []
    for idx in range(a, b + 1):
        det = _pptx_slide_detail(zf, targets[idx - 1], budget)
        lines.append(f"=== Slide #{idx}: {det['title'] or '(untitled)'} ===")
        for bul in det["bullets"]:
            lines.append(f"  - {bul}")
        if det["tables"] or det["images"]:
            lines.append(f"  [tables: {det['tables']}, images: {det['images']}]")
        if det["notes"]:
            lines.append(f"  [notes] {det['notes']}")
    out_lines: list[str] = []
    used = 0
    for ln in lines:
        c = len(ln.encode("utf-8", "replace")) + 1
        if out_lines and used + c > max_bytes:
            break
        out_lines.append(ln)
        used += c
    cont = f"slides:{b + 1}:{min(len(targets), b + (b - a + 1))}" if b < len(targets) else None
    return f"slides:{a}:{b} of {len(targets)}", "\n".join(out_lines), cont


def _extract_epub_part(zf: zipfile.ZipFile, part: str, max_lines: int, max_bytes: int) -> tuple[str, str, str | None]:
    if part.startswith("member:"):
        return _extract_zip_member_part(zf, part, max_lines, max_bytes)
    if not part.startswith("chapter:"):
        raise ContainerError(f"unknown epub --part {part!r}; use chapter:<N>[:A:B] or member:<path>[:A:B]")
    info = _inspect_epub(zf, b"")
    chapters = info.get("chapters") or []
    rest = part[len("chapter:"):]
    ch_key, a, b = _parse_range_suffix(rest, 100)
    if not ch_key.isdigit():
        raise ContainerError(f"invalid chapter selector {part!r}; use chapter:<N>[:A:B]")
    idx = int(ch_key)
    if idx < 1 or idx > len(chapters):
        raise ContainerError(f"chapter {idx} out of range (1..{len(chapters)})")
    member = chapters[idx - 1]["member"]
    raw_bytes = _read_zip_member(zf, member, [MAX_MEMBER_DECOMPRESS])
    if raw_bytes is None:
        raise ContainerError(f"cannot read epub chapter {member!r}")
    plain = _html_to_text(raw_bytes)
    lines = [f"L{i}: {ln}" for i, ln in enumerate(plain.splitlines(), 1)]
    return _slice_lines(lines, a, b, f"chapter:{idx}", max_lines, max_bytes)


def _extract_zip_member_part(zf: zipfile.ZipFile, part: str, max_lines: int, max_bytes: int) -> tuple[str, str, str | None]:
    if not part.startswith("member:"):
        raise ContainerError(f"unknown archive --part {part!r}; use member:<path>[:A:B]")
    rest = part[len("member:"):]
    mpath, a, b = _parse_range_suffix(rest, 100)
    if mpath not in zf.namelist():
        raise ContainerError(f"archive member {mpath!r} not found")
    raw_bytes = _read_zip_member(zf, mpath, [MAX_MEMBER_DECOMPRESS])
    if raw_bytes is None:
        raise ContainerError(f"archive member {mpath!r} refused (encrypted, oversized, or extreme compression ratio)")
    if b"\x00" in raw_bytes[:4096]:
        from ctx import binfmt

        binfo = binfmt.inspect(raw_bytes)
        return f"member:{mpath} (binary)", binfmt.render_digest(binfo), None
    lines = [f"L{i}: {ln}" for i, ln in enumerate(raw_bytes.decode("utf-8", "replace").splitlines(), 1)]
    return _slice_lines(lines, a, b, f"member:{mpath}", max_lines, max_bytes)


def _extract_tar_part(data: bytes, fmt: str, part: str, max_lines: int, max_bytes: int) -> tuple[str, str, str | None]:
    if not part.startswith("member:"):
        raise ContainerError(f"unknown tar --part {part!r}; use member:<path>[:A:B]")
    rest = part[len("member:"):]
    mpath, a, b = _parse_range_suffix(rest, 100)
    mode = "r:gz" if fmt == "tar.gz" else "r:"
    raw_bytes: bytes | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
            member = tf.getmember(mpath)
            if not member.isfile():
                raise ContainerError(f"tar member {mpath!r} is not a regular file")
            extracted = tf.extractfile(member)
            if extracted is not None:
                raw_bytes = extracted.read(MAX_MEMBER_DECOMPRESS)
    except KeyError:
        raise ContainerError(f"tar member {mpath!r} not found") from None
    except (tarfile.TarError, OSError, zlib.error, EOFError) as exc:
        raise ContainerError(f"cannot read tar member {mpath!r}: {exc}") from None
    if raw_bytes is None:
        raise ContainerError(f"tar member {mpath!r} has no readable content")
    if b"\x00" in raw_bytes[:4096]:
        from ctx import binfmt

        return f"member:{mpath} (binary)", binfmt.render_digest(binfmt.inspect(raw_bytes)), None
    lines = [f"L{i}: {ln}" for i, ln in enumerate(raw_bytes.decode("utf-8", "replace").splitlines(), 1)]
    return _slice_lines(lines, a, b, f"member:{mpath}", max_lines, max_bytes)


def _extract_sqlite_part(data: bytes, part: str, max_lines: int, max_bytes: int) -> tuple[str, str, str | None]:
    if not (part.startswith("schema:") or part.startswith("table:")):
        raise ContainerError(f"unknown sqlite --part {part!r}; use schema:<name> or table:<name>[:A:B]")
    with tempfile.TemporaryDirectory(prefix="ctx-sqlite-") as tmpdir:
        db_path = os.path.join(tmpdir, "ro.sqlite")
        with open(db_path, "wb") as fh:
            fh.write(data)
        conn: sqlite3.Connection | None = None
        try:
            conn = _open_sqlite_ro(db_path)
            cur = conn.cursor()
            if part.startswith("schema:"):
                tname = part[len("schema:"):]
                cur.execute(
                    "SELECT sql FROM sqlite_master WHERE (name = ? OR tbl_name = ?) AND sql IS NOT NULL",
                    (tname, tname),
                )
                ddls = [str(r[0]) for r in cur.fetchall()]
                if not ddls:
                    raise ContainerError(f"sqlite table/view {tname!r} not found")
                return f"schema:{tname}", "\n\n".join(ddls), None
            rest = part[len("table:"):]
            tname, a, b = _parse_range_suffix(rest, 50)
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (tname,),
            )
            if cur.fetchone() is None:
                raise ContainerError(f"sqlite table/view {tname!r} not found")
            qname = _quote_ident(tname)
            cur.execute(f"PRAGMA table_info({qname})")
            col_names = [str(r[1]) for r in cur.fetchall()]
            if a < 1 or b < a:
                raise ContainerError(f"invalid row range {a}:{b}; use 1-based A:B with A <= B")
            want = min(b - a + 1, bounds.count(max_lines) or 50, 200)
            cur.execute(f"SELECT * FROM {qname} LIMIT ? OFFSET ?", (want + 1, a - 1))
            fetched = cur.fetchall()
            has_more = len(fetched) > want
            rows = fetched[:want]
            if not rows and a > 1:
                raise ContainerError(f"table {tname!r} has fewer than {a} rows")
            lines = ["columns: " + "\t".join(col_names)] if col_names else []
            used = len(lines[0].encode("utf-8")) + 1 if lines else 0
            shown = 0
            for idx, r in enumerate(rows, a):
                row_txt = f"R{idx}: " + "\t".join("NULL" if v is None else one_line(str(v)) for v in r)
                cost = len(row_txt.encode("utf-8", "replace")) + 1
                if shown > 0 and used + cost > max_bytes:
                    has_more = True
                    break
                lines.append(row_txt)
                used += cost
                shown += 1
            actual_b = a + shown - 1 if shown else a - 1
            cont = f"table:{tname}:{actual_b + 1}:{actual_b + (shown or 50)}" if has_more else None
            return f"table:{tname}:{a}:{actual_b}", "\n".join(lines), cont
        except sqlite3.Error as exc:
            raise ContainerError(f"sqlite query failed: {exc}") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass


def extract_part(
    data: bytes,
    part: str,
    *,
    policy: Any,
    max_lines: int = 240,
    max_bytes: int = 4800,
) -> tuple[str, str, str | None]:
    """Extract a structured ``--part <spec>`` from a container or Jupyter
    notebook. Returns ``(selector_label, rendered_body, next_part_spec)``.
    """
    spec = part.strip()
    if not spec:
        raise ContainerError("--part cannot be empty")
    fmt = sniff_container(data)
    if fmt is None:
        # Check for Jupyter .ipynb notebook (--part cell:N / cells:A:B).
        if spec.startswith(("cell:", "cells:")):
            from ctx.digest.nbprof import extract_notebook_part

            return extract_notebook_part(data, spec, max_lines=max_lines, max_bytes=max_bytes)
        raise ContainerError(
            "--part is supported for Office/ODF/EPUB documents, archives, SQLite databases, "
            "and Jupyter notebooks; for plain text use --lines or --json-pointer"
        )
    if fmt == "sqlite":
        return _extract_sqlite_part(data, spec, max_lines, max_bytes)
    if fmt in ("tar", "tar.gz"):
        return _extract_tar_part(data, fmt, spec, max_lines, max_bytes)
    if fmt == "gzip":
        if not spec.startswith("raw"):
            raise ContainerError(f"unknown gzip --part {spec!r}; use raw[:A:B]")
        _, a, b = _parse_range_suffix(spec, 100)
        decompressed, _ = _gzip_peek(data, MAX_MEMBER_DECOMPRESS)
        if decompressed is None:
            raise ContainerError("cannot decompress gzip stream")
        if b"\x00" in decompressed[:4096]:
            raise ContainerError("gzip payload is binary; use --bytes on a decompressed run")
        lines = [f"L{i}: {ln}" for i, ln in enumerate(decompressed.decode("utf-8", "replace").splitlines(), 1)]
        return _slice_lines(lines, a, b, "raw", max_lines, max_bytes)
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            if fmt == "docx":
                return _extract_docx_part(zf, spec, max_lines, max_bytes)
            if fmt in ("xlsx", "xlsm"):
                return _extract_xlsx_part(zf, spec, max_lines, max_bytes)
            if fmt == "pptx":
                return _extract_pptx_part(zf, spec, max_lines, max_bytes)
            if fmt == "epub":
                return _extract_epub_part(zf, spec, max_lines, max_bytes)
            if fmt in ("odt", "ods", "odp"):
                if spec.startswith("member:"):
                    return _extract_zip_member_part(zf, spec, max_lines, max_bytes)
                raw_xml = _read_zip_member(zf, "content.xml", [MAX_MEMBER_DECOMPRESS])
                root = _parse_xml_safe(raw_xml)
                if root is None:
                    raise ContainerError(f"unreadable {fmt} content.xml")
                if fmt == "ods" and spec.startswith("sheet:"):
                    rest = spec[len("sheet:"):]
                    target_key, a, b = _parse_range_suffix(rest, 50)
                    tbls = [e for e in root.iter() if _local(e.tag) == "table"]
                    chosen_el = None
                    chosen_idx = 0
                    if target_key.startswith("#") and target_key[1:].isdigit():
                        idx = int(target_key[1:])
                        if 1 <= idx <= len(tbls):
                            chosen_el, chosen_idx = tbls[idx - 1], idx
                    else:
                        for i_t, el in enumerate(tbls, 1):
                            nm = next((v for k, v in el.attrib.items() if _local(k) == "name"), "")
                            if nm == target_key or nm.lower() == target_key.lower():
                                chosen_el, chosen_idx = el, i_t
                                break
                    if chosen_el is None:
                        raise ContainerError(f"ods sheet {target_key!r} not found")
                    rows_out: list[str] = []
                    for row_el in chosen_el:
                        if _local(row_el.tag) != "table-row":
                            continue
                        cells = ["".join(c.itertext()).strip() for c in row_el if _local(c.tag) == "table-cell"]
                        if any(cells):
                            rows_out.append(f"R{len(rows_out) + 1}: " + "\t".join(cells))
                    return _slice_lines(rows_out, a, b, f"sheet:#{chosen_idx}", max_lines, max_bytes)
                if fmt == "odp" and (spec.startswith("slide:") or spec.startswith("slides:")):
                    pages = [e for e in root.iter() if _local(e.tag) == "page"]
                    if spec.startswith("slide:"):
                        s_num = spec.split(":", 1)[1]
                        if not s_num.isdigit():
                            raise ContainerError(f"invalid slide selector {spec!r}")
                        a = b = int(s_num)
                    else:
                        _, a, b = _parse_range_suffix(spec, 5)
                    win = bounds.span(a, b, len(pages))
                    if win is None:
                        raise ContainerError(f"slide range {a}:{b} out of range (1..{len(pages)})")
                    a, b = win
                    sl_lines: list[str] = []
                    for idx in range(a, b + 1):
                        txts = ["".join(p.itertext()).strip() for p in pages[idx - 1].iter() if _local(p.tag) == "p" and "".join(p.itertext()).strip()]
                        sl_lines.append(f"=== Slide #{idx}: {txts[0] if txts else '(untitled)'} ===")
                        for bul in txts[1:]:
                            sl_lines.append(f"  - {bul}")
                    cont = f"slides:{b + 1}:{min(len(pages), b + (b - a + 1))}" if b < len(pages) else None
                    return f"slides:{a}:{b} of {len(pages)}", "\n".join(sl_lines), cont
                # ODT section:N or paragraphs:A:B
                paras: list[tuple[int | None, str]] = []
                for elem in root.iter():
                    tg = _local(elem.tag)
                    if tg == "h":
                        t = "".join(elem.itertext()).strip()
                        lvl = next((int(v) for k, v in elem.attrib.items() if _local(k) == "outline-level" and v.isdigit()), 1)
                        if t:
                            paras.append((min(6, lvl), t))
                    elif tg == "p":
                        t = "".join(elem.itertext()).strip()
                        if t:
                            paras.append((None, t))
                formatted = [f"P{i}: {'#' * lv + ' ' if lv else ''}{tx}" for i, (lv, tx) in enumerate(paras, 1)]
                if spec.startswith("section:"):
                    s_num = spec.split(":", 1)[1]
                    if not s_num.isdigit():
                        raise ContainerError(f"invalid section selector {spec!r}")
                    want = int(s_num)
                    heads = [i for i, (lv, _) in enumerate(paras) if lv is not None]
                    if want < 1 or want > len(heads):
                        raise ContainerError(f"section {want} out of range (1..{len(heads)})")
                    st_p = heads[want - 1] + 1
                    en_p = heads[want] if want < len(heads) else len(paras)
                    return _slice_lines(formatted, st_p, en_p, "paragraphs", max_lines, max_bytes)
                _, a, b = _parse_range_suffix(spec, 50)
                return _slice_lines(formatted, a, b, "paragraphs", max_lines, max_bytes)
            return _extract_zip_member_part(zf, spec, max_lines, max_bytes)
    except ContainerError:
        raise
    except (zipfile.BadZipFile, OSError, zlib.error, RuntimeError, ValueError) as exc:
        raise ContainerError(f"cannot read {fmt} container: {exc}") from None


def outline_container_file(
    path_str: str,
    policy: Any,
    *,
    repo_rel: str | None = None,
    max_file_bytes: int = 32 * 1024 * 1024,
) -> tuple[str, str] | None:
    """Return ``(format, bounded_outline)`` for a workspace container file,
    or None if unreadable/oversized/corrupt."""
    try:
        st = os.stat(path_str)
        if st.st_size <= 0 or st.st_size > max_file_bytes:
            return None
        with open(path_str, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    fmt = sniff_container(data)
    if fmt is None:
        return None
    info = inspect_container(data, fmt)
    if info is None or info.get("error"):
        return None
    addr = f"repo:{repo_rel}" if repo_rel else path_str
    body, sugg, notes = render_container_body(info, address=addr, policy=policy, header=[], level=1)
    lines = list(body)
    for cmd in sugg[:3]:
        lines.append(f"next: {cmd}")
    lines.extend(notes)
    return fmt, "\n".join(lines)


__all__ = [
    "ARCHIVE_FORMATS",
    "CONTAINER_FORMATS",
    "ContainerError",
    "OOXML_FORMATS",
    "SQLITE_FORMATS",
    "extract_part",
    "inspect_container",
    "outline_container_file",
    "profile_for_format",
    "render_container_body",
    "sniff_container",
]
