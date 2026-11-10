"""Deterministic deliverable contract extraction and pre-emission structural verification.

Implements Straitjacket's Emission Gate for file-producing agent tasks without
task-specific hints or external dependencies:
  1. Extracts concrete output file paths explicitly named in the user prompt
     (for example ``outputs/report.json`` or ``/task/outputs/summary.md``) while
     ignoring wildcard or template placeholders (``*``, ``0X``, ``<id>``).
  2. Verifies deliverable existence, non-emptiness, syntax validity (JSON, CSV,
     XML/SVG), stray scratch-script hygiene inside ``outputs/``, and intra-array
     record field sparsity across homogeneous JSON object arrays.
  3. Renders a bounded structural receipt summarizing generated deliverables so
     an agent can fix omitted files, syntax errors, or accidental null fields
     before finalizing.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

__all__ = [
    "DeliverableAudit",
    "audit_deliverables",
    "extract_declared_deliverables",
]

_PLACEHOLDER_RE = re.compile(
    r"(?:\*|\?|<[^>]*>|\{[^}]*\}|\b(?:example|sample|placeholder)\b|0X\b|_XX\b|\.\.\.)",
    re.I,
)
_SCRATCH_SUFFIXES = frozenset({".py", ".sh", ".bash", ".tmp", ".bak", ".pyc", ".swp"})
_RECORD_ID_KEYS = ("id", "claim_id", "sku", "asin", "item_id", "code", "key", "name", "title")
_MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,4})\s+(.+?)\s*$")
_MD_BULLET_RE = re.compile(r"^\s*[-*•]\s+\S")
_MD_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+\S")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")


@dataclass(frozen=True)
class DeliverableAudit:
    """Result of deterministic pre-emission inspection over workspace deliverables."""

    blocking_defects: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    structural_summary: tuple[str, ...] = ()
    present_files_resolved: tuple[str, ...] = field(default=())

    @property
    def has_blocking_defects(self) -> bool:
        return bool(self.blocking_defects)

    @property
    def should_gate(self) -> bool:
        return bool(self.blocking_defects or self.advisories)

    def format_gate_message(self) -> str:
        lines = ["[ctx-emission-gate] Deliverable check found issues to resolve or confirm before finalizing:"]
        for defect in self.blocking_defects:
            lines.append(f"- [REQUIRED] {defect}")
        for adv in self.advisories:
            lines.append(f"- [CHECK] {adv}")
        if self.structural_summary:
            lines.append("Current deliverable structure:")
            for s in self.structural_summary[:10]:
                lines.append(f"  * {s}")
        return "\n".join(lines)


def _is_concrete_rel_path(rel: str) -> bool:
    if not rel or _PLACEHOLDER_RE.search(rel):
        return False
    p = Path(rel)
    if ".." in p.parts:
        return False
    suffix = p.suffix.lower()
    if not suffix or len(suffix) > 9 or not re.match(r"^\.[a-z0-9]+$", suffix):
        return False
    return True


def extract_declared_deliverables(
    prompt_text: str,
    *,
    output_roots: tuple[str, ...] = ("outputs",),
) -> list[str]:
    """Extract concrete relative deliverable paths under ``output_roots`` named in ``prompt_text``."""
    if not prompt_text or not output_roots:
        return []

    roots_alt = "|".join(re.escape(r.strip("/")) for r in output_roots if r.strip("/"))
    if not roots_alt:
        return []

    direct_re = re.compile(
        rf"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_.-]+)*?((?:{roots_alt})/[A-Za-z0-9_./-]+\.[A-Za-z0-9]{{1,8}})"
    )

    found: list[str] = []
    seen: set[str] = set()

    for m in direct_re.finditer(prompt_text):
        cand = m.group(1).strip().rstrip(".,;:!?)`'\"]")
        norm = Path(cand).as_posix()
        if _is_concrete_rel_path(norm) and norm not in seen:
            seen.add(norm)
            found.append(norm)

    # Also capture backticked filenames explicitly listed in the same paragraph/sentence
    # as an ``outputs/`` directory directive (e.g. "Write to `outputs/`: `a.json`, `b.md`").
    backtick_file_re = re.compile(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8})`")
    dir_mention_re = re.compile(rf"(?:/task/)?\b({roots_alt})/(?:\s|$|[`'\"):,.;，。：）、])")
    for para in prompt_text.split("\n\n"):
        dm = dir_mention_re.search(para)
        if not dm:
            continue
        root_prefix = dm.group(1)
        for bm in backtick_file_re.finditer(para):
            raw_tok = bm.group(1).strip().lstrip("/")
            if raw_tok.startswith("task/"):
                raw_tok = raw_tok[len("task/") :]
            if raw_tok.startswith(("workspace/", "tmp/", "http:", "https:")):
                continue
            if any(raw_tok.startswith(f"{r.strip('/')}/") for r in output_roots):
                rel_cand = Path(raw_tok).as_posix()
            else:
                # Skip tokens that clearly refer to workspace inputs mentioned earlier in the paragraph
                if f"workspace/{raw_tok}" in para:
                    continue
                rel_cand = f"{root_prefix}/{Path(raw_tok).as_posix()}"
            if _is_concrete_rel_path(rel_cand) and rel_cand not in seen:
                seen.add(rel_cand)
                found.append(rel_cand)

    return found


def _record_label(rec: dict[str, Any], idx: int) -> str:
    for k in _RECORD_ID_KEYS:
        val = rec.get(k)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val).strip()[:32]
    return f"#{idx}"


def _is_empty_scalar_or_collection(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    return False


def _inspect_json_record_arrays(
    rel_label: str,
    data: Any,
) -> tuple[list[str], str]:
    """Return ``(advisories, shape_summary)`` for parsed JSON ``data``."""
    advisories: list[str] = []
    arrays_to_check: list[tuple[str, list[dict[str, Any]]]] = []

    if isinstance(data, list):
        shape = f"JSON array (len={len(data)})"
        if len(data) == 0:
            advisories.append(f"{rel_label} is an empty JSON array ([]).")
        elif all(isinstance(x, dict) for x in data):
            arrays_to_check.append(("$", data))
    elif isinstance(data, dict):
        key_parts: list[str] = []
        empty_top: list[str] = []
        for k, v in list(data.items())[:16]:
            if isinstance(v, list):
                key_parts.append(f"{k}[{len(v)}]")
                if len(v) == 0:
                    empty_top.append(f"{k}=[]")
                elif all(isinstance(x, dict) for x in v):
                    arrays_to_check.append((str(k), v))
            elif isinstance(v, dict):
                key_parts.append(f"{k}{{{len(v)}}}")
                if len(v) == 0:
                    empty_top.append(f"{k}={{}}")
            elif v is None or (isinstance(v, str) and not v.strip()):
                key_parts.append(f"{k}=empty")
                empty_top.append(f"{k}={v!r}")
            else:
                key_parts.append(str(k))
        more = f", +{len(data) - 16} keys" if len(data) > 16 else ""
        shape = f"JSON object keys=[{', '.join(key_parts)}{more}]"
        if empty_top and len(empty_top) <= 4:
            advisories.append(
                f"{rel_label} has empty/null top-level field(s): {', '.join(empty_top)} — confirm this matches the schema."
            )
    else:
        shape = f"JSON {type(data).__name__}"

    for arr_name, records in arrays_to_check:
        n = len(records)
        if n < 3:
            continue
        all_keys: list[str] = []
        seen_k: set[str] = set()
        for r in records:
            for k in r:
                if k not in seen_k:
                    seen_k.add(k)
                    all_keys.append(str(k))
        # Identify low-cardinality categorical string columns (>=3 states) for
        # multi-state enum conditional-nullability checks.
        enum_cols: list[str] = []
        if n >= 4:
            for ck in all_keys:
                if ck in _RECORD_ID_KEYS:
                    continue
                vals = [r.get(ck) for r in records]
                if all(isinstance(v, str) and 1 <= len(v.strip()) <= 40 for v in vals):
                    distinct_vals = {str(v).strip() for v in vals}
                    if 3 <= len(distinct_vals) <= min(6, n - 1):
                        enum_cols.append(ck)
            enum_cols.sort(key=lambda ck: len({str(r.get(ck)).strip() for r in records}))

        for k in all_keys:
            missing_labels: list[str] = []
            populated = 0
            for idx, r in enumerate(records):
                if k not in r or _is_empty_scalar_or_collection(r.get(k)):
                    missing_labels.append(_record_label(r, idx))
                else:
                    populated += 1
            sparse_cnt = len(missing_labels)
            if populated >= max(2, (n + 1) // 2) and 1 <= sparse_cnt <= (n // 2):
                sample = ", ".join(missing_labels[:5])
                if sparse_cnt > 5:
                    sample += f", +{sparse_cnt - 5} more"
                advisories.append(
                    f"In {rel_label}, array '{arr_name}' ({n} records) has null/empty '{k}' on "
                    f"{sparse_cnt} record(s) [{sample}] while populated on {populated}/{n} — "
                    f"verify whether empty '{k}' is allowed for those records."
                )
                if len(advisories) >= 4:
                    break
            elif enum_cols and 2 <= populated < max(2, (n + 1) // 2) and all(k in r for r in records):
                for ck in enum_cols:
                    if ck == k:
                        continue
                    groups: dict[str, list[tuple[bool, str]]] = {}
                    for idx, r in enumerate(records):
                        gval = str(r.get(ck)).strip()
                        is_pop = not _is_empty_scalar_or_collection(r.get(k))
                        groups.setdefault(gval, []).append((is_pop, _record_label(r, idx)))
                    full_groups = [g for g, items in groups.items() if all(p for p, _ in items)]
                    zero_groups = [g for g, items in groups.items() if not any(p for p, _ in items)]
                    if full_groups and len(zero_groups) >= 2:
                        parts_s: list[str] = []
                        for g, items in groups.items():
                            pop_g = sum(1 for p, _ in items if p)
                            tot_g = len(items)
                            if pop_g == 0 and tot_g <= 4:
                                ids_s = ", ".join(lbl for _, lbl in items[:4])
                                parts_s.append(f"{g}=0/{tot_g} [{ids_s}]")
                            else:
                                parts_s.append(f"{g}={pop_g}/{tot_g}")
                        advisories.append(
                            f"In {rel_label}, array '{arr_name}' ({n} records) populates '{k}' on "
                            f"{populated}/{n} records (by '{ck}': {'; '.join(parts_s)}) — "
                            f"verify which '{ck}' states require non-empty '{k}'."
                        )
                        break
                if len(advisories) >= 4:
                    break
        if len(advisories) >= 4:
            break

    return advisories, shape


def _inspect_markdown(text: str) -> str:
    lines = text.splitlines()
    headings: list[str] = []
    bullets = 0
    numbered = 0
    table_rows = 0
    in_table = False
    for ln in lines:
        hm = _MD_HEADING_RE.match(ln)
        if hm:
            headings.append(hm.group(2)[:36])
        if _MD_BULLET_RE.match(ln):
            bullets += 1
        elif _MD_NUMBERED_RE.match(ln):
            numbered += 1
        if "|" in ln and ln.strip().startswith("|"):
            if _MD_TABLE_SEP_RE.match(ln):
                in_table = True
            elif in_table:
                table_rows += 1
        else:
            in_table = False
    h_preview = f" sections={headings[:6]}" if headings else ""
    return (
        f"Markdown ({len(lines)} lines, {bullets} bullets, {numbered} numbered, "
        f"{table_rows} table data rows{h_preview})"
    )


def _to_rel_display(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def audit_deliverables(
    workspace_root: Path,
    presented_paths: list[str | Path],
    *,
    declared_paths: list[str] | None = None,
    output_dir: str = "outputs",
    ignore_dir_names: frozenset[str] = frozenset({"mock_audit", "mock_state"}),
) -> DeliverableAudit:
    """Inspect workspace deliverables for missing files, syntax errors, and structural anomalies."""
    ws = workspace_root.resolve()
    out_root = (ws / output_dir).resolve()

    blocking: list[str] = []
    advisories: list[str] = []
    summaries: list[str] = []

    resolved_presented: list[Path] = []
    seen_resolved: set[Path] = set()
    for raw_p in presented_paths:
        s = str(raw_p or "").strip()
        if not s:
            continue
        if s.startswith("/task/"):
            p = (ws / s[len("/task/") :]).resolve()
        elif s == "/task":
            p = ws
        else:
            cand = Path(s)
            p = cand.resolve() if cand.is_absolute() else (ws / cand).resolve()
        if p not in seen_resolved:
            seen_resolved.add(p)
            resolved_presented.append(p)

    # 1. Check prompt-declared concrete deliverables exist and are included.
    for rel_decl in declared_paths or ():
        dp = (ws / rel_decl).resolve()
        if not dp.exists() or not dp.is_file():
            blocking.append(
                f"Deliverable '{rel_decl}' explicitly requested in task instructions does not exist."
            )
        elif dp.stat().st_size == 0:
            blocking.append(
                f"Deliverable '{rel_decl}' is empty (0 bytes)."
            )
        elif resolved_presented and dp not in seen_resolved:
            advisories.append(
                f"Deliverable '{rel_decl}' exists on disk ({dp.stat().st_size} bytes) but was omitted from present_files."
            )
            seen_resolved.add(dp)
            resolved_presented.append(dp)

    # If present_files was called with an empty list while user files exist in outputs/, inspect them too.
    files_to_inspect = list(resolved_presented)
    if not files_to_inspect and out_root.is_dir():
        for p in sorted(out_root.rglob("*")):
            if p.is_file() and not any(ig in p.parts for ig in ignore_dir_names):
                files_to_inspect.append(p.resolve())

    # 2. Inspect each deliverable's syntax & structure.
    for fp in files_to_inspect:
        rel_lbl = _to_rel_display(fp, ws)
        if not fp.exists() or not fp.is_file():
            msg = f"Presented deliverable '{rel_lbl}' does not exist."
            if msg not in blocking:
                blocking.append(msg)
            continue
        sz = fp.stat().st_size
        if sz == 0:
            msg = f"Presented deliverable '{rel_lbl}' is empty (0 bytes)."
            if msg not in blocking:
                blocking.append(msg)
            continue

        ext = fp.suffix.lower()
        if ext == ".json":
            try:
                raw_txt = fp.read_text(encoding="utf-8")
                parsed = json.loads(raw_txt)
            except Exception as exc:
                blocking.append(f"Invalid JSON syntax in '{rel_lbl}': {exc}")
                continue
            j_adv, j_shape = _inspect_json_record_arrays(rel_lbl, parsed)
            advisories.extend(j_adv)
            summaries.append(f"{rel_lbl}: {j_shape}")
        elif ext == ".csv":
            try:
                raw_txt = fp.read_text(encoding="utf-8")
                rows = list(csv.reader(io.StringIO(raw_txt)))
            except Exception as exc:
                blocking.append(f"Invalid CSV syntax in '{rel_lbl}': {exc}")
                continue
            if not rows:
                blocking.append(f"CSV deliverable '{rel_lbl}' has no header or rows.")
            elif len(rows) == 1:
                advisories.append(f"CSV deliverable '{rel_lbl}' has header row ({len(rows[0])} cols) but 0 data rows.")
                summaries.append(f"{rel_lbl}: CSV 0 data rows, cols={rows[0][:10]}")
            else:
                col_counts = {len(r) for r in rows if any(c.strip() for c in r)}
                if len(col_counts) > 1:
                    advisories.append(
                        f"CSV deliverable '{rel_lbl}' has inconsistent column counts across rows: {sorted(col_counts)}."
                    )
                summaries.append(
                    f"{rel_lbl}: CSV {len(rows) - 1} data rows x {len(rows[0])} cols={rows[0][:10]}"
                )
        elif ext in (".md", ".markdown", ".txt"):
            raw_txt = fp.read_text(encoding="utf-8", errors="replace")
            if not raw_txt.strip():
                blocking.append(f"Text deliverable '{rel_lbl}' contains only whitespace.")
            else:
                summaries.append(f"{rel_lbl}: {_inspect_markdown(raw_txt)}")
        elif ext in (".svg", ".xml"):
            try:
                ET.fromstring(fp.read_text(encoding="utf-8"))
                summaries.append(f"{rel_lbl}: valid {ext.lstrip('.').upper()} ({sz:,} bytes)")
            except Exception as exc:
                blocking.append(f"Malformed {ext.upper()} markup in '{rel_lbl}': {exc}")
        else:
            summaries.append(f"{rel_lbl}: {sz:,} bytes")

    # 3. Check for stray scratch scripts inside outputs/ not explicitly declared.
    declared_resolved = {(ws / d).resolve() for d in (declared_paths or ())}
    if out_root.is_dir():
        for extra_p in sorted(out_root.rglob("*")):
            if not extra_p.is_file():
                continue
            if any(ig in extra_p.parts for ig in ignore_dir_names):
                continue
            rp = extra_p.resolve()
            if rp in seen_resolved or rp in declared_resolved:
                continue
            if extra_p.suffix.lower() in _SCRATCH_SUFFIXES or extra_p.name.startswith("."):
                rel_ex = _to_rel_display(rp, ws)
                blocking.append(
                    f"Stray temporary/scratch file '{rel_ex}' found inside {output_dir}/ — remove temporary scripts from {output_dir}/ before presenting."
                )

    return DeliverableAudit(
        blocking_defects=tuple(blocking),
        advisories=tuple(advisories),
        structural_summary=tuple(summaries),
        present_files_resolved=tuple(_to_rel_display(p, ws) for p in resolved_presented),
    )
