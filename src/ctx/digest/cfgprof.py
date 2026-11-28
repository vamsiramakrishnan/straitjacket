"""config/v1 — structural outline for YAML, TOML, INI, HCL/Terraform, SQL,
Protobuf, and GraphQL files.

Pure stdlib (``tomllib`` + single-pass linear scanners). Strict detection
predicates exclude source code, logs, plain prose, JSON, CSV, and Markdown so
non-config payloads never false-positive here.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import Any

from ctx import bounds
from ctx.digest.base import MAX_PARSE_BYTES, DigestContext, Profile, from_intake
from ctx.digest.intakeprof import mappable_text
from ctx.digest.shapeutil import Quoter, Windows, examine_line, fit, inline_limit
from ctx.textutil import fmt_bytes, fmt_int

_TOML_SEC_RE = re.compile(r"^[ \t]*(\[\[?[^\]]+\]\]?)[ \t]*(?:#.*)?$")
_INI_SEC_RE = re.compile(r"^[ \t]*\[([A-Za-z0-9_.:/\- ]{1,64})\][ \t]*(?:[#;].*)?$")
_INI_KV_RE = re.compile(r"^[ \t]*[A-Za-z0-9_.-]{1,64}[ \t]*[=:][ \t]*.*$")
_HCL_BLOCK_RE = re.compile(
    r'^[ \t]*(resource|data|module|variable|output|provider|terraform|locals)\b([^{#\n]{0,120})\{'
)
_PROTO_SYNTAX_RE = re.compile(r'^\s*(?:syntax|edition)\s*=\s*["\']proto[23]["\']\s*;')
_PROTO_PKG_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;")
_PROTO_DECL_RE = re.compile(r"^[ \t]*(service|message|enum)\s+([A-Za-z_][\w]*)\s*\{")
_PROTO_RPC_RE = re.compile(r"^[ \t]*rpc\s+([A-Za-z_][\w]*)\s*\(")
_GQL_DECL_RE = re.compile(
    r"^(?:extend\s+)?(type|interface|input|enum|union|scalar|schema|query|mutation|subscription)\s+([A-Za-z_][\w]*)"
)
_SQL_STMT_RE = re.compile(
    r"^[ \t]*(CREATE(?:\s+OR\s+REPLACE)?\s+(?:TEMP(?:ORARY)?\s+)?(?:TABLE|VIEW|INDEX|FUNCTION|PROCEDURE|TRIGGER|SCHEMA|TYPE|MATERIALIZED\s+VIEW)"
    r"|ALTER\s+(?:TABLE|VIEW|INDEX|SCHEMA)|DROP\s+(?:TABLE|VIEW|INDEX)|WITH\s+[A-Za-z_][\w]*\s+AS"
    r"|INSERT\s+INTO|UPDATE\s+[A-Za-z_\"`][\w.\"`]*|DELETE\s+FROM|SELECT\b)\b(.*)$",
    re.I,
)
_SQL_IDENT_RE = re.compile(r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([A-Za-z_\"`][\w.\"`]{0,80})", re.I)
_YAML_TOP_KEY_RE = re.compile(r'^([A-Za-z_"][A-Za-z0-9_./"\-]{0,63}):(?:[ \t]+(.*))?$')
_YAML_KV_OR_ITEM_RE = re.compile(
    r"^(?:[ \t]*-[ \t]+.*|[ \t]+[A-Za-z0-9_./\"'\-]{1,64}:.*|[A-Za-z_\"][A-Za-z0-9_./\"\-]{0,63}:.*|---|\.\.\.)$"
)
_LOG_TS_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[\d{2}:\d{2}|(?:INFO|WARN|ERROR|DEBUG)\b)")
_CODE_LINE_RE = re.compile(
    r"^(?:def |class |import |from [A-Za-z_.\w]+ import |func |const |let |var |export |function |"
    r"public |private |protected |fn |pub |impl |#include\b|#!/)"
)


@dataclass(frozen=True)
class ConfigEntry:
    kind: str
    name: str
    start_line: int
    end_line: int
    detail: str = ""


@dataclass(frozen=True)
class ConfigDoc:
    kind: str  # yaml | toml | ini | hcl | sql | proto | graphql
    header_note: str
    entries: tuple[ConfigEntry, ...]
    parsed_lines: int


def _close_ranges(raw: list[tuple[str, str, int, str]], total: int) -> tuple[ConfigEntry, ...]:
    out: list[ConfigEntry] = []
    n = len(raw)
    for i, (kind, name, st, detail) in enumerate(raw):
        en = raw[i + 1][2] - 1 if i + 1 < n else total
        if en < st:
            en = st
        out.append(ConfigEntry(kind=kind, name=name, start_line=st, end_line=en, detail=detail))
    return tuple(out)


def _parse_proto(lines: list[str], total: int) -> ConfigDoc | None:
    has_syntax = False
    pkg = ""
    raw: list[tuple[str, str, int, str]] = []
    rpc_count = 0
    for idx, row in enumerate(lines, 1):
        head = examine_line(row)
        if _PROTO_SYNTAX_RE.match(head):
            has_syntax = True
            continue
        pm = _PROTO_PKG_RE.match(head)
        if pm and not pkg:
            pkg = pm.group(1)
            continue
        dm = _PROTO_DECL_RE.match(head)
        if dm and len(raw) < 300:
            raw.append((dm.group(1), dm.group(2), idx, ""))
            continue
        if _PROTO_RPC_RE.match(head):
            rpc_count += 1
    if not raw or (not has_syntax and not pkg and len(raw) < 3):
        return None
    note = f"package={pkg}" if pkg else "protobuf"
    if rpc_count:
        note += f" · {fmt_int(rpc_count)} rpcs"
    return ConfigDoc("proto", note, _close_ranges(raw, total), total)


def _parse_graphql(lines: list[str], total: int) -> ConfigDoc | None:
    raw: list[tuple[str, str, int, str]] = []
    code_hits = 0
    for idx, row in enumerate(lines, 1):
        head = examine_line(row)
        if _CODE_LINE_RE.match(head):
            code_hits += 1
            continue
        gm = _GQL_DECL_RE.match(head)
        if gm and len(raw) < 300:
            raw.append((gm.group(1), gm.group(2), idx, ""))
    if len(raw) < 3 or code_hits >= 2:
        return None
    return ConfigDoc("graphql", f"{fmt_int(len(raw))} definitions", _close_ranges(raw, total), total)


def _parse_hcl(lines: list[str], total: int) -> ConfigDoc | None:
    raw: list[tuple[str, str, int, str]] = []
    code_hits = 0
    for idx, row in enumerate(lines, 1):
        head = examine_line(row)
        if _CODE_LINE_RE.match(head):
            code_hits += 1
            continue
        hm = _HCL_BLOCK_RE.match(head)
        if hm and len(raw) < 300:
            btype = hm.group(1)
            labels = " ".join(hm.group(2).strip().split())
            raw.append((btype, f"{btype} {labels}".strip(), idx, ""))
    if len(raw) < 2 or code_hits >= 2:
        return None
    return ConfigDoc("hcl", f"{fmt_int(len(raw))} blocks", _close_ranges(raw, total), total)


def _parse_sql(lines: list[str], total: int) -> ConfigDoc | None:
    raw: list[tuple[str, str, int, str]] = []
    semicolons = 0
    code_hits = 0
    for idx, row in enumerate(lines, 1):
        head = examine_line(row)
        if _CODE_LINE_RE.match(head):
            code_hits += 1
            continue
        if ";" in head:
            semicolons += 1
        sm = _SQL_STMT_RE.match(head)
        if sm and len(raw) < 300:
            verb = " ".join(sm.group(1).upper().split())
            rest = sm.group(2).strip()
            im = _SQL_IDENT_RE.match(rest) if rest else None
            target = im.group(1) if im else ""
            label = f"{verb} {target}".strip() if target and not verb.startswith("SELECT") else verb
            raw.append(("sql", label, idx, ""))
    if len(raw) < 2 or semicolons < 2 or code_hits >= 2:
        return None
    return ConfigDoc("sql", f"{fmt_int(len(raw))} statements", _close_ranges(raw, total), total)


def _parse_toml(text: str, lines: list[str], total: int) -> ConfigDoc | None:
    # Fast pre-check before invoking stdlib tomllib.
    sec_hits: list[tuple[str, str, int, str]] = []
    eq_hits = 0
    for idx, row in enumerate(lines, 1):
        head = examine_line(row)
        tm = _TOML_SEC_RE.match(head)
        if tm:
            sec_hits.append(("section", tm.group(1), idx, ""))
        elif "=" in head and not head.lstrip().startswith("#"):
            eq_hits += 1
    if not sec_hits and eq_hits < 4:
        return None
    try:
        parsed = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    if not sec_hits:
        # Flat TOML: list top-level keys
        for idx, row in enumerate(lines, 1):
            head = examine_line(row).strip()
            if "=" in head and not head.startswith("#") and len(sec_hits) < 200:
                key = head.split("=", 1)[0].strip()
                if key:
                    sec_hits.append(("key", key, idx, ""))
    return ConfigDoc(
        "toml",
        f"{fmt_int(len(parsed))} top-level keys · {fmt_int(len(sec_hits))} sections",
        _close_ranges(sec_hits, total),
        total,
    )


def _parse_ini(lines: list[str], total: int) -> ConfigDoc | None:
    sections: list[tuple[str, str, int, str]] = []
    kv_count = 0
    non_empty = 0
    code_hits = 0
    for idx, row in enumerate(lines, 1):
        head = examine_line(row)
        s = head.strip()
        if not s or s.startswith(("#", ";")):
            continue
        non_empty += 1
        if _CODE_LINE_RE.match(s):
            code_hits += 1
            continue
        im = _INI_SEC_RE.match(head)
        if im and len(sections) < 300:
            sections.append(("section", f"[{im.group(1)}]", idx, ""))
        elif _INI_KV_RE.match(head):
            kv_count += 1
    if len(sections) < 2 or kv_count < 4 or code_hits >= 2:
        return None
    if non_empty > 0 and (len(sections) + kv_count) * 100 < non_empty * 65:
        return None
    return ConfigDoc("ini", f"{fmt_int(len(sections))} sections · {fmt_int(kv_count)} keys", _close_ranges(sections, total), total)


def _parse_yaml(lines: list[str], total: int) -> ConfigDoc | None:
    if lines and lines[0].lstrip("\ufeff").startswith("["):
        return None
    # Detect multi-document YAML upfront so doc #1 is emitted as a document
    # block rather than mixing doc #1's keys with doc #2..N blocks.
    sep_lines = [i for i, row in enumerate(lines, 1) if examine_line(row).strip() == "---"]
    non_empty_before_first = any(
        examine_line(lines[k]).strip() and not examine_line(lines[k]).strip().startswith("#")
        for k in range(0, (sep_lines[0] - 1) if sep_lines else 0)
    )
    is_multi_doc = len(sep_lines) >= 2 or (len(sep_lines) == 1 and non_empty_before_first)

    entries: list[tuple[str, str, int, str]] = []
    yaml_lines = 0
    non_empty = 0
    code_hits = 0
    log_hits = 0
    docs = 0
    doc_start = 1
    doc_first_key = ""
    k8s_kind = ""
    k8s_name = ""
    in_k8s_meta = False

    def _flush_doc(doc_num: int, st: int) -> None:
        if len(entries) >= 300:
            return
        if k8s_kind:
            lbl = f"{k8s_kind}/{k8s_name}" if k8s_name else k8s_kind
        else:
            lbl = doc_first_key or f"document #{doc_num}"
        entries.append(("doc", lbl, st, f"doc #{doc_num}"))

    for idx, row in enumerate(lines, 1):
        head = examine_line(row)
        s = head.strip()
        if not s or s.startswith("#"):
            continue
        non_empty += 1
        if _CODE_LINE_RE.match(s):
            code_hits += 1
            continue
        if _LOG_TS_RE.match(s):
            log_hits += 1
            continue
        if s == "---":
            yaml_lines += 1
            if is_multi_doc:
                if docs > 0:
                    _flush_doc(docs, doc_start)
                docs += 1
                doc_start = idx
                doc_first_key = ""
                k8s_kind = ""
                k8s_name = ""
                in_k8s_meta = False
            continue
        if is_multi_doc and docs == 0:
            docs = 1
            doc_start = idx
        if _YAML_KV_OR_ITEM_RE.match(head):
            yaml_lines += 1
        km = _YAML_TOP_KEY_RE.match(head)
        if km:
            key = km.group(1)
            val = (km.group(2) or "").strip()
            if not doc_first_key:
                doc_first_key = key
            if key == "kind" and val:
                k8s_kind = val.strip("\"'")
            in_k8s_meta = key == "metadata"
            if not is_multi_doc and len(entries) < 300:
                entries.append(("key", key, idx, val if val and len(val) <= 32 else ""))
        elif in_k8s_meta and head.startswith(("  name:", "    name:")):
            k8s_name = head.split(":", 1)[1].strip().strip("\"'")
            in_k8s_meta = False

    if is_multi_doc and docs > 0:
        _flush_doc(docs, doc_start)

    if code_hits >= 3 or log_hits * 5 > non_empty:
        return None
    if non_empty < 8 or yaml_lines * 100 < non_empty * 65:
        return None
    if len(entries) < 2:
        return None
    note = f"{fmt_int(docs)} documents" if is_multi_doc else f"{fmt_int(len(entries))} top-level keys"
    return ConfigDoc("yaml", note, _close_ranges(entries, total), total)


def parse_config(text: str) -> ConfigDoc | None:
    """Detect and parse structured config or schema text."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total < 4:
        return None
    first = lines[0].lstrip("\ufeff")
    if first.startswith(("#!", "{", "<")):
        return None

    for fn in (_parse_proto, _parse_graphql, _parse_hcl, _parse_sql):
        doc = fn(lines, total)
        if doc is not None:
            return doc
    toml_doc = _parse_toml(text, lines, total)
    if toml_doc is not None:
        return toml_doc
    ini_doc = _parse_ini(lines, total)
    if ini_doc is not None:
        return ini_doc
    return _parse_yaml(lines, total)


def _render_config_lines(doc: ConfigDoc, q: Quoter, width: int, max_items: int) -> list[str]:
    out = [f"config ({doc.kind}): {doc.header_note}"]
    shown = doc.entries[:max_items]
    out.append(f"structure ({len(shown)} of {fmt_int(len(doc.entries))} shown):")
    for e in shown:
        n_lines = e.end_line - e.start_line + 1
        det = f" = {q(e.detail, 28)}" if e.detail else ""
        out.append(
            f"  L{e.start_line}-L{e.end_line} {q(e.name, width)}{det} ({fmt_int(n_lines)}L)"
        )
    if len(doc.entries) > len(shown):
        out.append(f"  … +{fmt_int(len(doc.entries) - len(shown))} more entries omitted")
    return out


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
    doc = parse_config(mappable_text(data.decode("utf-8", "replace"), not partial))
    if doc is None:
        return None
    q = Quoter(68, policy)
    lines = _render_config_lines(doc, q, 68, bounds.count(max_items))
    lines.extend(q.note())
    return "\n".join(lines)


class ConfigProfile(Profile):
    version = "config/v1"

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
        doc = parse_config(mappable_text(view.text, view.parsed_fully))
        if doc is None:
            return None
        return f"structured {doc.kind} ({len(doc.entries)} blocks)"

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        partial = not view.parsed_fully
        text = mappable_text(view.text, view.parsed_fully)
        doc = parse_config(text)
        if doc is None:
            return "\n".join(list(ctx.header_lines()) + ["config: unreadable"])
        handle = "run:PENDING#stdout"
        rid = "run:PENDING"
        total_lines = view.lines
        policy = ctx.ws.config.redaction
        windows = Windows(ctx, text, total_lines)

        def build(level: int) -> str:
            max_items, width = ((28, 70), (16, 58), (10, 46), (6, 36), (3, 28))[level]
            q = Quoter(width, policy)
            out = (
                [ctx.intake_line(total=partial) + f" · {doc.kind}"]
                if from_intake(ctx)
                else list(ctx.header_lines())
            )
            out.extend(_render_config_lines(doc, q, width, max_items))
            if partial:
                out.append(
                    f"coverage: parsed L1-L{fmt_int(doc.parsed_lines)} of {fmt_int(total_lines)} "
                    f"lines (parse cap {fmt_bytes(MAX_PARSE_BYTES)})"
                )
            else:
                out.append(f"coverage: all {fmt_int(total_lines)} lines parsed")

            sugg: list[str] = []
            if doc.entries:
                e0 = doc.entries[0]
                end0 = windows.fit_end(e0.start_line, e0.end_line)
                sugg.append(f"ctx get {handle} --lines {e0.start_line}:{end0}")
                if len(doc.entries) > 1:
                    e1 = doc.entries[1]
                    end1 = windows.fit_end(e1.start_line, e1.end_line)
                    cmd1 = f"ctx get {handle} --lines {e1.start_line}:{end1}"
                    if cmd1 not in sugg:
                        sugg.append(cmd1)
            sugg.append(f"ctx search {rid} '<pattern>' --context 2")
            return "\n".join(out + self.next_lines(ctx, sugg) + q.note())

        return fit(ctx, build, 5)


__all__ = ["ConfigDoc", "ConfigEntry", "ConfigProfile", "outline_file", "parse_config"]
