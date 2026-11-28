"""notebook/v1 — structural cell outline for Jupyter notebooks (``.ipynb``).

Jupyter notebooks are valid JSON, so without this profile ``json/v1`` claims
them and loses cell ordering, markdown headings, execution errors, and output
summaries — while raw reads drown the context window in embedded base64
``image/png`` blobs.

``notebook/v1`` sits directly before ``JsonProfile`` in ``_PROFILES`` and
claims only valid Jupyter notebook JSON (integer ``nbformat`` + ``cells`` list).
Every embedded binary MIME output (``image/png``, ``image/jpeg``, ``image/svg+xml``,
``application/pdf``) is summarized by MIME type and byte size — base64 payload
strings never enter the digest or ``--part cell:N`` retrieval.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass
from typing import Any

from ctx import bounds
from ctx.digest.base import DigestContext, Profile, from_intake
from ctx.digest.shapeutil import Quoter, fit, inline_limit
from ctx.textutil import fmt_bytes, fmt_int, loads_fast

_ANSI_STRIP_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BINARY_MIMES = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "application/pdf",
)


@dataclass(frozen=True)
class CellSummary:
    index: int            # 1-based cell number
    cell_type: str        # markdown | code | raw
    exec_count: int | None
    source_lines: int
    first_line: str
    stdout_lines: int
    error_name: str
    error_value: str
    images: tuple[tuple[str, int], ...]  # (mime, raw_chars)
    searchable: str


@dataclass(frozen=True)
class NotebookDoc:
    nbformat: int
    kernel: str
    language: str
    cells: tuple[CellSummary, ...]
    md_count: int
    code_count: int
    raw_count: int
    error_count: int
    image_count: int
    image_bytes: int


def _join_source(src: Any) -> str:
    if isinstance(src, list):
        return "".join(str(x) for x in src)
    if isinstance(src, str):
        return src
    return ""


def _first_meaningful_line(source: str) -> str:
    for item in source.splitlines():
        cleaned = item.strip()
        if cleaned:
            return cleaned
    return ""


def _parse_notebook_obj(obj: Any) -> NotebookDoc | None:
    if not isinstance(obj, dict):
        return None
    nbf = obj.get("nbformat")
    if not isinstance(nbf, int) or isinstance(nbf, bool) or nbf < 3 or nbf > 10:
        return None
    raw_cells = obj.get("cells")
    if not isinstance(raw_cells, list):
        return None
    meta = obj.get("metadata")
    if raw_cells:
        first_c = raw_cells[0]
        if not isinstance(first_c, dict) or first_c.get("cell_type") not in ("markdown", "code", "raw"):
            return None
        if "source" not in first_c:
            return None
    elif not isinstance(meta, dict):
        return None

    kernel = ""
    language = ""
    if isinstance(meta, dict):
        ks = meta.get("kernelspec")
        if isinstance(ks, dict):
            kernel = str(ks.get("display_name") or ks.get("name") or "").strip()
        li = meta.get("language_info")
        if isinstance(li, dict):
            lname = str(li.get("name") or "").strip()
            lver = str(li.get("version") or "").strip()
            language = f"{lname} {lver}".strip() if lname else ""

    cells: list[CellSummary] = []
    md_c = code_c = raw_c = err_c = img_c = img_bytes = 0

    for idx, cell in enumerate(raw_cells, 1):
        if not isinstance(cell, dict):
            continue
        ctype = str(cell.get("cell_type") or "raw")
        if ctype == "markdown":
            md_c += 1
        elif ctype == "code":
            code_c += 1
        else:
            raw_c += 1
        src = _join_source(cell.get("source"))
        src_lines = len(src.splitlines()) if src else 0
        first_ln = _first_meaningful_line(src)
        ec = cell.get("execution_count")
        exec_count = ec if isinstance(ec, int) and not isinstance(ec, bool) else None

        stdout_lines = 0
        err_name = ""
        err_val = ""
        images: list[tuple[str, int]] = []
        search_parts = [first_ln]

        outputs = cell.get("outputs")
        if isinstance(outputs, list):
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                otype = str(item.get("output_type") or "")
                if otype == "stream":
                    stxt = _join_source(item.get("text"))
                    stdout_lines += len(stxt.splitlines()) if stxt else 0
                elif otype == "error":
                    err_name = str(item.get("ename") or "Error")
                    err_val = _ANSI_STRIP_RE.sub("", str(item.get("evalue") or ""))
                    err_c += 1
                    search_parts.append(err_name)
                    search_parts.append(err_val)
                elif otype in ("display_data", "execute_result"):
                    dmap = item.get("data")
                    if isinstance(dmap, dict):
                        for mime in _BINARY_MIMES:
                            if mime in dmap:
                                bdata = _join_source(dmap[mime])
                                sz = len(bdata)
                                images.append((mime, sz))
                                img_c += 1
                                img_bytes += sz
                        if "text/plain" in dmap and not images:
                            ptxt = _join_source(dmap["text/plain"])
                            stdout_lines += len(ptxt.splitlines()) if ptxt else 0

        cells.append(
            CellSummary(
                index=idx,
                cell_type=ctype,
                exec_count=exec_count,
                source_lines=src_lines,
                first_line=first_ln,
                stdout_lines=stdout_lines,
                error_name=err_name,
                error_value=err_val,
                images=tuple(images),
                searchable=" ".join(search_parts).lower(),
            )
        )

    return NotebookDoc(
        nbformat=nbf,
        kernel=kernel,
        language=language,
        cells=tuple(cells),
        md_count=md_c,
        code_count=code_c,
        raw_count=raw_c,
        error_count=err_c,
        image_count=img_c,
        image_bytes=img_bytes,
    )


def parse_notebook(text: str) -> NotebookDoc | None:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    if '"cells"' not in text or '"nbformat"' not in text:
        return None
    try:
        obj = loads_fast(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return _parse_notebook_obj(obj)


def _plural(n: int, word: str) -> str:
    return f"{fmt_int(n)} {word}" if n == 1 else f"{fmt_int(n)} {word}s"


def _cell_priority(cells: tuple[CellSummary, ...], focus_hits: set[int]) -> list[int]:
    n = len(cells)
    if n == 0:
        return []
    order: list[int] = [1]
    if n > 1:
        order.append(n)
    order.extend(sorted(focus_hits))
    # Prioritize error cells, markdown headings, then code cells.
    order.extend(c.index for c in cells if c.error_name)
    order.extend(c.index for c in cells if c.cell_type == "markdown" and c.first_line.startswith("#"))
    order.extend(c.index for c in cells if c.cell_type == "markdown")
    order.extend(c.index for c in cells)
    seen: set[int] = set()
    return [i for i in order if not (i in seen or seen.add(i))]


def _format_cell_row(c: CellSummary, q: Quoter, width: int, mark: str = "") -> str:
    if c.cell_type == "markdown":
        tag = "[md]"
        label = q(c.first_line or "(empty)", width)
        return f"  {mark}#{c.index} {tag} {label} ({fmt_int(c.source_lines)}L)"
    if c.cell_type == "raw":
        tag = "[raw]"
        return f"  {mark}#{c.index} {tag} {q(c.first_line or '(empty)', width)} ({fmt_int(c.source_lines)}L)"
    tag = f"[code:{c.exec_count}]" if c.exec_count is not None else "[code]"
    parts = [f"{fmt_int(c.source_lines)}L"]
    if c.error_name:
        parts.append(f"! {q(c.error_name + ': ' + c.error_value, min(width, 48))}")
    else:
        if c.stdout_lines:
            parts.append(f"out {fmt_int(c.stdout_lines)}L")
        if c.images:
            img_s = ", ".join(f"{m} {fmt_bytes(sz)} omitted" for m, sz in c.images[:2])
            parts.append(img_s)
    lead = q(c.first_line or "(empty)", min(width, 52))
    return f"  {mark}#{c.index} {tag} {lead} ({' · '.join(parts)})"


def outline_file(
    path_str: str,
    policy: Any,
    *,
    repo_rel: str | None = None,
    max_bytes: int = 8 * 1024 * 1024,
    max_cells: int = 36,
) -> str | None:
    """Bounded cell outline for whole-file reads of ``.ipynb`` notebooks."""
    try:
        with open(path_str, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return None
    if not data or len(data) > max_bytes or b"\x00" in data[:4096]:
        return None
    doc = parse_notebook(data.decode("utf-8", "replace"))
    if doc is None:
        return None
    q = Quoter(68, policy)
    lines: list[str] = []
    meta_bits = [f"nbformat v{doc.nbformat}"]
    if doc.kernel:
        meta_bits.append(f"kernel={q(doc.kernel, 36)}")
    if doc.language:
        meta_bits.append(f"lang={q(doc.language, 24)}")
    lines.append("notebook: " + " · ".join(meta_bits))
    census = [
        _plural(len(doc.cells), "cell"),
        f"{fmt_int(doc.md_count)} md",
        f"{fmt_int(doc.code_count)} code",
    ]
    if doc.raw_count:
        census.append(f"{fmt_int(doc.raw_count)} raw")
    if doc.error_count:
        census.append(_plural(doc.error_count, "error"))
    if doc.image_count:
        census.append(
            f"{_plural(doc.image_count, 'embedded image')} "
            f"({fmt_bytes(doc.image_bytes)} base64 omitted)"
        )
    lines.append("summary: " + " · ".join(census))
    cap = bounds.count(max_cells)
    kept = sorted(_cell_priority(doc.cells, set())[:cap])
    lines.append(f"cells ({len(kept)} of {fmt_int(len(doc.cells))} shown):")
    for idx in kept:
        lines.append(_format_cell_row(doc.cells[idx - 1], q, 64))
    omitted = len(doc.cells) - len(kept)
    if omitted > 0:
        lines.append(f"  … +{fmt_int(omitted)} more cells omitted")
    if repo_rel:
        lines.append(f"next: ctx get repo:{repo_rel} --part cell:1")
        if len(doc.cells) > 1:
            lines.append(f"next: ctx get repo:{repo_rel} --part cells:1:{min(5, len(doc.cells))}")
    lines.extend(q.note())
    return "\n".join(lines)


def extract_notebook_part(
    data: bytes,
    spec: str,
    *,
    max_lines: int = 240,
    max_bytes: int = 4800,
) -> tuple[str, str, str | None]:
    """Extract ``--part cell:N`` or ``--part cells:A:B`` from a Jupyter
    notebook with base64 images omitted."""
    from ctx.containers import ContainerError

    try:
        obj = loads_fast(data.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContainerError(f"content is not a valid Jupyter notebook: {exc}") from None
    if _parse_notebook_obj(obj) is None:
        raise ContainerError("content is not a Jupyter .ipynb notebook")
    raw_cells = obj["cells"]
    total = len(raw_cells)
    if spec.startswith("cell:"):
        num = spec[len("cell:"):]
        if not num.isdigit():
            raise ContainerError(f"invalid notebook selector {spec!r}; use cell:<N> or cells:<A>:<B>")
        a = b = int(num)
    elif spec.startswith("cells:"):
        rest = spec[len("cells:"):]
        m = re.fullmatch(r"(\d+):(\d+)", rest)
        if not m:
            raise ContainerError(f"invalid notebook selector {spec!r}; use cells:<A>:<B>")
        a, b = int(m.group(1)), int(m.group(2))
    else:
        raise ContainerError(f"unknown notebook --part {spec!r}; use cell:<N> or cells:<A>:<B>")

    win = bounds.span(a, b, total)
    if win is None:
        raise ContainerError(f"cell range {a}:{b} out of range (notebook has {total} cells)")
    a, b = win

    rendered: list[str] = []
    used = 0
    shown_end = a - 1
    for idx in range(a, b + 1):
        cell = raw_cells[idx - 1]
        ctype = str(cell.get("cell_type") or "raw")
        ec = cell.get("execution_count")
        tag = f"{ctype}:{ec}" if ctype == "code" and isinstance(ec, int) else ctype
        block = [f"=== Cell #{idx} [{tag}] ==="]
        src = _join_source(cell.get("source"))
        if src:
            block.extend(src.splitlines())
        outputs = cell.get("outputs")
        if isinstance(outputs, list) and outputs:
            block.append("--- outputs ---")
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                otype = str(item.get("output_type") or "")
                if otype == "stream":
                    stxt = _join_source(item.get("text"))
                    if stxt:
                        block.extend(itertools.islice(stxt.splitlines(), 40))
                elif otype == "error":
                    ename = str(item.get("ename") or "Error")
                    evalue = _ANSI_STRIP_RE.sub("", str(item.get("evalue") or ""))
                    block.append(f"ERROR {ename}: {evalue}")
                    tb = item.get("traceback")
                    if isinstance(tb, list):
                        for tln in itertools.islice(tb, 20):
                            block.append(_ANSI_STRIP_RE.sub("", str(tln)))
                elif otype in ("display_data", "execute_result"):
                    dmap = item.get("data")
                    if isinstance(dmap, dict):
                        for mime in _BINARY_MIMES:
                            if mime in dmap:
                                sz = len(_join_source(dmap[mime]))
                                block.append(f"[{mime} {fmt_bytes(sz)} base64 omitted]")
                        if "text/plain" in dmap:
                            ptxt = _join_source(dmap["text/plain"])
                            if ptxt:
                                block.extend(itertools.islice(ptxt.splitlines(), 30))
        block_text = "\n".join(block)
        cost = len(block_text.encode("utf-8", "replace")) + 1
        if rendered and used + cost > max_bytes:
            break
        rendered.append(block_text)
        used += cost
        shown_end = idx

    if shown_end < a:
        shown_end = a
    cont = None
    if shown_end < total:
        span = shown_end - a + 1
        cont = f"cells:{shown_end + 1}:{min(total, shown_end + (span if span > 0 else 5))}"
    return f"cells:{a}:{shown_end} of {fmt_int(total)}", "\n\n".join(rendered), cont


class NotebookProfile(Profile):
    version = "notebook/v1"

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
        if not view.parsed_fully:
            return None
        if view.bytes <= inline_limit(ctx):
            return None
        doc = parse_notebook(view.text)
        if doc is None:
            return None
        return f"jupyter notebook v{doc.nbformat}: {len(doc.cells)} cells"

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        doc = parse_notebook(view.text)
        if doc is None:
            return "\n".join(list(ctx.header_lines()) + ["notebook: unreadable"])
        handle = "run:PENDING#stdout"
        rid = "run:PENDING"
        policy = ctx.ws.config.redaction

        focus_hits: set[int] = set()
        if ctx.focus_terms:
            for c in doc.cells:
                if any(t in c.searchable for t in ctx.focus_terms):
                    focus_hits.add(c.index)

        priority = _cell_priority(doc.cells, focus_hits)

        def build(level: int) -> str:
            max_cells, width = ((26, 68), (16, 56), (10, 44), (6, 36), (3, 28))[level]
            q = Quoter(width, policy)
            out = (
                [ctx.intake_line(total=False) + " · notebook"]
                if from_intake(ctx)
                else list(ctx.header_lines())
            )
            meta_bits = [f"nbformat v{doc.nbformat}"]
            if doc.kernel:
                meta_bits.append(f"kernel={q(doc.kernel, 36)}")
            if doc.language:
                meta_bits.append(f"lang={q(doc.language, 24)}")
            out.append("notebook: " + " · ".join(meta_bits))
            census = [
                _plural(len(doc.cells), "cell"),
                f"{fmt_int(doc.md_count)} md",
                f"{fmt_int(doc.code_count)} code",
            ]
            if doc.raw_count:
                census.append(f"{fmt_int(doc.raw_count)} raw")
            if doc.error_count:
                census.append(_plural(doc.error_count, "error"))
            if doc.image_count:
                census.append(
                    f"{_plural(doc.image_count, 'embedded image')} "
                    f"({fmt_bytes(doc.image_bytes)} base64 omitted)"
                )
            out.append("summary: " + " · ".join(census))

            kept = sorted(priority[:max_cells])
            focus_note = " (* matches focus)" if focus_hits else ""
            out.append(f"cells ({len(kept)} of {fmt_int(len(doc.cells))} shown){focus_note}:")
            for idx in kept:
                c = doc.cells[idx - 1]
                mark = "* " if c.index in focus_hits else ""
                out.append(_format_cell_row(c, q, width, mark=mark))
            omitted = len(doc.cells) - len(kept)
            if omitted > 0:
                out.append(f"  … +{fmt_int(omitted)} more cells omitted")
            out.append(
                f"coverage: all {fmt_int(len(doc.cells))} cells indexed · "
                f"{len(kept)}/{fmt_int(len(doc.cells))} shown"
            )

            sugg: list[str] = []
            if doc.cells:
                first_err = next((c.index for c in doc.cells if c.error_name), None)
                target = first_err if first_err is not None else (sorted(focus_hits)[0] if focus_hits else 1)
                sugg.append(f"ctx get {handle} --part cell:{target}")
                if len(doc.cells) > 1:
                    sugg.append(f"ctx get {handle} --part cells:1:{min(5, len(doc.cells))}")
            sugg.append(f"ctx search {rid} '<pattern>' --context 2")
            return "\n".join(out + self.next_lines(ctx, sugg) + q.note())

        return fit(ctx, build, 5)


__all__ = [
    "CellSummary",
    "NotebookDoc",
    "NotebookProfile",
    "extract_notebook_part",
    "outline_file",
    "parse_notebook",
]
