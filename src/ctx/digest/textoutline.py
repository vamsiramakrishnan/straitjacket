"""Structural PreToolUse outlines for large plain-text documents (`.txt`).

Recognizes four universal content shapes without external dependencies:
  1. Aligned columnar tables (ALL-CAPS multi-column header + aligned rows).
  2. Delimited section documents (`--- Title ---`, `=== Title ===`, `*** Title ***`).
  3. Key-value record streams (repeated `Field: value` blocks separated by dividers
     or blank lines, such as issue/ticket/incident CLI listings).
  4. Timestamped message threads (repeated author + ISO-8601 timestamp headers).

Strict predicates ensure unstructured prose and uniform log streams return None,
leaving the ordinary line-window path untouched.
"""

from __future__ import annotations

import re
from collections import Counter

from ctx import bounds
from ctx.digest.intakeprof import mappable_text
from ctx.digest.shapeutil import LINE_EXAMINE_CAP, Quoter, examine_line
from ctx.digest.tableprof import _HEADER_CELL_RE, _MIN_ROWS as _TABLE_MIN_ROWS
from ctx.textutil import fmt_bytes, fmt_int, mask_hits, secret_hits

_SECTION_DIV_RE = re.compile(
    r"^[ \t]*(?:-{2,}|={2,}|\*{3,})[ \t]+(.+?)[ \t]+(?:-{2,}|={2,}|\*{3,})[ \t]*$"
)
_KV_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _./-]{1,28}):[ \t]+(.+)$")
_RECORD_SEP_RE = re.compile(r"^[ \t]*(?:-{3,}|={3,}|\*{3,})[ \t]*$")
_ISO_TS_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?)\b"
)
_THREAD_BOLD_RE = re.compile(
    r"^\*\*([^*]{1,48})\*\*[ \t]+(?:at|on)[ \t]+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_THREAD_BRACKET_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[^\]]{0,16})\][ \t]+([^:\n]{1,48}):[ \t]*(.*)$"
)
_THREAD_AUTHOR_PAREN_RE = re.compile(
    r"^([A-Za-z0-9_.@-][A-Za-z0-9 _.@-]{0,40})[ \t]+\((\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[^)]{0,16})\):[ \t]*(.*)$"
)

_DETAIL_KEYS = ("title", "subject", "summary", "name", "status", "priority", "state")


def _plural(n: int, word: str) -> str:
    return f"{fmt_int(n)} {word}" if n == 1 else f"{fmt_int(n)} {word}s"


def _sample_indices(total: int, cap: int) -> list[int]:
    """Deterministic head + evenly spaced + tail index selection (0-based)."""
    if total <= cap:
        return list(range(total))
    if cap <= 2:
        return [0, total - 1][:cap]
    head_n = min(6, cap // 3)
    tail_n = min(4, cap // 4)
    mid_n = cap - head_n - tail_n
    picked: list[int] = list(range(head_n))
    if mid_n > 0:
        span_start = head_n
        span_end = total - tail_n
        step = (span_end - span_start) / (mid_n + 1)
        for i in range(1, mid_n + 1):
            idx = int(span_start + i * step)
            if span_start <= idx < span_end and idx not in picked:
                picked.append(idx)
    for idx in range(total - tail_n, total):
        if idx not in picked:
            picked.append(idx)
    return sorted(picked)


_UNDERLINE_CELL_RE = re.compile(r"(?:-{3,}|={3,})")


def _split_caps_header(line: str) -> list[tuple[int, str]]:
    cells: list[tuple[int, str]] = []
    for m in re.finditer(r"\S+(?: \S+)*", line):
        if not _HEADER_CELL_RE.fullmatch(m.group(0)):
            return []
        cells.append((m.start(), m.group(0)))
    return cells


def _split_underlined_header(hdr_line: str, sep_line: str) -> list[tuple[int, str]]:
    """Parse a mixed-case columnar header underlined by `---  ---  ---`."""
    if not sep_line.strip():
        return []
    # Every non-whitespace token on the underline row must be dashes/equals.
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", sep_line)]
    if len(spans) < 2:
        return []
    for a, b in spans:
        if not _UNDERLINE_CELL_RE.fullmatch(sep_line[a:b]):
            return []
    cells: list[tuple[int, str]] = []
    for idx, (a, _b) in enumerate(spans):
        next_a = spans[idx + 1][0] if idx + 1 < len(spans) else len(hdr_line)
        name = hdr_line[a:next_a].strip()
        if not name:
            return []
        cells.append((a, name))
    return cells


def _outline_aligned_table(
    lines: list[str], total: int, partial: bool, q: Quoter
) -> str | None:
    hdr_idx = 0
    while hdr_idx < min(total, 5) and not lines[hdr_idx].strip():
        hdr_idx += 1
    if hdr_idx >= total:
        return None
    data_start = hdr_idx + 1
    cells = (
        _split_underlined_header(lines[hdr_idx], lines[hdr_idx + 1])
        if hdr_idx + 1 < total
        else []
    )
    if cells:
        data_start = hdr_idx + 2
    else:
        cells = _split_caps_header(lines[hdr_idx])
        if len(cells) < 3:
            return None
    rows = [
        (i, ln)
        for i, ln in enumerate(lines[data_start:], start=data_start + 1)
        if ln.strip()
    ]
    if len(rows) < _TABLE_MIN_ROWS:
        return None
    offsets = [start for start, _ in cells[1:]]
    min_cols = len(offsets) - 1 if len(offsets) > 1 else 1
    aligned = sum(
        1
        for _, ln in rows
        if sum(1 for o in offsets if o < len(ln) and ln[o] != " ") >= min_cols
    )
    if aligned < len(rows) * 0.7:
        return None

    names = [name for _, name in cells]
    starts = [start for start, _ in cells]

    def cell(ln: str, col: int) -> str:
        a = starts[col]
        b = starts[col + 1] if col + 1 < len(starts) else len(ln)
        return ln[a:b].strip()

    out = [
        f"aligned table: {_plural(len(rows), 'row')}"
        + (" (parsed)" if partial else "")
        + f" × {len(names)} columns · {_plural(total, 'line')}",
        "columns: " + " · ".join(q(n, 32) for n in names[:12]),
    ]
    shown_hist = 0
    for col, name in enumerate(names):
        if shown_hist >= 3:
            break
        counts: Counter[str] = Counter(cell(ln, col) for _, ln in rows)
        counts.pop("", None)
        if 2 <= len(counts) <= 8 and len(counts) <= len(rows) // 3:
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            out.append(
                f"  {q(name, 24)}: "
                + " · ".join(f"{q(v, 28)} ({fmt_int(n)})" for v, n in ranked[:6])
            )
            shown_hist += 1

    # Date span if any column holds ISO dates/timestamps.
    dates: list[str] = []
    for _, ln in (rows[0], rows[-1]):
        m = _ISO_TS_RE.search(examine_line(ln))
        if m:
            dates.append(m.group(1))
    if len(dates) == 2 and dates[0] != dates[1]:
        out.append(f"time span (first → last row): {dates[0]} → {dates[1]}")

    def fmt_row(ln_no: int, ln: str) -> str:
        parts = [cell(ln, c) for c in range(min(len(names), 4))]
        parts = [q(p, 48) for p in parts if p]
        return f"  L{ln_no} " + " | ".join(parts)

    out.append("first rows:")
    for ln_no, ln in rows[:3]:
        out.append(fmt_row(ln_no, ln))
    if len(rows) > 6:
        out.append(f"  … +{fmt_int(len(rows) - 6)} middle rows omitted")
    out.append("last rows:")
    for ln_no, ln in rows[-3:]:
        out.append(fmt_row(ln_no, ln))
    out.extend(q.note())
    return "\n".join(out)


def _outline_delimited_sections(
    lines: list[str], total: int, partial: bool, q: Quoter
) -> str | None:
    hits: list[tuple[int, str]] = []
    for idx, raw in enumerate(lines, 1):
        head = examine_line(raw)
        m = _SECTION_DIV_RE.match(head)
        if m:
            title = m.group(1).strip()
            if any(c.isalnum() for c in title):
                hits.append((idx, title))
    if len(hits) < 3:
        return None
    # Verify dividers actually partition text rather than decorating every line.
    if len(hits) > total // 2:
        return None

    sections: list[tuple[int, int, str, str]] = []
    for k, (start_ln, title) in enumerate(hits):
        end_ln = hits[k + 1][0] - 1 if k + 1 < len(hits) else total
        if end_ln < start_ln:
            end_ln = start_ln
        preview = ""
        for j in range(start_ln, min(end_ln, start_ln + 4)):
            s = examine_line(lines[j]).strip()
            if s:
                preview = s
                break
        sections.append((start_ln, end_ln, title, preview))

    cap = bounds.count(28)
    picked = _sample_indices(len(sections), cap)
    out = [
        f"sectioned text: {_plural(len(sections), 'section')}"
        + (" (parsed)" if partial else "")
        + f" · {_plural(total, 'line')}",
        f"sections ({len(picked)} of {fmt_int(len(sections))} shown):",
    ]
    prev_i = -1
    for i in picked:
        if prev_i >= 0 and i > prev_i + 1:
            out.append(f"  … +{fmt_int(i - prev_i - 1)} sections omitted")
        a, b, title, preview = sections[i]
        tail = f" · {q(preview, 56)}" if preview else ""
        out.append(f"  L{a}-L{b} {q(title, 64)}{tail}")
        prev_i = i
    out.extend(q.note())
    return "\n".join(out)


def _outline_kv_records(
    lines: list[str], total: int, partial: bool, q: Quoter
) -> str | None:
    # Find repeated opening `Key: value` lines that begin records after a
    # separator (`---`/`===`), blank line, or start of file (after an optional
    # 1-line summary banner).
    candidates: list[tuple[int, str, str]] = []
    prev_boundary = True
    for idx, raw in enumerate(lines, 1):
        head = examine_line(raw)
        s = head.strip()
        if not s or _RECORD_SEP_RE.match(head):
            prev_boundary = True
            continue
        m = _KV_LINE_RE.match(head)
        if m and prev_boundary:
            candidates.append((idx, m.group(1).strip(), m.group(2).strip()))
            prev_boundary = False
        elif idx == 1 and not m:
            # Optional 1-line preamble such as "Showing first 49 issue(s)..."
            prev_boundary = True
        else:
            prev_boundary = False

    if len(candidates) < 3:
        return None
    key_counts = Counter(k for _, k, _ in candidates)
    primary_key, count = key_counts.most_common(1)[0]
    if count < 3:
        return None

    starts = [(idx, val) for idx, k, val in candidates if k == primary_key]
    all_fields: Counter[str] = Counter()
    records: list[tuple[int, int, str, str]] = []
    for k, (start_ln, pval) in enumerate(starts):
        end_ln = starts[k + 1][0] - 1 if k + 1 < len(starts) else total
        while end_ln > start_ln and (
            not lines[end_ln - 1].strip()
            or _RECORD_SEP_RE.match(examine_line(lines[end_ln - 1]))
        ):
            end_ln -= 1
        detail_map: dict[str, str] = {}
        for j in range(start_ln, min(end_ln, start_ln + 18)):
            m = _KV_LINE_RE.match(examine_line(lines[j]))
            if m:
                fk = m.group(1).strip()
                all_fields[fk] += 1
                fl = fk.lower()
                if fl in _DETAIL_KEYS and fl not in detail_map:
                    detail_map[fl] = m.group(2).strip()
        extras = []
        for dk in ("status", "priority", "state", "title", "subject", "summary", "name"):
            if dk in detail_map:
                extras.append(q(detail_map[dk], 56))
        records.append((start_ln, end_ln, pval, " · ".join(extras[:2])))

    top_fields = [primary_key] + [
        f for f, _ in all_fields.most_common(8) if f != primary_key
    ]
    cap = bounds.count(20)
    picked = _sample_indices(len(records), cap)
    out = [
        f"record stream: {_plural(len(records), 'record')} keyed by {q(primary_key, 24)}"
        + (" (parsed)" if partial else "")
        + f" · {_plural(total, 'line')}",
    ]
    # Surface line-1 preamble banner if present before record 1.
    if starts[0][0] > 1:
        preamble = examine_line(lines[0]).strip()
        if preamble and not _RECORD_SEP_RE.match(preamble):
            out.append(f"header: {q(preamble, 96)}")
    out.append("fields: " + " · ".join(q(f, 24) for f in top_fields[:8]))
    out.append(f"records ({len(picked)} of {fmt_int(len(records))} shown):")
    prev_i = -1
    for i in picked:
        if prev_i >= 0 and i > prev_i + 1:
            out.append(f"  … +{fmt_int(i - prev_i - 1)} records omitted")
        a, b, pval, extra = records[i]
        tail = f" · {extra}" if extra else ""
        out.append(f"  L{a}-L{b} {q(pval, 36)}{tail}")
        prev_i = i
    out.extend(q.note())
    return "\n".join(out)


def _outline_timestamped_thread(
    lines: list[str], total: int, partial: bool, q: Quoter
) -> str | None:
    entries: list[tuple[int, str, str, str]] = []  # (line, speaker, ts, inline_msg)
    for idx, raw in enumerate(lines, 1):
        head = examine_line(raw)
        m = _THREAD_BOLD_RE.match(head)
        if m:
            entries.append((idx, m.group(1).strip(), m.group(2).strip(), ""))
            continue
        m = _THREAD_BRACKET_RE.match(head)
        if m:
            entries.append((idx, m.group(2).strip(), m.group(1).strip(), m.group(3).strip()))
            continue
        m = _THREAD_AUTHOR_PAREN_RE.match(head)
        if m:
            entries.append((idx, m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))
            continue
    if len(entries) < 3:
        return None

    speakers = Counter(sp for _, sp, _, _ in entries)
    ts_first = entries[0][2]
    ts_last = entries[-1][2]
    items: list[tuple[int, int, str, str, str]] = []
    for k, (start_ln, sp, ts, inline_msg) in enumerate(entries):
        end_ln = entries[k + 1][0] - 1 if k + 1 < len(entries) else total
        snippet = inline_msg
        if not snippet:
            for j in range(start_ln, min(end_ln, start_ln + 4)):
                s = examine_line(lines[j]).strip()
                if s and not s.startswith("[link]("):
                    snippet = s
                    break
        items.append((start_ln, end_ln, sp, ts, snippet))

    cap = bounds.count(20)
    picked = _sample_indices(len(items), cap)
    sp_summary = ", ".join(f"{q(s, 24)} ({n})" for s, n in speakers.most_common(5))
    out = [
        f"message thread: {_plural(len(items), 'message')}"
        + (" (parsed)" if partial else "")
        + f" · {_plural(total, 'line')}",
        f"time span: {ts_first} → {ts_last}",
        f"participants: {sp_summary}",
        f"messages ({len(picked)} of {fmt_int(len(items))} shown):",
    ]
    prev_i = -1
    for i in picked:
        if prev_i >= 0 and i > prev_i + 1:
            out.append(f"  … +{fmt_int(i - prev_i - 1)} messages omitted")
        a, b, sp, ts, snippet = items[i]
        tail = f" {q(snippet, 64)}" if snippet else ""
        out.append(f"  L{a}-L{b} {ts} {q(sp, 24)}:{tail}")
        prev_i = i
    out.extend(q.note())
    return "\n".join(out)


def outline_file(
    path_str: str,
    policy,
    *,
    max_bytes: int = 2 * 1024 * 1024,
) -> str | None:
    """Bounded structural outline for a whole-file read of a large plain-text
    file, or None when the file lacks a recognized structured shape."""
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
    raw_text = mappable_text(data.decode("utf-8", "replace"), not partial)
    # Redact multiline secrets (such as PEM private keys) BEFORE slicing into
    # per-line previews while preserving exact line counts.
    hits = secret_hits(raw_text, 0, len(raw_text), policy=policy)
    redacted_text = mask_hits(raw_text, 0, len(raw_text), hits) if hits else raw_text
    lines = redacted_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total < 10:
        return None

    q = Quoter(72, policy)
    for builder in (
        _outline_aligned_table,
        _outline_delimited_sections,
        _outline_kv_records,
        _outline_timestamped_thread,
    ):
        res = builder(lines, total, partial, q)
        if res:
            return res
    return None
