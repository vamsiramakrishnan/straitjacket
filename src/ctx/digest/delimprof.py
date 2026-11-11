"""delimited/v1 -- a purpose-built view of pasted CSV / TSV / semicolon / pipe data.

Applies ONLY to manifests created by :mod:`ctx.intake` (gated on
:func:`ctx.digest.base.from_intake`), and only when the payload is too large
for ``text/v1`` to inline. Command output never reaches this profile, so every
existing command digest stays byte-identical.

Decision the digest supports: *what are the columns, how many rows, and what do
the values look like?* The loss that would make it unsafe: a wrong row or
column count, statistics presented as exact when they cover a sample, or a cell
(a secret, text imitating the digest's own structure) reaching the model
unquoted.

The delimiter is found from the data, never from a file name: each of comma,
tab, semicolon and pipe is tried on a bounded head sample with the standard
library's :mod:`csv` reader (so quoting, doubled quotes and multi-line cells
are handled), and a delimiter is accepted only when the same number of fields
appears in at least :data:`CONSISTENCY_PERCENT` of the sampled rows.

Shape::

    intake: <label> ... · delimited data
    delimited: <comma|tab|semicolon|pipe> · <C> columns · <header row detected|no header row> (heuristic) · <R> data rows
    statistics: all <R> rows | sample of <k> of <R> rows (every <s>th, deterministic) ...
    columns:
      1 "id" · int · empty 0% · range 1 .. 50000
      2 "status" · text · empty 2% · 4 distinct: "ok" 70%, "fail" 20% ...
    first rows: L1-L4 · span <id>
      L1 "id" | "status" | ...
    last row: L<n> ...
    coverage: ...
    next:
      ctx get <handle> --lines 1:21

Row counts and column counts come from one pass over the whole parsed payload
(exact); per-column statistics come from at most :data:`MAX_SAMPLE_ROWS`
deterministically decimated rows, and the digest says which. Every cell is
quoted by :class:`ctx.digest.shapeutil.Quoter` (single line, control
characters removed, clipped, with the clipping declared), and the digest is
fitted to its budget on the text as it is emitted.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction

from ctx import bounds
from ctx.digest.base import MAX_PARSE_BYTES, DigestContext, Profile, from_intake
from ctx.digest.intakeprof import mappable_text
from ctx.digest.shapeutil import FIELD_KEEP, Quoter, Windows, ascii_digits, fit, inline_limit, pct
from ctx.textutil import _ANSI_RE, fmt_bytes, fmt_int

DELIMITERS = (",", "\t", ";", "|")
DELIMITER_NAMES = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}
#: Characters of the payload examined to decide the delimiter.
SAMPLE_CHARS = 64 * 1024
#: Rows the delimiter test needs, and the share of them that must agree on
#: one field count.
MIN_SAMPLE_ROWS = 5
CONSISTENCY_PERCENT = 95
#: Per-column statistics are computed over at most this many rows (kept by
#: deterministic decimation, never random sampling) and at most this many
#: stored cells, whichever is smaller.
MAX_SAMPLE_ROWS = 10_000
MAX_SAMPLE_CELLS = 400_000
MAX_COLUMNS = 200
#: Longest cell the TYPE of a cell is read from; a longer one is text.
CELL_KEEP = 64
#: A sampled cell is stored whole up to this many characters; a longer one is
#: stored as its first part plus its length and a digest of the whole, so two
#: values that share a prefix stay two values.
SAMPLE_CELL_KEEP = 256
#: How many leading records are looked at to find where the table starts.
PREAMBLE_LOOKAHEAD = 64
#: Distinct values counted per column before the count is declared a floor.
DISTINCT_CAP = 1000
#: A text column with at most this many distinct values shows its top values.
LOW_CARDINALITY = 12
_ISO_LOG_PREFIX = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d", re.ASCII)

_INT = re.compile(r"[-+]?\d{1,40}", re.ASCII)
_FLOAT = re.compile(
    r"[-+]?(?:\d{1,40}\.\d{0,40}|\.\d{1,40})(?:[eE][-+]?\d{1,4})?|[-+]?\d{1,40}[eE][-+]?\d{1,4}",
    re.ASCII,
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[-+]\d{2}:?\d{2})?",
    re.ASCII,
)
_BOOLS = frozenset({"true", "false"})


# ----------------------------------------------------------------- reading
def physical_lines(text: str):
    """The payload's lines for :mod:`csv`, split on ``\\n`` only (the store's
    line index), each ending in ``\\n``. A trailing ``\\r`` (CRLF) is dropped
    and a stray inner ``\\r`` becomes a space, so ``reader.line_num`` counts
    exactly the lines ``ctx get --lines`` counts."""
    pos, n = 0, len(text)
    first = True
    while pos < n:
        j = text.find("\n", pos)
        piece = text[pos:] if j < 0 else text[pos:j]
        pos = n if j < 0 else j + 1
        if first:
            piece = piece.removeprefix("\ufeff")
            first = False
        if "\r" in piece:
            piece = piece[:-1] if piece.endswith("\r") else piece
            piece = piece.replace("\r", " ")
        yield piece + "\n"


def read_records(lines, delimiter: str):
    """Yield ``(row | None, first_line, last_line)`` for each record of the
    CSV reading of ``lines``; ``None`` marks a record the parser rejected (a
    field past the reader's size limit, say). Progress is guaranteed: every
    ``next`` consumes at least one source line."""
    reader = csv.reader(lines, delimiter=delimiter)
    prev = 0
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error:
            row = None
        end = reader.line_num
        yield row, prev + 1, end
        prev = end


def _is_blank(row: list[str] | None) -> bool:
    return row is not None and (not row or (len(row) == 1 and not row[0].strip()))


# --------------------------------------------------------------- detection
@dataclass(frozen=True)
class Candidate:
    delimiter: str
    columns: int       # modal field count
    consistent: int    # sampled rows with exactly that many fields
    rows: int          # sampled rows (blank lines excluded)


def _value_like(cell: str) -> bool:
    """A cell that is a VALUE rather than a fragment of text: a number, date or
    boolean, or an email address, URL, IPv4 address or UUID."""
    c = cell.strip()
    if not c or len(c) > 254:
        return False
    if cell_kind(c) in ("int", "float", "date", "datetime", "bool"):
        return True
    if "://" in c and " " not in c:
        return True
    if "@" in c and " " not in c:
        domain = c.rsplit("@", 1)[1]
        return "." in domain and not domain.startswith(".") and not domain.endswith(".")
    parts = c.split(".")
    if len(parts) == 4 and all(ascii_digits(x) and len(x) <= 3 for x in parts):
        return True
    return len(c) == 36 and c.count("-") == 4 and all(ch in "0123456789abcdefABCDEF-" for ch in c)


#: A bracketed time (``[12:00:01]``, ``[2024-01-02 10:00]``) or level tag
#: (``[INFO]``) opening a line.
_LOG_TAG = re.compile(
    r"\[(?:\d{1,4}[-/]\d\d[-/]\d\d[^\]]{0,40}|\d\d:\d\d(?::\d\d)?[^\]]{0,40}"
    r"|(?:trace|debug|info|notice|warn|warning|error|err|fatal|critical|crit)\b[^\]]{0,20})\]",
    re.ASCII | re.IGNORECASE,
)


def _log_prefix(first_cell: str) -> bool:
    """A log line opens with a timestamp or a bracketed level tag, whatever
    delimiter also appears in it. An interval bin (``[0-5)``) is a VALUE in a
    column, closes with another bracket type and holds no time or level."""
    head = first_cell.lstrip()
    return bool(_ISO_LOG_PREFIX.match(head) or _LOG_TAG.match(head))


def _unbalanced(cell: str) -> bool:
    """A bracket, brace or parenthesis opened in ``cell`` and not closed in
    it (or closed before it was opened). Types are not matched against each
    other, so a half-open interval such as ``[0-5)`` is balanced."""
    depth = 0
    for ch in _ANSI_RE.sub("", cell[:SAMPLE_CELL_KEEP]):   # terminal colour is not a bracket
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return True
    return depth != 0


def _markdown_table(sample: str, delimiter: str) -> bool:
    """A Markdown table without leading pipes: its second line is only
    dashes, colons, pipes and spaces."""
    lines = [ln.strip() for ln in sample.split("\n", 4)[:3] if ln.strip()]
    return (delimiter == "|" and len(lines) >= 2 and "-" in lines[1]
            and all(ch in "-:| " for ch in lines[1]))


def _code_wrapper(sample: str) -> bool:
    """The sample opens like a statement that takes a list (``... = {``,
    ``INSERT ... VALUES``) or closes like one (``};``, ``);``)."""
    lines = [ln.strip() for ln in sample.split("\n") if ln.strip()]
    if not lines:
        return False
    first, last = lines[0], lines[-1]
    return (first.endswith(("{", "[", "(", "=", ":")) or first.lower().endswith(" values")
            or last in ("};", ");", "];", "}", ")", "]", "},", "),"))


def _tabular(records, columns: int, sample: str, delimiter: str = ",") -> bool:
    """Is this a table of values, as opposed to code, SQL, config or prose
    that happens to have the same number of commas on every line?

    The field count being consistent across 95% of many rows is already
    required; on top of that, a reading is a table only when it shows
    EVIDENCE OF VALUES, any one of:

    * **typed values**: at least a tenth of the non-empty cells are numbers,
      dates, booleans, emails, URLs, IPv4 addresses or UUIDs;
    * **a categorical column**: some column repeats a few values (two to a
      quarter of the rows distinct) -- text that is prose or code does not
      repeat its cells;
    * **a header of another kind than the cells under it**: a first record of
      distinct short labels (at most three words, 32 characters) over a column
      whose typical cell is at least twice as long as its label.

    and none of these code signals:

    * a **code wrapper** (:func:`_code_wrapper`) around the rows;
    * **unbalanced cells**: in real delimited data a bracket, brace or
      parenthesis opened in a cell is closed in the same cell (or the cell is
      quoted); splitting code on its commas leaves ``draw_rect(ctx`` and
      ``(1`` and ``[a`` behind. A quarter of the rows having one is enough;
    * a Markdown table's dashes-and-pipes second line.

    The first characters of a cell decide nothing: a trailing delimiter on
    every row, cells that start with a bracket, ``x=`` or ``--``, interval
    bins like ``[0-5)`` and lowercase words that are also keywords are all
    ordinary in data.
    """
    if _code_wrapper(sample) or _markdown_table(sample, delimiter):
        return False
    rows = [r for r, _, _ in records if r is not None and len(r) == columns]
    probe = rows[:500]
    if len(probe) < MIN_SAMPLE_ROWS:
        return False
    # Bracket balance means something for text that was split on the delimiter
    # UNQUOTED; a quoted free-text comment (":)", "(see note", "1)") is free to
    # hold any brackets. So only rows whose raw lines hold no quote character
    # are counted.
    raw = sample.split("\n")
    bare = [r for r, a, b in records
            if r is not None and len(r) == columns and '"' not in "\n".join(raw[a - 1:b])][:500]
    if len(bare) >= MIN_SAMPLE_ROWS and sum(1 for r in bare if any(_unbalanced(c) for c in r)) * 4 >= len(bare):
        return False
    cells = [c for r in probe for c in r if c.strip()]
    if cells and sum(1 for c in cells if _value_like(c)) * 10 >= len(cells):
        return True
    if len(probe) >= 40:
        for j in range(columns):
            col = {r[j].strip() for r in probe if r[j].strip()}
            if len(col) >= 2 and len(col) * 4 <= len(probe):
                return True
    first, body = probe[0], probe[1:]
    labels = [c.strip() for c in first]
    if (all(labels) and len(set(labels)) == len(labels)
            and all(len(c) <= 32 and len(c.split()) <= 3 and _LABEL.fullmatch(c) for c in labels)):
        for j, label in enumerate(labels):
            lengths = sorted(len(r[j].strip()) for r in body if r[j].strip())
            if lengths and lengths[len(lengths) // 2] >= 2 * len(label):
                return True
    return False


@functools.lru_cache(maxsize=2)
def sniff(text: str) -> tuple[Candidate, ...]:
    """Every delimiter that fits the head of ``text``, best first.

    Linear in :data:`SAMPLE_CHARS`. A payload whose first line is longer than
    the sample, log-timestamped rows, rows
    that all begin with the delimiter (indented text, a markdown-style pipe
    table) or that show no evidence of values (:func:`_tabular`) is not delimited
    data here. Ranking: the larger share of rows with the common field count
    first (compared as a fraction, exactly), then more columns.
    """
    if len(text) > SAMPLE_CHARS:
        cut = text.rfind("\n", 0, SAMPLE_CHARS)
        if cut < 0:
            return ()
        sample = text[: cut + 1]
        truncated = True
    else:
        sample, truncated = text, False
    found: list[Candidate] = []
    for d in DELIMITERS:
        if d not in sample:
            continue
        counts: Counter[int] = Counter()
        rows = empty_first = logish = 0
        records = [r for r in read_records(physical_lines(sample), d) if not _is_blank(r[0])]
        if truncated and records:
            records.pop()   # the last record may be cut by the sample boundary
        for row, _, _ in records:
            if row is None:
                continue
            rows += 1
            counts[len(row)] += 1
            if row and not row[0].strip():
                empty_first += 1
            if row and _log_prefix(row[0]):
                logish += 1
        if rows < MIN_SAMPLE_ROWS:
            continue
        columns, consistent = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        if columns < 2 or consistent * 100 < CONSISTENCY_PERCENT * len(records):
            continue
        if logish * 2 > rows:
            continue
        if empty_first * 10 >= rows * 9:
            continue   # a leading delimiter on every row: indentation, or a markdown-style pipe table
        if not _tabular(records, columns, sample, d):
            continue
        found.append(Candidate(d, columns, consistent, rows))
    # Fractions compared exactly, never floored: flooring a negative made
    # 99.67% tie with 100% and let tuple order pick the comma.
    return tuple(sorted(found, key=lambda c: (-Fraction(c.consistent, c.rows), -c.columns, DELIMITERS.index(c.delimiter))))


# ------------------------------------------------------------------ parsing
@dataclass
class Table:
    delimiter: str
    columns: int
    records: int = 0               # non-blank records, header row included
    blank_lines: int = 0
    rejected: int = 0              # records the csv reader refused
    first_rejected: int = 0
    ragged: int = 0                # records whose field count is not ``columns``
    first_ragged: int = 0
    min_fields: int = 0
    max_fields: int = 0
    first_rows: list[tuple[list[str], int, int]] = field(default_factory=list)
    last_row: tuple[list[str], int, int] | None = None
    sample: list[list[str]] = field(default_factory=list)   # decimated body rows
    stride: int = 1
    body_seen: int = 0             # body rows (all records after the first)
    lines: int = 0
    first_line: int = 0
    preamble: int = 0              # leading records skipped: their field count is not the table's
    preamble_first: int = 0        # first and last line of that preamble
    preamble_last: int = 0
    preamble_text: str = ""        # first field of the first preamble record, whole


FIRST_ROWS_KEPT = 4     # header candidate + 3 data rows
ROW_CELLS_KEPT = 12


def _stored(cell: str) -> str:
    """A sampled cell: whole up to :data:`SAMPLE_CELL_KEEP` characters, else
    its first part, its length and a digest of the whole -- so values that
    share a prefix remain distinct values."""
    if len(cell) <= SAMPLE_CELL_KEEP:
        return cell
    digest = hashlib.blake2b(cell.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()
    return f"{cell[:SAMPLE_CELL_KEEP]}\x00{len(cell)}:{digest}"


def _unstored(stored: str) -> tuple[str, bool, int]:
    """(text, was_cut, original length) of a :func:`_stored` cell."""
    head, nul, rest = stored.partition("\x00")
    if not nul:
        return stored, False, len(stored)
    length = rest.split(":", 1)[0]
    return head, True, int(length) if ascii_digits(length) else len(head)


def _body_records(records, columns: int, skipped: list):
    """``records`` with a leading preamble removed: the table starts at the
    first record that has the table's field count and is followed by another
    that does (looked for among the first :data:`PREAMBLE_LOOKAHEAD`
    non-blank records). What came before -- ``# Exported by ...`` lines, a
    title -- is appended to ``skipped``."""
    buf: list = []
    live = 0
    it = iter(records)
    for rec in it:
        buf.append(rec)
        if not _is_blank(rec[0]):
            live += 1
            if live >= PREAMBLE_LOOKAHEAD:
                break
    solid = [k for k, rec in enumerate(buf) if not _is_blank(rec[0])]
    start = 0
    for pos, k in enumerate(solid):
        row = buf[k][0]
        nxt = buf[solid[pos + 1]][0] if pos + 1 < len(solid) else row
        if row is not None and len(row) == columns and nxt is not None and len(nxt) == columns:
            start = k
            break
    skipped.extend(rec for rec in buf[:start] if not _is_blank(rec[0]))
    yield from buf[start:]
    yield from it


@functools.lru_cache(maxsize=2)
def read_table(text: str, delimiter: str, columns: int) -> Table:
    """One pass over the whole text: exact record, column and raggedness
    counts, the first rows and the last row, and a decimated sample of body
    rows for statistics. A preamble ahead of the table is skipped and
    counted separately."""
    t = Table(delimiter, columns)
    keep_cols = min(columns, MAX_COLUMNS)
    cap_rows = min(MAX_SAMPLE_ROWS, max(200, MAX_SAMPLE_CELLS // (keep_cols or 1)))
    stride = 1
    sample: list[list[str]] = []
    widths: Counter[int] = Counter()
    skipped: list = []
    for row, a, b in _body_records(read_records(physical_lines(text), delimiter), columns, skipped):
        if skipped and not t.preamble:
            t.preamble = len(skipped)
            t.preamble_first, t.preamble_last = skipped[0][1], skipped[-1][2]
            first_row = skipped[0][0]
            t.preamble_text = first_row[0][:FIELD_KEEP] if first_row else ""
        if row is None:
            t.rejected += 1
            t.first_rejected = t.first_rejected or a
            continue
        if _is_blank(row):
            t.blank_lines += b - a + 1
            continue
        t.records += 1
        t.first_line = t.first_line or a
        n = len(row)
        widths[n] += 1
        if n != columns:
            t.ragged += 1
            t.first_ragged = t.first_ragged or a
        small = [c[:FIELD_KEEP] for c in row[:ROW_CELLS_KEPT]]
        if len(t.first_rows) < FIRST_ROWS_KEPT:
            t.first_rows.append((small, a, b))
        t.last_row = (small, a, b)
        if t.records > 1:
            if t.body_seen % stride == 0:
                sample.append([_stored(c) for c in row[:keep_cols]])
                if len(sample) > 2 * cap_rows:
                    sample = sample[::2]
                    stride *= 2
            t.body_seen += 1
    if skipped and not t.preamble:      # nothing but a preamble
        t.preamble = len(skipped)
        t.preamble_first, t.preamble_last = skipped[0][1], skipped[-1][2]
    if widths:
        t.min_fields, t.max_fields = min(widths), max(widths)
    t.sample, t.stride = sample, stride
    return t


# -------------------------------------------------------------- statistics
def cell_kind(cell: str) -> str:
    c = cell.strip()
    if not c:
        return "empty"
    if len(c) > CELL_KEEP:
        return "text"
    if _INT.fullmatch(c):
        return "int"
    if _FLOAT.fullmatch(c):
        return "float"
    if _DATE.fullmatch(c):
        return "date"
    if _DATETIME.fullmatch(c):
        return "datetime"
    if c.lower() in _BOOLS:
        return "bool"
    return "text"


def column_kind(counts: Counter[str], nonempty: int) -> str:
    """The most specific kind covering at least 95% of the non-empty cells."""
    if nonempty == 0:
        return "empty"
    need = CONSISTENCY_PERCENT * nonempty

    def share(*kinds: str) -> bool:
        return sum(counts[k] for k in kinds) * 100 >= need

    if share("int"):
        return "int"
    if share("int", "float"):
        return "float"
    if share("date"):
        return "date"
    if share("date", "datetime"):
        return "datetime"
    if share("bool"):
        return "bool"
    return "text"


@dataclass
class ColumnStats:
    name: str
    kind: str
    cells: int            # sampled rows that had this column
    empty: int
    lo: str = ""
    hi: str = ""
    distinct: int = 0
    overflow: bool = False
    top: list[tuple[str, int]] = field(default_factory=list)
    longest: int = 0


def analyse(rows: list[list[str]], names: list[str], first: list[str] | None) -> list[ColumnStats]:
    """Per-column statistics over ``rows`` (plus ``first`` when the first
    record is data, not a header)."""
    out: list[ColumnStats] = []
    body = ([first] if first is not None else []) + rows
    for j, name in enumerate(names):
        counts: Counter[str] = Counter()
        values: Counter[str] = Counter()
        overflow = False
        n = empty = longest = 0
        num_lo = num_hi = None
        lo_cell = hi_cell = ""
        txt_lo = txt_hi = None
        for r in body:
            if j >= len(r):
                continue
            n += 1
            cell = r[j]
            longest = max(longest, _unstored(cell)[2])
            k = cell_kind(cell)
            counts[k] += 1
            if k == "empty":
                empty += 1
                continue
            c = cell.strip()
            if k in ("int", "float"):
                v = float(c)
                if num_lo is None or v < num_lo:
                    num_lo, lo_cell = v, c
                if num_hi is None or v > num_hi:
                    num_hi, hi_cell = v, c
            elif k in ("date", "datetime"):
                if txt_lo is None or c < txt_lo:
                    txt_lo = c
                if txt_hi is None or c > txt_hi:
                    txt_hi = c
            else:
                # Keyed on the WHOLE value (see _stored), never a prefix.
                if c in values or len(values) < DISTINCT_CAP:
                    values[c] += 1
                else:
                    overflow = True
        kind = column_kind(counts, n - empty)
        st = ColumnStats(name, kind, n, empty, longest=longest)
        if kind in ("int", "float"):
            st.lo, st.hi = lo_cell, hi_cell
        elif kind in ("date", "datetime"):
            st.lo, st.hi = txt_lo or "", txt_hi or ""
        elif kind in ("text", "bool"):
            st.distinct, st.overflow = len(values), overflow
            st.top = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        out.append(st)
    return out


_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_ ./-]{0,39}", re.ASCII)


def looks_like_header(first: list[str], stats_body: list[ColumnStats], rows: list[list[str]],
                      columns: int) -> bool:
    """Heuristic: the first record is a header when it has the table's field
    count, its cells are distinct, non-numeric labels, and either a typed body
    column has a label over it, or the body is visibly not made of labels
    too (a column of values that are not label-shaped). A table whose every
    cell is a short word, ``Smith, John``, shows no header: nothing in it
    says which line is the label."""
    if len(first) != columns:
        return False
    cells = [c.strip() for c in first]
    if not cells or sum(1 for c in cells if c) * 5 < len(cells) * 4:
        return False
    named = [c for c in cells if c]
    if len(set(named)) != len(named):
        return False
    if any(cell_kind(c) in ("int", "float", "date", "datetime") for c in named):
        return False
    typed = [j for j, st in enumerate(stats_body) if st.kind in ("int", "float", "date", "datetime", "bool")]
    if typed:
        return True
    if not rows:
        return True
    probe = rows[: 2000]
    for j, c in enumerate(cells):
        if c and any(j < len(r) and r[j].strip() == c for r in probe):
            return False
    if not all(_LABEL.fullmatch(c) for c in named):
        return False
    for j in range(len(cells)):
        col = [_unstored(r[j])[0].strip() for r in probe if j < len(r)]
        col = [c for c in col if c]
        if col and sum(1 for c in col if not _LABEL.fullmatch(c)) * 5 >= len(col):
            return True
    return False


# ------------------------------------------------------------------- profile
def _plural(n: int, word: str) -> str:
    return f"{fmt_int(n)} {word}{'' if n == 1 else 's'}"


def _every(n: int) -> str:
    """'every 4th' -- the stride, as the sample line words it."""
    if n % 100 in (11, 12, 13):
        return f"every {fmt_int(n)}th"
    return f"every {fmt_int(n)}{('th', 'st', 'nd', 'rd')[n % 10] if n % 10 < 4 else 'th'}"


def _why(chosen: Candidate, other: Candidate) -> str:
    """Why ``chosen`` outranks ``other``, said truthfully."""
    a, b = Fraction(chosen.consistent, chosen.rows), Fraction(other.consistent, other.rows)
    if a > b:
        return "the more consistent reading"
    return "the wider reading (equally consistent)"


class DelimitedDataProfile(Profile):
    version = "delimited/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        if not from_intake(ctx):
            return None
        view = ctx.stdout
        if ctx.stderr.bytes or not view.bytes or not view.text:
            return None
        if view.media_type.startswith("application/octet-stream"):
            return None
        if view.bytes <= inline_limit(ctx):
            return None
        found = sniff(mappable_text(view.text, view.parsed_fully))
        if not found:
            return None
        best = found[0]
        return (
            f"{DELIMITER_NAMES[best.delimiter]}-delimited: {best.consistent}/{best.rows} sampled "
            f"rows have {best.columns} fields ({pct(best.consistent, best.rows)})"
        )

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        partial = not view.parsed_fully
        text = mappable_text(view.text, view.parsed_fully)
        found = sniff(text)
        best = found[0]
        table = read_table(text, best.delimiter, best.columns)
        handle = "run:PENDING#stdout"
        rid = "run:PENDING"
        parsed_lines = text.count("\n") + (0 if not text or text.endswith("\n") else 1)
        total_lines = view.lines
        ncols = table.columns

        first_row = table.first_rows[0][0] if table.first_rows else []
        body = table.sample
        names_guess = [f"col{j + 1}" for j in range(min(ncols, MAX_COLUMNS))]
        stats = analyse(body, names_guess, None)
        header = looks_like_header(first_row, stats, body, ncols) if table.records > 1 else False
        full_first = None
        if header:
            for j, st in enumerate(stats):
                label = first_row[j] if j < len(first_row) and first_row[j].strip() else ""
                st.name = label or st.name
        elif table.first_rows:
            # The first record is data: it joins the statistics.
            full_first = [_stored(c) for c in first_row[:MAX_COLUMNS]]
            stats = analyse(body, names_guess, full_first)
        data_rows = table.records - (1 if header else 0)
        n_stat = len(body) + (1 if full_first is not None else 0)
        exact = n_stat == data_rows
        spans: dict[tuple[int, int], str | None] = {}

        windows = Windows(ctx, text, total_lines)
        policy = ctx.ws.config.redaction

        def span(a: int, b: int) -> str | None:
            """A span only where it resolves to exactly those lines."""
            if not windows.exact(a, b):
                return None
            if (a, b) not in spans:
                spans[(a, b)] = ctx.mint_span(view, "region", a=a, b=b)
            return spans[(a, b)]

        also = [c for c in found[1:3]]
        scope = " in the parsed part" if partial else ""
        last = table.last_row
        first_end = table.first_rows[-1][2] if table.first_rows else 0
        window_end = windows.fit_end(table.first_line, min(first_end + 20, parsed_lines)) if table.first_line else 0
        tail_from = parsed_lines + 1

        def build(level: int) -> str:
            n_cols, n_rows, cell_cap, name_cap = ((12, 3, 18, 24), (8, 2, 14, 20), (5, 1, 12, 16),
                                                  (3, 1, 10, 12), (0, 0, 8, 12))[level]
            q = Quoter(cell_cap, policy)
            out = [ctx.intake_line(total=partial) + " · delimited data"]
            kind_txt = (f"{_plural(data_rows, 'data row')}" if header
                        else f"{_plural(data_rows, 'row')}") + " (exact" + (
                            "" if not partial else ", in the parsed part") + ")"
            line = (f"delimited: {DELIMITER_NAMES[best.delimiter]} · {_plural(ncols, 'column')} (the common "
                    f"field count) · " + ("header row detected (heuristic)" if header else "no header row detected (heuristic)")
                    + f" · {kind_txt}")
            out.append(line)
            if table.preamble and level <= 2:
                shown_pre = q(table.preamble_text, name_cap * 2) if table.preamble_text else '""'
                out.append(
                    f"preamble: skipped {_plural(table.preamble, 'leading record')} at "
                    f"L{table.preamble_first}-L{table.preamble_last} whose field count is not the table's "
                    f"{ncols}; first field of the first: {shown_pre} · not counted below"
                )
            elif table.preamble:
                out.append(f"preamble: skipped {_plural(table.preamble, 'leading record')} "
                           f"(L{table.preamble_first}-L{table.preamble_last}), not counted below")
            if also and level <= 1:
                out.append("also fits: " + ", ".join(
                    f"{DELIMITER_NAMES[c.delimiter]} ({c.columns} columns, "
                    f"{c.consistent}/{c.rows} sampled rows agree)" for c in also)
                    + " · chose " + _why(best, also[0]))
            if exact:
                out.append(f"statistics: all {fmt_int(n_stat)} data rows")
            else:
                out.append(
                    f"statistics: SAMPLE of {fmt_int(n_stat)} of {fmt_int(data_rows)} data rows "
                    f"({_every(table.stride)} record after the first, deterministic): ranges, empty shares and "
                    f"value counts describe the sample; row and column counts above are exact"
                )
            if n_cols:
                shown = stats[:n_cols]
                out.append(f"columns{scope} ({len(shown)} of {fmt_int(min(ncols, MAX_COLUMNS))} analysed shown):")
                for j, st in enumerate(shown, start=1):
                    bits = [f"{j} {q(st.name, name_cap)}", st.kind]
                    bits.append("empty " + pct(st.empty, st.cells) if st.cells else "no cells")
                    if st.kind in ("int", "float", "date", "datetime") and st.lo != "":
                        bits.append(f"range {q(st.lo, cell_cap)} .. {q(st.hi, cell_cap)}" if st.kind in ("date", "datetime")
                                    else f"range {st.lo[:24]} .. {st.hi[:24]}")
                    elif st.kind in ("text", "bool"):
                        nonempty = st.cells - st.empty
                        of_sample = "" if exact else " in the sample"
                        if st.distinct and not st.overflow and st.distinct <= LOW_CARDINALITY:
                            tops = ", ".join(
                                f"{q(_unstored(v)[0], cell_cap, truncated=_unstored(v)[1])} {pct(c, nonempty)}"
                                for v, c in st.top)
                            bits.append(f"{st.distinct} distinct{of_sample}: {tops}")
                        elif st.distinct:
                            bits.append(f"{'at least ' if st.overflow else ''}{fmt_int(st.distinct)} distinct{of_sample}")
                        if st.longest > cell_cap:
                            bits.append(f"longest cell {fmt_int(st.longest)} chars (clipped here)")
                    out.append("  " + " · ".join(bits))
                if len(stats) > n_cols:
                    out.append(f"  … +{fmt_int(ncols - n_cols)} more columns not shown; names are in the first record: ctx get {handle} --lines {table.first_line}:{table.first_line}")
            if n_rows and table.first_rows:
                a, b = table.first_rows[0][1], table.first_rows[min(n_rows, len(table.first_rows) - 1)][2]
                sid = span(a, b)
                out.append(f"first records: L{a}-L{b}" + (f" · span {sid}" if sid else ""))
                for cells, la, lb in table.first_rows[: n_rows + 1]:
                    tag = " (header)" if header and la == table.first_rows[0][1] else ""
                    out.append(f"  L{la}{'-L' + str(lb) if lb != la else ''}{tag} " + self._row(q, cells, ncols, cell_cap))
            if n_rows and last is not None and table.records > len(table.first_rows):
                cells, la, lb = last
                sid = span(la, lb)
                out.append(f"last record: L{la}{'-L' + str(lb) if lb != la else ''}" + (f" · span {sid}" if sid else "")
                           + " " + self._row(q, cells, ncols, cell_cap))
            notes = []
            if table.ragged:
                notes.append(f"ragged: {fmt_int(table.ragged)} records do not have {ncols} fields (range {table.min_fields}-{table.max_fields}; first at L{table.first_ragged})")
            if table.rejected:
                notes.append(f"parser-rejected: {fmt_int(table.rejected)} records (first at L{table.first_rejected})")
            if table.blank_lines:
                notes.append(f"blank lines: {fmt_int(table.blank_lines)}")
            out += notes
            if partial:
                cov = (f"coverage: parsed L1-L{fmt_int(parsed_lines)} of {fmt_int(total_lines)} lines "
                       f"({fmt_bytes(len(text.encode('utf-8')))} of {fmt_bytes(view.bytes)}; parse cap "
                       f"{fmt_bytes(MAX_PARSE_BYTES)}), NOT parsed: L{fmt_int(tail_from)}-L{fmt_int(total_lines)}")
            else:
                cov = f"coverage: all {fmt_int(total_lines)} lines parsed"
            cov += (f" · {fmt_int(table.records)} records over L{table.first_line}-L{parsed_lines}"
                    f" · {'values shown: first ' + str(min(n_rows + 1, len(table.first_rows))) + ' and last record' if n_rows else 'no rows shown at this size'}")
            if q.clipped:
                cov += f" · {_plural(q.clipped, 'cell')} clipped to {cell_cap} chars in this view"
            out.append(cov)
            sugg = [f"ctx get {handle} --lines {table.first_line}:{window_end}"]
            if last is not None and last[1] > window_end:
                sugg.append(f"ctx get {handle} --lines {windows.fit_start(bounds.count(last[2] - 20) + 1, last[2])}:{last[2]}")
            if table.ragged:
                sugg.append(f"ctx get {handle} --lines {table.first_ragged}:{windows.fit_end(table.first_ragged, min(table.first_ragged + 9, parsed_lines))}")
            elif partial:
                sugg.append(f"ctx get {handle} --lines {tail_from}:{tail_from + 39}")
            else:
                sugg.append(f"ctx search {rid} '<pattern>' --context 2")
            return "\n".join(out + self.next_lines(ctx, sugg) + q.note())

        return fit(ctx, build, 5)

    @staticmethod
    def _row(q: Quoter, cells: list[str], ncols: int, cell_cap: int) -> str:
        shown = cells[:6]
        text = " | ".join(q(c, cell_cap) for c in shown)   # whole, as stored: see Quoter
        if ncols > len(shown):
            text += f" | … +{ncols - len(shown)} columns"
        return text


__all__ = ["DelimitedDataProfile", "read_table", "sniff", "cell_kind"]
