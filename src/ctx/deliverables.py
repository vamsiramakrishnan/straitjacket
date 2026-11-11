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

from collections.abc import Sequence
import csv
from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

__all__ = [
    "CLOSED_WORLD_CONTRACT_RULE",
    "DeliverableAudit",
    "audit_deliverables",
    "extract_declared_deliverables",
    "extract_deliverable_specs",
    "format_deliverable_contract",
    "verify_declared_deliverables",
]

CLOSED_WORLD_CONTRACT_RULE = (
    "Closed-world contract: all requirements are in /task/TASK.md and [deliverable-contract]. "
    "Never search outside /task/workspace for hidden graders/tests; if a minor format detail "
    'is unstated, choose standard conventions and log "assumed: <choice>" in state_note.'
)

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
_PLAIN_NUM_STR_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")
_FORMATTED_NUM_STR_RE = re.compile(
    r"^[+-]?\$?\s*(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s*%)\s*$"
)
_CLEAN_ENUM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_JSON_KEY_COLON_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_.-]{0,48})"\s*:')
_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]{1,120})`")
_IDENT_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,48}$")
_IGNORED_SPEC_TOKENS = frozenset(
    {
        "true",
        "false",
        "null",
        "none",
        "json",
        "jsonl",
        "csv",
        "tsv",
        "md",
        "markdown",
        "html",
        "yaml",
        "yml",
        "txt",
        "xml",
        "svg",
        "utf-8",
        "utf8",
        "ascii",
        "bash",
        "sh",
        "python",
        "python3",
        "jq",
        "curl",
        "task",
        "outputs",
        "workspace",
        "tmp",
        "present_files",
        "state_note",
    }
)
_FILE_LIKE_SUFFIXES = (
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".md",
    ".markdown",
    ".txt",
    ".py",
    ".sh",
    ".html",
    ".yml",
    ".yaml",
    ".toml",
    ".xml",
    ".svg",
    ".pdf",
    ".png",
    ".jpg",
    ".db",
    ".sqlite",
)


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


def _load_task_markdown(task_markdown: str | Path = "", workspace_root: str | Path = "/task") -> str:
    if isinstance(task_markdown, Path):
        workspace_root = task_markdown
        raw_md = ""
    else:
        raw_md = str(task_markdown or "")
        if (
            raw_md.strip()
            and "\n" not in raw_md
            and len(raw_md) < 240
            and not raw_md.lstrip().startswith("#")
        ):
            try:
                cand_dir = Path(raw_md)
                if cand_dir.is_dir():
                    workspace_root = cand_dir
                    raw_md = ""
            except OSError:
                pass
    if raw_md.strip():
        return raw_md
    try:
        ws = Path(workspace_root)
        if ws.is_dir():
            for fname in ("TASK.md", "task.md"):
                tp = ws / fname
                if tp.is_file():
                    return tp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def _deliverable_format_label(rel_path: str) -> str:
    ext = Path(rel_path).suffix.lower()
    if ext in (".json", ".jsonl"):
        return "json"
    if ext in (".csv", ".tsv"):
        return "csv"
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext in (".html", ".htm"):
        return "html"
    if ext in (".yaml", ".yml"):
        return "yaml"
    if ext in (".svg", ".xml"):
        return "xml"
    if ext == ".txt":
        return "text"
    return ext.lstrip(".") or "file"


def _is_valid_field_token(tok: str, declared_set: set[str]) -> bool:
    t = tok.strip()
    if not t or len(t) > 48:
        return False
    low = t.lower()
    if low in _IGNORED_SPEC_TOKENS:
        return False
    if "/" in t or "\\" in t or ":" in t:
        return False
    if any(low.endswith(sfx) for sfx in _FILE_LIKE_SUFFIXES):
        return False
    if t in declared_set or any(Path(d).name == t for d in declared_set):
        return False
    return bool(_IDENT_TOKEN_RE.match(t))


def _extract_tokens_from_span(
    span_text: str,
    fmt: str,
    declared_set: set[str],
) -> tuple[list[str], list[str]]:
    fields: list[str] = []
    headings: list[str] = []
    seen_f: set[str] = set()
    seen_h: set[str] = set()

    def _add_f(cand: str) -> None:
        c = cand.strip()
        if c not in seen_f and _is_valid_field_token(c, declared_set):
            seen_f.add(c)
            fields.append(c)

    def _add_h(cand: str) -> None:
        c = " ".join(cand.strip().lstrip("#").split())[:48]
        if c and c not in seen_h:
            seen_h.add(c)
            headings.append(c)

    for m_jk in _JSON_KEY_COLON_RE.finditer(span_text):
        _add_f(m_jk.group(1))

    for m_bt in _BACKTICK_SPAN_RE.finditer(span_text):
        bt = m_bt.group(1).strip()
        if not bt:
            continue
        if bt.startswith("#"):
            _add_h(bt)
            continue
        if bt.startswith(("{", "[")):
            for m_jk in _JSON_KEY_COLON_RE.finditer(bt):
                _add_f(m_jk.group(1))
            continue
        if "," in bt and " " not in bt and fmt == "csv":
            for part in bt.split(","):
                _add_f(part)
            continue
        _add_f(bt)

    if fmt == "markdown":
        for m_fen in re.finditer(r"```(?:markdown|md)?\n(.*?)```", span_text, re.S | re.I):
            for ln in m_fen.group(1).splitlines():
                hm = _MD_HEADING_RE.match(ln)
                if hm:
                    _add_h(hm.group(2))

    return fields, headings


def extract_deliverable_specs(
    task_markdown: str = "",
    workspace_root: str | Path = "/task",
) -> list[dict[str, Any]]:
    """Extract structured deliverable specifications (path, format, required keys/headers/sections)."""
    md_text = _load_task_markdown(task_markdown, workspace_root)
    if not md_text:
        return []

    declared = extract_declared_deliverables(md_text)
    if not declared:
        return []

    declared_set = set(declared)
    paragraphs = [p for p in re.split(r"\n\s*\n", md_text) if p.strip()]
    specs: list[dict[str, Any]] = []

    for rel_path in declared:
        fmt = _deliverable_format_label(rel_path)
        fname = Path(rel_path).name
        matched_paras: list[str] = []
        for idx, para in enumerate(paragraphs):
            if rel_path in para or fname in para:
                matched_paras.append(para)
                if idx + 1 < len(paragraphs):
                    nxt = paragraphs[idx + 1]
                    if not any(other != rel_path and (other in nxt or Path(other).name in nxt) for other in declared):
                        matched_paras.append(nxt)

        local_text = "\n\n".join(matched_paras) if matched_paras else md_text
        fields, headings = _extract_tokens_from_span(local_text, fmt, declared_set)
        if len(fields) < 2 and len(declared) == 1 and local_text != md_text:
            all_fields, all_headings = _extract_tokens_from_span(md_text, fmt, declared_set)
            for af in all_fields:
                if af not in fields:
                    fields.append(af)
            for ah in all_headings:
                if ah not in headings:
                    headings.append(ah)

        capped_fields = fields[:14]
        capped_headings = headings[:10]
        specs.append(
            {
                "path": rel_path,
                "format": fmt,
                "fields": capped_fields,
                "required_keys": capped_fields,
                "headings": capped_headings,
            }
        )

    return specs


def format_deliverable_contract(
    task_markdown: str = "",
    workspace_root: str | Path = "/task",
) -> str:
    """Render a compact (<=150 tokens) ``[deliverable-contract]`` card ending with the Closed-World rule."""
    md_text = _load_task_markdown(task_markdown, workspace_root)
    if not md_text.strip():
        return ""

    specs = extract_deliverable_specs(md_text, workspace_root=workspace_root)
    lines: list[str] = ["[deliverable-contract]"]
    if specs:
        for spec in specs[:6]:
            rel_path = spec["path"]
            disp_path = f"/task/{rel_path}" if not str(rel_path).startswith("/task/") else str(rel_path)
            fmt = spec["format"]
            fields = spec.get("fields") or []
            headings = spec.get("headings") or []
            if fmt == "markdown" and headings:
                sec_s = ", ".join(headings[:8])
                extra_f = f"; fields=[{', '.join(fields[:6])}]" if fields else ""
                entry = f"- {disp_path} ({fmt}): sections=[{sec_s}]{extra_f}"
            elif fields:
                lbl = "keys" if fmt == "json" else ("columns" if fmt == "csv" else "fields")
                entry = f"- {disp_path} ({fmt}): {lbl}=[{', '.join(fields[:10])}]"
            else:
                entry = f"- {disp_path} ({fmt})"
            lines.append(entry[:140])
    else:
        lines.append("- deliverables: none declared under /task/outputs (complete required service/UI actions)")

    body = "\n".join(lines)
    if len(body) > 360:
        body = body[:357].rsplit("\n", 1)[0]
    return f"{body}\n{CLOSED_WORLD_CONTRACT_RULE}"


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


def _is_raw_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _is_plain_numeric_str(val: Any) -> bool:
    return isinstance(val, str) and bool(_PLAIN_NUM_STR_RE.match(val.strip()))


def _is_formatted_numeric_str(val: Any) -> bool:
    return isinstance(val, str) and bool(_FORMATTED_NUM_STR_RE.match(val.strip()))


def _inspect_cross_row_consistency(
    rel_label: str,
    arr_name: str,
    records: list[dict[str, Any]],
    all_keys: list[str],
    task_markdown: str = "",
) -> list[str]:
    """Detect Q4 cross-row numeric formatting/type outliers and singleton enum/status outliers."""
    issues: list[str] = []
    n = len(records)
    if n < 5:
        return issues

    md_lower = (task_markdown or "").lower()

    for k in all_keys:
        non_null: list[tuple[int, dict[str, Any], Any]] = []
        for idx, r in enumerate(records):
            val = r.get(k)
            if not _is_empty_scalar_or_collection(val) and not isinstance(val, (dict, list)):
                non_null.append((idx, r, val))
        m = len(non_null)
        if m < 5:
            continue

        # 1. Numeric formatting/type outliers (e.g. "2,400" or string numbers mixed with raw int/float).
        raw_num_rows = [(idx, r, v) for idx, r, v in non_null if _is_raw_number(v)]
        plain_str_rows = [(idx, r, v) for idx, r, v in non_null if _is_plain_numeric_str(v)]
        fmt_str_rows = [(idx, r, v) for idx, r, v in non_null if _is_formatted_numeric_str(v)]

        clean_numeric_count = len(raw_num_rows) + len(plain_str_rows)
        if clean_numeric_count * 5 >= m * 4 and 1 <= len(fmt_str_rows) <= 2:
            for idx, r, v in fmt_str_rows:
                lbl = _record_label(r, idx)
                issues.append(
                    f"In {rel_label}, array '{arr_name}' row {idx} ({lbl}) key '{k}' has formatted numeric "
                    f"outlier {v!r} while {clean_numeric_count}/{m} sibling rows use unformatted numbers."
                )
            continue

        str_num_outliers = plain_str_rows + fmt_str_rows
        if len(raw_num_rows) * 5 >= m * 4 and 1 <= len(str_num_outliers) <= 2:
            for idx, r, v in str_num_outliers:
                lbl = _record_label(r, idx)
                issues.append(
                    f"In {rel_label}, array '{arr_name}' row {idx} ({lbl}) key '{k}' has string-typed numeric "
                    f"outlier {v!r} while {len(raw_num_rows)}/{m} sibling rows use raw int/float numbers."
                )
            continue

        # 2. Singleton enum/status outliers (e.g. "needs_rounding_note" absent from task_markdown).
        if k in _RECORD_ID_KEYS:
            continue
        if not all(isinstance(v, str) and 1 <= len(v.strip()) <= 80 for _, _, v in non_null):
            continue

        val_groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for idx, r, v in non_null:
            val_groups.setdefault(v.strip(), []).append((idx, r))

        singletons = [sv for sv, items in val_groups.items() if len(items) == 1]
        if len(singletons) != 1:
            continue

        s_val = singletons[0]
        s_idx, s_rec = val_groups[s_val][0]
        other_vals = [ov for ov in val_groups if ov != s_val]
        if not (1 <= len(other_vals) <= 4):
            continue

        shared_rows = m - 1
        if shared_rows * 5 < m * 4:
            continue

        s_in_md = bool(md_lower and s_val.lower() in md_lower)
        if s_in_md:
            continue

        other_clean = all(_CLEAN_ENUM_TOKEN_RE.match(ov) for ov in other_vals)
        other_in_md = bool(md_lower) and all(ov.lower() in md_lower for ov in other_vals)
        other_repeated = all(len(val_groups[ov]) >= 2 for ov in other_vals) or len(other_vals) >= 2

        if (other_clean and other_repeated) or other_in_md:
            lbl = _record_label(s_rec, s_idx)
            issues.append(
                f"In {rel_label}, array '{arr_name}' row {s_idx} ({lbl}) key '{k}' has singleton enum/status "
                f"outlier {s_val!r} alongside standard values {sorted(other_vals)}."
            )

    return issues


def _inspect_json_record_arrays(
    rel_label: str,
    data: Any,
    task_markdown: str = "",
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

        if n >= 5:
            q4_issues = _inspect_cross_row_consistency(
                rel_label, arr_name, records, all_keys, task_markdown=task_markdown
            )
            for issue in q4_issues:
                if issue not in advisories:
                    advisories.append(issue)

        if len(advisories) >= 6:
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
    task_markdown: str = "",
    output_dir: str = "outputs",
    ignore_dir_names: frozenset[str] = frozenset({"mock_audit", "mock_state"}),
) -> DeliverableAudit:
    """Inspect workspace deliverables for missing files, syntax errors, and structural anomalies."""
    ws = workspace_root.resolve()
    out_root = (ws / output_dir).resolve()
    resolved_md = _load_task_markdown(task_markdown, ws)

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
        s_decl = str(rel_decl).strip()
        if s_decl.startswith("/task/"):
            s_decl = s_decl[len("/task/") :]
        dp_cand = Path(s_decl)
        dp = dp_cand.resolve() if dp_cand.is_absolute() else (ws / dp_cand).resolve()
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
            j_adv, j_shape = _inspect_json_record_arrays(rel_lbl, parsed, task_markdown=resolved_md)
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
    declared_resolved = {
        (Path(str(d)) if Path(str(d)).is_absolute() else (ws / str(d).removeprefix("/task/"))).resolve()
        for d in (declared_paths or ())
    }
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


def verify_declared_deliverables(
    declared_paths: Sequence[str | Path] | Path,
    task_markdown: str = "",
    *,
    workspace_root: str | Path | None = None,
    presented_paths: Sequence[str | Path] | None = None,
) -> tuple[bool, list[str]]:
    """Verify declared deliverables and return ``(ok, issues)`` including Q4 cross-row checks."""
    if isinstance(declared_paths, (str, Path)):
        dp_cand = Path(declared_paths)
        if dp_cand.is_dir() and workspace_root is None:
            workspace_root = dp_cand
            raw_list: list[str | Path] = list(presented_paths or ())
        else:
            raw_list = [declared_paths]
    else:
        raw_list = list(declared_paths or ())

    if not raw_list and task_markdown:
        raw_list = list(extract_declared_deliverables(task_markdown))

    ws: Path | None = Path(workspace_root).resolve() if workspace_root is not None else None
    if ws is None:
        for item in raw_list:
            s = str(item).strip()
            if not s or s.startswith("/task/"):
                continue
            p = Path(s)
            if p.is_absolute() and p.exists():
                ws = p.resolve().parent.parent if p.resolve().parent.name == "outputs" else p.resolve().parent
                break
        if ws is None:
            ws = Path("/task").resolve() if Path("/task").is_dir() else Path.cwd().resolve()

    norm_declared: list[str] = []
    for item in raw_list:
        s = str(item).strip()
        if not s:
            continue
        if s.startswith("/task/"):
            norm_declared.append(s[len("/task/") :])
        else:
            p = Path(s)
            if p.is_absolute():
                norm_declared.append(_to_rel_display(p, ws))
            else:
                norm_declared.append(s)

    pres_list: list[str | Path] = (
        list(presented_paths) if presented_paths is not None else list(raw_list)
    )
    audit = audit_deliverables(
        ws,
        pres_list,
        declared_paths=norm_declared,
        task_markdown=task_markdown,
    )
    issues = [*audit.blocking_defects, *audit.advisories]
    return (not bool(issues), issues)
