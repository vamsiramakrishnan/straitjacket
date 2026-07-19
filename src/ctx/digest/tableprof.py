"""Tabular output profile (SPEC §9 "tabular output"): aligned columnar
listings with an ALL-CAPS header — docker/podman ps/images, kubectl/oc get,
docker compose ps, and anything of that shape from foreign tools.

Measured motivation (evals/coverage-corpus-2026-07-19.md): a 180-row
``kubectl get pods`` under text/v1 shows the first and last five rows; the
8 CrashLoopBackOff and 5 ImagePullBackOff pods live in the omitted middle —
the quiet-needle failure mode for tables. Rarity in a table is a *value
census*, not a keyword: exact per-column histograms surface the minority
states with coordinates at a fraction of the head/tail budget.

Detection is shape-based (header + alignment), so MCP-delivered tables with
synthesized argv classify identically. Columns that are near-unique (names,
ids, ages) get no histogram — only low-cardinality columns carry decisions.
"""

from __future__ import annotations

import re
from collections import Counter

from ctx.digest.base import DigestContext, Profile
from ctx.textutil import fmt_int

# A header cell: starts with a capital letter or %, continues in caps,
# digits, and light punctuation; single spaces allowed inside a cell
# ("CONTAINER ID") — cells are separated by runs of >= 2 spaces.
_HEADER_CELL_RE = re.compile(r"[A-Z%][A-Z0-9%_/()\-\.]*(?: [A-Z0-9%_/()\-\.]+)*")
_MIN_ROWS = 15
_MAX_HISTOGRAM_VALUES = 8


def _split_header(line: str) -> list[tuple[int, str]]:
    """Header cells as (start_offset, name); [] when any cell is non-caps."""
    cells: list[tuple[int, str]] = []
    for m in re.finditer(r"\S+(?: \S+)*", line):
        if not _HEADER_CELL_RE.fullmatch(m.group(0)):
            return []
        cells.append((m.start(), m.group(0)))
    return cells


class TableProfile(Profile):
    version = "table/v1"

    def _parse(self, ctx: DigestContext) -> tuple[list[tuple[int, str]], list[tuple[int, str]]] | None:
        lines = ctx.stdout.text_lines
        if not lines:
            return None
        header_idx = 0
        while header_idx < len(lines) and not lines[header_idx].strip():
            header_idx += 1
        if header_idx >= len(lines):
            return None
        cells = _split_header(lines[header_idx])
        if len(cells) < 3:
            return None
        rows = [
            (i, ln)
            for i, ln in enumerate(lines[header_idx + 1 :], start=header_idx + 2)
            if ln.strip()
        ]
        if len(rows) < _MIN_ROWS:
            return None
        # Alignment check: most rows must have content at column offsets.
        offsets = [start for start, _ in cells[1:]]
        aligned = sum(
            1
            for _, ln in rows
            if sum(1 for o in offsets if o < len(ln) and ln[o] != " ") >= max(1, len(offsets) - 1)
        )
        if aligned < len(rows) * 0.7:
            return None
        return cells, rows

    def detect(self, ctx: DigestContext) -> str | None:
        parsed = self._parse(ctx)
        if parsed is None:
            return None
        cells, rows = parsed
        return f"aligned table: {len(cells)}-column caps header over {len(rows)} rows"

    def render(self, ctx: DigestContext) -> str:
        cells, rows = self._parse(ctx)  # type: ignore[misc]  # detect guaranteed
        names = [name for _, name in cells]
        starts = [start for start, _ in cells]

        def cell(ln: str, col: int) -> str:
            a = starts[col]
            b = starts[col + 1] if col + 1 < len(starts) else len(ln)
            return ln[a:b].strip()

        summary = [
            "summary:",
            f"  table (exact): {fmt_int(len(rows))} rows × {len(names)} columns",
            "  columns: " + " · ".join(names),
        ]
        shown = 1  # header

        # Exact value census for low-cardinality columns; near-unique columns
        # (names, ids, ages) carry no decision and get none.
        dense = bool(getattr(ctx, "dense", False))
        histogrammed: list[tuple[int, str, Counter[str]]] = []
        for col, name in enumerate(names):
            counts: Counter[str] = Counter(cell(ln, col) for _, ln in rows)
            counts.pop("", None)
            if 2 <= len(counts) <= _MAX_HISTOGRAM_VALUES and len(counts) <= len(rows) // 3:
                histogrammed.append((col, name, counts))
        for _col, name, counts in histogrammed if dense else histogrammed[:3]:
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            summary.append(
                f"  {name} (exact): " + " · ".join(f"{v} {fmt_int(n)}" for v, n in ranked)
            )

        # Minority evidence: the first row carrying each non-modal value,
        # rarest value first, with real coordinates — the quiet needle is a
        # census entry here, never an omitted middle line.
        evidence_cap = 12 if dense else 4
        seen_lines: set[int] = set()
        evidence: list[tuple[str, str]] = []  # (value, rendered line)
        for col, _name, counts in histogrammed:
            ranked = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))
            for value, _n in ranked[: len(ranked) - 1][:3]:  # skip the modal value
                for lineno, ln in rows:
                    if cell(ln, col) == value:
                        if lineno not in seen_lines:
                            seen_lines.add(lineno)
                            evidence.append(
                                (value, f"  first {value} stdout:L{lineno}: {ln.strip()[:160]}")
                            )
                        break
                if len(evidence) >= evidence_cap:
                    break
            if len(evidence) >= evidence_cap:
                break
        summary.extend(line for _v, line in evidence)
        shown += len(evidence)

        first_row, last_row = rows[0][0], rows[-1][0]
        marker = f"  rows at stdout:L{first_row}-L{last_row}"
        sid = ctx.mint_span(ctx.stdout, "region", a=first_row, b=last_row)
        if sid:
            marker += f" · span {sid}"
        summary.append(marker)

        rid = "run:PENDING"
        suggestions = []
        if evidence:
            suggestions.append(f"ctx search {rid} '{evidence[0][0]}' --context 0")
        suggestions.append(f"ctx get {rid}#stdout --lines {first_row}:{min(last_row, first_row + 39)}")
        return "\n".join(
            ctx.header_lines()
            + summary
            + self.coverage_lines(ctx, shown)
            + self.next_lines(ctx, suggestions)
        )
