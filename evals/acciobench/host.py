"""Tool execution host supporting paired `raw` baseline and `sj` Straitjacket arms."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import csv
import glob as pyglob
import hashlib
import html as htmlmod
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
from typing import Any
import urllib.parse
import urllib.request

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _REPO_SRC):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ctx.deliverables import audit_deliverables  # noqa: E402
from ctx.hook import ctx_command_words, ctx_read_kind  # noqa: E402
from ctx.residence import extract_mutation_receipts  # noqa: E402
from ctx.selfcmd import SAFE_PATH_FLAG  # noqa: E402
from ctx.sessiondir import session_reads_path  # noqa: E402
from ctx.toolresult import prepend_structured_data_shape  # noqa: E402

from evals.acciobench.browser import BrowserSession
from evals.acciobench.config import BenchConfig, RunMetrics, resolve_ctx_bin
from evals.acciobench.ports import CompiledPortMap, rewrite_ports_surgical
from evals.acciobench.schemas import (
    MUTATING_BROWSER_TOOLS,
    MUTATING_SHELL_RE,
    READONLY_TOOLS,
    canonical_tool_sig,
)

__all__ = [
    "IMAGE_EXTS",
    "ToolHost",
    "bwrap_sandbox_executable",
    "extract_pdf_text",
    "format_numbered_lines",
    "has_user_deliverables",
    "parse_pdf_pages",
]

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_TASK_PATH_RE = re.compile(r"(^|(?<![A-Za-z0-9_.-]))/task(?=/|$|[^A-Za-z0-9_-])")
# `/tmp` as a whole component at the START of a path word: preceded by the start, a
# blank, a quote, `=`, `:`, `(`, a backtick or a shell operator (not by `}`, `)`, a
# `$VAR`, a path or word character: not ${X}/tmp, ~/tmp, /var/tmp, http://x/tmp/y),
# and followed by `/`, a blank, a quote, a shell operator or the end (not /tmpfoo,
# /tmp.txt, /tmp~). The first component below it is captured.
_TMP_PATH_RE = re.compile(
    r"(?<![^\s'\"=:(;|&<>`])/tmp(?=[/\s'\"`;|&<>()]|$)(?:/([^/\s'\"`;|&<>(){}$\\]*))?"
)
_DIGEST_SIZE_RE = re.compile(r"stdout:\s*\d+\s*lines\s*\u00b7\s*([\d.]+)\s*(B|KiB|MiB)")
_SHELL_HISTORY_RE = re.compile(r"\.(?:bash|zsh)_history\b")
_INLINE_PY_C_RE = re.compile(r"\bpython3?(?:\.\d+)?\b[^\n]*\s-c\s")
_PY_PARSE_ERR_RE = re.compile(
    r"(?:SyntaxError:|IndentationError:|unterminated string literal|unexpected EOF while parsing)"
)
_BWRAP_PROBE_CACHE: tuple[bool, str | None] = (False, None)
_ROOT_BWRAP_MOUNTS_CACHE: list[str] | None = None


def _root_bwrap_mounts() -> list[str]:
    global _ROOT_BWRAP_MOUNTS_CACHE
    if _ROOT_BWRAP_MOUNTS_CACHE is not None:
        return list(_ROOT_BWRAP_MOUNTS_CACHE)
    mounts: list[str] = []
    skip = {"proc", "dev", "tmp", "task"}
    try:
        entries = sorted(os.listdir("/"))
    except OSError:
        entries = []
    for entry in entries:
        if entry in skip:
            continue
        p = "/" + entry
        if os.path.islink(p):
            try:
                mounts.extend(["--symlink", os.readlink(p), p])
            except OSError:
                continue
        elif os.path.isdir(p) or os.path.isfile(p):
            mounts.extend(["--ro-bind-try", p, p])
    _ROOT_BWRAP_MOUNTS_CACHE = mounts
    return list(mounts)


def bwrap_sandbox_executable() -> str | None:
    """Return usable ``bwrap`` binary path on Linux when mount + PID namespaces work, else ``None``."""
    global _BWRAP_PROBE_CACHE
    if os.environ.get("ACCIOBENCH_NO_BWRAP", "").strip().lower() in ("1", "true", "yes"):
        return None
    probed, cached_bin = _BWRAP_PROBE_CACHE
    if probed:
        return cached_bin
    if sys.platform != "linux":
        _BWRAP_PROBE_CACHE = (True, None)
        return None
    bwrap_bin = shutil.which("bwrap")
    if not bwrap_bin:
        _BWRAP_PROBE_CACHE = (True, None)
        return None
    try:
        cp = subprocess.run(
            [
                bwrap_bin,
                "--die-with-parent",
                "--unshare-pid",
                *_root_bwrap_mounts(),
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ok_bin = bwrap_bin if cp.returncode == 0 else None
    except Exception:
        ok_bin = None
    _BWRAP_PROBE_CACHE = (True, ok_bin)
    return ok_bin
_UNTITLED_CREATE_RE = re.compile(
    r'(?:Created|Added)\s+(?:page|doc|document|database|sheet)\s+"Untitled"\s*\(([A-Za-z0-9_-]{4,64})\)',
    re.I,
)
_RESOURCE_CLEANUP_RE = re.compile(
    r"\b(?:trash|trashed|deleted|archived|updated|renamed|moved\s+to\s+trash|unlinked|removed)\b",
    re.I,
)
_EXT_SERVICE_TARGET_RE = re.compile(
    r"\b(?:stripe|todoist|jira|box|dws|gws|ntn|shopify|gh)\b"
    r"|https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d+"
    r"|[\"']method[\"']\s*:\s*[\"']tools/call[\"']",
    re.I,
)
_EXT_CLI_LINE_RE = re.compile(
    r"^\s*(?:env\s+[A-Z0-9_]+=\S+\s+)*(stripe|todoist|jira|box|dws|gws|ntn|shopify|gh)\s+(.+?)\s*$",
    re.I,
)
_NON_IDEMPOTENT_CLI_VERB_RE = re.compile(
    r"\b(?:create|add|upload|link|append|insert|send|draft|duplicate|copy)\b",
    re.I,
)
_CLEANUP_CLI_VERB_RE = re.compile(
    r"\b(?:delete|trash|unlink|remove|rm|void|cancel)\b",
    re.I,
)
_READBACK_CLI_VERB_RE = re.compile(
    r"(?:\b(?:list|ls|get|show|view|search|find|status|retrieve|info|read|export|whoami|query|resolve)\b|:items\b)",
    re.I,
)
_MUTATING_CLI_VERB_RE = re.compile(
    r"\b(?:create|update|delete|add|complete|close|reopen|move|assign|comment|transition|"
    r"upload|mkdir|cp|mv|rm|share|collaborations|append|replace|insert|batch|send|draft|"
    r"trash|archive|finalize|pay|void|cancel|confirm|capture|refund|edit|patch|post|put|"
    r"submit|publish|seed|import|modify|link|resolve|star|unstar|restore)\b",
    re.I,
)
_HTTP_MUTATION_RE = re.compile(
    r"\bcurl\b[^\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data|-d\s|-F\s)"
    r"|urllib\.request\.Request\b.*(?:\bdata\s*=|\bmethod\s*=\s*['\"](?:POST|PUT|PATCH|DELETE))"
    r"|\b(?:requests|httpx)\.(?:post|put|patch|delete)\b",
    re.I | re.S,
)
_HTTP_READBACK_RE = re.compile(
    r"\b(?:requests|httpx)\.get\b"
    r"|urllib\.request\.urlopen\b"
    r"|\bcurl\b(?![^\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data|-d\s|-F\s))[^\n]*https?://",
    re.I,
)
_MCP_READONLY_CALL_RE = re.compile(
    r"[\"']method[\"']\s*:\s*[\"'](?:tools/list|resources/list|resources/read)[\"']"
    r"|[\"']method[\"']\s*:\s*[\"']tools/call[\"'].*[\"']name[\"']\s*:\s*[\"'][A-Za-z0-9_.-]*\b(?:list|get|read|search|query|fetch|find|inspect|describe|status)[A-Za-z0-9_.-]*[\"']",
    re.I | re.S,
)
_MCP_MUTATING_CALL_RE = re.compile(
    r"[\"']method[\"']\s*:\s*[\"']tools/call[\"'].*[\"']name[\"']\s*:\s*[\"'][A-Za-z0-9_.-]*(?:create|update|delete|remove|modify|send|reply|draft|star|unstar|archive|trash|label|add|close|reopen|complete|upload|insert|append|replace|batch|transition|assign|comment|link|merge)[A-Za-z0-9_.-]*[\"']",
    re.I | re.S,
)
_NAMED_CREATE_RECEIPT_RE = re.compile(
    r'(?:✓|✔|SUCCESS:?)\s*(Created|Uploaded|Added|Linked)\s+'
    r'(page|file|doc|document|sheet|database|task|issue|draft|folder)\s+"([^"]{2,120})"',
    re.I,
)
_DOMAIN_CLIS: tuple[str, ...] = ("stripe", "jira", "dws", "box", "gws", "ntn", "todoist")
_CLI_SUBCOMMAND_REFS: dict[str, str] = {
    "stripe": (
        "stripe <resource> <op> [--k=v | -d \"nested[k]=v\"]; ops: customers, products, prices, "
        "subscriptions, invoices, invoiceitems, charges, refunds; credit/negative invoice adjustments: "
        "`stripe invoiceitems create --customer=CUS_ID --currency=usd --amount=-CENTS --description=\"...\"`; "
        "`stripe invoices finalize-invoice INV_ID` (all amounts integer cents)."
    ),
    "jira": (
        "jira issue list [--raw] | view KEY | create | edit KEY | comment add KEY | "
        "move KEY \"<Exact Case-Sensitive State>\" | link INWARD OUTWARD LINK_TYPE; jira project list."
    ),
    "dws": (
        "dws <service> <cmd> [flags]; doc read --node <id|url> --content-format markdown|jsonml; "
        "doc list|create|comment|copy; doc block list|insert|update|delete --node <id>; dws schema for exact params."
    ),
    "box": (
        "box <resource>:<op>; folders:items 0 | folders:create NAME --parent-id ID; "
        "files:get ID | files:upload PATH --parent-id ID; collaborations:create; comments:create; tasks:create; shared-links:create."
    ),
    "gws": (
        "gws sheets get-text --spreadsheet-id <id|url> | gws slides <cmd> --presentation-id <id> "
        "(run `gws sheets --help` / `gws slides --help` once upfront for exact subcommands)."
    ),
    "ntn": (
        "ntn pages get|create|update <page-id> --content <md>; "
        "ntn datasources resolve <db-id> | query <ds-id>; ntn files list; ntn api for raw calls."
    ),
    "todoist": (
        "todoist list | show <id> | add ... | modify <id> [--section-name ...] [--label-names ...] | "
        "close <id> | delete <id> | projects | sections."
    ),
}
_SHELL_CLI_INV_RE = re.compile(
    r"(?:^|&&|\|\||;|\||\bdo\b|\bthen\b|\$\()\s*(?:env\s+[A-Z0-9_]+=\S+\s+)*"
    r"(stripe|todoist|jira|box|dws|gws|ntn|shopify|gh)(?:\s+([^\n;|&`]*)|$)",
    re.I | re.M,
)
_PY_LIST_CLI_RE = re.compile(
    r"\[\s*[\"'](stripe|todoist|jira|box|dws|gws|ntn|shopify|gh)[\"']\s*,\s*([^\]]+)\]",
    re.I,
)
_PY_STR_CLI_RE = re.compile(
    r"""[fFrR]{0,2}(["'])\s*(?:env\s+[A-Z0-9_]+=\S+\s+)*(stripe|todoist|jira|box|dws|gws|ntn|shopify|gh)\s+([^"'\n]+?)\1""",
    re.I,
)
_SCRATCH_PROBE_CLI_CREATES_RE = re.compile(
    r"(?:files:(?:upload|create)|folders:create|\b(?:create|upload|add|mkdir|new|copy)\b)",
    re.I,
)
_PROJ_KEY_RE = re.compile(r"\bPROJ-\d+\b")
_BOX_17943_ID_RE = re.compile(r"\b17943(?:\d{7}|\d{9,12})\b")
_STRIPE_TRIGGER_REST_RE = re.compile(r"^(?:--\S+(?:=\S+|\s+\S+)?\s+)*trigger(?:\s|$)", re.I)
_GDOCS_ROLLUP_LINE_RE = re.compile(r"^\s*([^|\n]+?)\s*\|\s*High\+Open\s*:\s*(\d+)\b", re.I | re.M)
_FLAG_CLUSTER_CMDS: dict[str, frozenset[str]] = {
    "ls": frozenset("aAdFhHilLnrRSt1"),
    "grep": frozenset("cEFhHilLnqrRvwx"),
    "sort": frozenset("fhHnruV"),
    "wc": frozenset("clLmw"),
    "du": frozenset("achkmsx"),
    "ps": frozenset("aefux"),
    "cat": frozenset("bns"),
    "head": frozenset("qv"),
    "tail": frozenset("fqv"),
}
_EMBEDDED_CLI_ERR_RE = re.compile(
    r"(?:\bcommand not found\b|^\s*Usage:\s|\bHTTP(?:/[\d.]+)?\s+[45]\d\d\b)",
    re.I | re.M,
)
_SECTION_HEADER_RE = re.compile(r"^\s*\[section:\s*([^\]]+?)\s*\]\s*$")


# Shell tokens for the dedup signature: blanks, control/redirection operators, and words
# (quoted strings and backslash escapes kept verbatim). `#` is ordinary inside a word.
_DEDUP_TOKEN_RE = re.compile(
    r"(?P<ws>[ \t]+)"
    r"|(?P<op>&&|\|\||;;|>>|>&|<&|>\||&>|\|&|<>|[;|&()<>])"
    r"|(?P<word>(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\\.|[^\s'\"\\;|&()<>])+)"
    r"|(?P<bad>.)",
    re.S,
)
_DEDUP_CONTROL_OPS = frozenset({";", "&&", "||", "|", "&", "|&", ";;", "(", ")"})
_DEDUP_BLANK_FREE_OPS = frozenset({";", "&&", "||", "|", "&", "|&", ";;"})
_SHORT_FLAGS_RE = re.compile(r"^-[A-Za-z0-9]+$")


def _normalize_bash_cmd_for_dedup(cmd: str) -> str:
    """Signature of a simple shell command for the readonly bash dedup.

    Only differences a shell ignores are normalized: runs of blanks, a trailing comment
    (``#`` at the START of a word outside quotes, as the shell reads it; inside a word
    such as ``run:ID#stdout`` or ``a#b`` it is text), and the order/clustering of
    known no-argument short flags (``ls -la`` == ``ls -a -l``). Tokens the shell reads
    as one word stay one word, and tokens written without blanks between them are
    joined without blanks, so two commands that differ in any word -- a handle, a
    ``--lines`` range -- never share a signature."""
    s = (cmd or "").strip()
    if not s:
        return ""
    if "<<" in s or "\n" in s:
        return "\n".join(ln.rstrip() for ln in s.splitlines() if ln.strip())
    tokens: list[tuple[str, bool]] = []  # (text, preceded by blanks)
    spaced = False
    for m in _DEDUP_TOKEN_RE.finditer(s):
        kind = m.lastgroup
        if kind == "ws":
            spaced = True
            continue
        if kind == "bad":  # an unterminated quote or a trailing backslash
            return " ".join(s.split())
        text = m.group()
        if kind == "word" and text.startswith("#"):
            break  # a comment runs to the end of the line
        if kind == "op" and text in _DEDUP_BLANK_FREE_OPS:
            # Blanks around these never change meaning (`a|b` == `a | b`).
            tokens.append((text, True))
            spaced = True
            continue
        tokens.append((text, spaced or not tokens))
        spaced = kind == "op" and text not in ("(", ")")  # `> out` == `>out`

    out: list[tuple[str, bool]] = []
    cmd_word: str | None = None
    i = 0
    while i < len(tokens):
        tok, sp = tokens[i]
        if tok in _DEDUP_CONTROL_OPS:
            out.append((tok, sp))
            cmd_word = None
            i += 1
            continue
        if cmd_word is None and not tok.startswith(("-", "'", '"', "<", ">")) and "=" not in tok:
            cmd_word = Path(tok).name.lower()
            out.append((tok, sp))
            i += 1
            continue
        allowed = _FLAG_CLUSTER_CMDS.get(cmd_word or "")
        if allowed and sp and _SHORT_FLAGS_RE.match(tok):
            flags: list[str] = []
            j = i
            while j < len(tokens):
                t, tsp = tokens[j]
                if tsp and _SHORT_FLAGS_RE.match(t) and all(ch in allowed for ch in t[1:]):
                    flags.extend(f"-{ch}" for ch in t[1:])
                    j += 1
                else:
                    break
            if flags:
                out.extend((f, True) for f in sorted(set(flags)))
                i = j
                continue
        out.append((tok, sp))
        i += 1
    return "".join((" " + t if (sp and k) else t) for k, (t, sp) in enumerate(out))


def _extract_cli_invocations(cmd: str) -> list[tuple[str, str, int]]:
    """Extract ``(cli_bin, rest_args, char_pos)`` across shell segments and inline script calls."""
    if not cmd:
        return []
    hits: list[tuple[str, str, int]] = []
    seen_pos: set[int] = set()

    # Track shell lines outside non-shell heredoc bodies cleanly so prose comments don't trigger.
    heredoc_end: str | None = None
    offset = 0
    for ln in cmd.splitlines(keepends=True):
        s_ln = ln.strip()
        if heredoc_end is not None:
            if s_ln == heredoc_end:
                heredoc_end = None
            offset += len(ln)
            continue
        m_hd = re.search(r"<<-?\s*['\"]?([A-Za-z0-9_]+)['\"]?", s_ln)
        if m_hd and m_hd.group(1).upper() not in ("SH", "BASH"):
            heredoc_end = m_hd.group(1)
        if s_ln and not s_ln.startswith("#"):
            for m in _SHELL_CLI_INV_RE.finditer(ln):
                pos = offset + m.start(1)
                if pos not in seen_pos:
                    seen_pos.add(pos)
                    hits.append((m.group(1).lower(), " ".join((m.group(2) or "").split()), pos))
        offset += len(ln)

    for m in _PY_LIST_CLI_RE.finditer(cmd):
        pos = m.start(1)
        if pos not in seen_pos:
            seen_pos.add(pos)
            arg_toks = [
                a or b or c
                for a, b, c in re.findall(
                    r'"([^"\n]+)"|\'([^\'\n]+)\'|([A-Za-z_][A-Za-z0-9_]*)',
                    m.group(2),
                )
            ]
            hits.append((m.group(1).lower(), " ".join(arg_toks), pos))

    for m in _PY_STR_CLI_RE.finditer(cmd):
        pos = m.start(2)
        if pos not in seen_pos:
            seen_pos.add(pos)
            hits.append((m.group(2).lower(), " ".join(m.group(3).split()), pos))

    hits.sort(key=lambda item: item[2])
    return hits


def _parse_jira_link_call(rest: str) -> tuple[str, str, str] | None:
    """Parse ``jira issue link INWARD OUTWARD LINK_TYPE`` positional tuple if matched."""
    try:
        toks = shlex.split(rest)
    except ValueError:
        toks = rest.split()
    pos_args: list[str] = []
    skip_next = False
    for tok in toks:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if tok in ("-t", "--type", "-o", "--output") and "=" not in tok:
                skip_next = True
            continue
        pos_args.append(tok)
    if len(pos_args) >= 5 and pos_args[0].lower() == "issue" and pos_args[1].lower() == "link":
        src = pos_args[2].strip().upper()
        tgt = pos_args[3].strip().upper()
        ltype = " ".join(pos_args[4:]).strip().lower()
        if src and tgt and ltype:
            return (src, tgt, ltype)
    return None


def _is_synthetic_probe_name(token: str) -> bool:
    """Return True for throwaway scratch probe filenames/titles (test*, dummy*, sample*, tmp*, Untitled)."""
    cleaned = token.strip().strip("\"'`").rstrip(",;")
    if not cleaned:
        return False
    base = Path(cleaned).name.lower()
    return base == "untitled" or base.startswith(("test", "dummy", "sample", "tmp"))


def _resolve_inline_probe_tokens(cmd_rw: str, toks: list[str]) -> list[str]:
    """Expand CLI argument tokens plus simple inline shell/Python variable assignments."""
    candidates: list[str] = []
    var_rx = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?|\{([A-Za-z_][A-Za-z0-9_]*)\}")
    bare_ident_rx = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for raw_tok in toks[1:]:
        val = raw_tok.split("=", 1)[1] if raw_tok.startswith("-") and "=" in raw_tok else raw_tok
        if raw_tok.startswith("-") and "=" not in raw_tok:
            continue
        candidates.append(val)
        var_names = [a or b for a, b in var_rx.findall(val)]
        if bare_ident_rx.match(val):
            var_names.append(val)
        for vname in dict.fromkeys(var_names):
            assign_rx = re.compile(rf"""\b{re.escape(vname)}\s*=\s*["']([^"'\n]+)["']""")
            candidates.extend(assign_rx.findall(cmd_rw))
    return candidates


def _format_write_receipt(path: Path, text: str) -> str:
    """Render a zero-extra-tool ``[write-receipt: ...]`` line summarizing file lines, bytes, and schema."""
    n_lines = len(text.splitlines())
    n_bytes = len(text.encode("utf-8"))
    suf = path.suffix.lower()
    schema = "text"

    stripped = text.lstrip()
    if suf == ".json" or (suf not in (".csv", ".tsv", ".md", ".txt", ".py", ".sh") and stripped.startswith(("{", "["))):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                keys = [str(k)[:24] for k in list(obj.keys())[:8]]
                more = f"+{len(obj) - 8}" if len(obj) > 8 else ""
                schema = f"schema=json-dict keys=[{', '.join(keys)}{more}]"
            elif isinstance(obj, list):
                first_keys = ""
                if obj and isinstance(obj[0], dict):
                    ks = [str(k)[:20] for k in list(obj[0].keys())[:6]]
                    first_keys = f" keys=[{', '.join(ks)}]"
                schema = f"schema=json-list len={len(obj)}{first_keys}"
            else:
                schema = f"schema=json-{type(obj).__name__}"
        except Exception as exc:
            schema = f"schema=INVALID-JSON({type(exc).__name__})"
    elif suf == ".jsonl":
        schema = f"schema=jsonl lines={n_lines}"
    elif suf in (".csv", ".tsv"):
        delim = "\t" if suf == ".tsv" else ","
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
            if rows:
                cols = [c.strip()[:24] for c in rows[0][:8]]
                more_c = f"+{len(rows[0]) - 8}" if len(rows[0]) > 8 else ""
                schema = f"schema={suf[1:]} rows={max(0, len(rows) - 1)} cols=[{', '.join(cols)}{more_c}]"
            else:
                schema = f"schema={suf[1:]} rows=0"
        except Exception as exc:
            schema = f"schema=INVALID-{suf[1:].upper()}({type(exc).__name__})"
    else:
        schema = f"schema={suf[1:] or 'text'}"

    return f"[write-receipt: lines={n_lines}, bytes={n_bytes}, {schema}]"


def _format_section_receipt(out_txt: str) -> str:
    """Summarize ``[section: <name>]`` blocks in inline bash stdout if present."""
    sections: list[tuple[str, int]] = []
    cur_name: str | None = None
    cur_lines = 0
    for ln in out_txt.splitlines():
        m = _SECTION_HEADER_RE.match(ln)
        if m:
            if cur_name is not None:
                sections.append((cur_name, cur_lines))
            cur_name = m.group(1).strip()[:36]
            cur_lines = 0
        elif cur_name is not None and ln.strip():
            cur_lines += 1
    if cur_name is not None:
        sections.append((cur_name, cur_lines))
    if not sections:
        return ""
    preview = ", ".join(f"{name}: {cnt} lines" for name, cnt in sections[:10])
    more = f", +{len(sections) - 10} more" if len(sections) > 10 else ""
    return f"[section-receipt: {len(sections)} section(s) ({preview}{more})]"


def format_numbered_lines(
    lines: list[str],
    start_idx: int,
    total_lines: int,
    extra_note: str = "",
) -> str:
    if not lines:
        return f"(empty slice; Total: {total_lines} lines{extra_note}.)"
    rendered = [f"{i:05d}| {ln}" for i, ln in enumerate(lines, start=start_idx + 1)]
    return "\n".join(rendered) + f"\n\n(Total: {total_lines} lines{extra_note}.)"


def parse_pdf_pages(pages_spec: str | None) -> tuple[int | None, int | None]:
    if not pages_spec:
        return None, None
    s = str(pages_spec).strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.match(r"^(\d+)$", s)
    if m2:
        p = int(m2.group(1))
        return p, p
    return None, None


def extract_pdf_text(pdf_path: Path, pages_spec: str | None) -> tuple[str, str]:
    pdftotext_bin = shutil.which("pdftotext") or "/usr/bin/pdftotext"
    p_start, p_end = parse_pdf_pages(pages_spec)
    cmd = [pdftotext_bin, "-layout"]
    note = ""
    if p_start is not None and p_end is not None:
        cmd.extend(["-f", str(p_start), "-l", str(p_end)])
        note = f", pages {p_start}-{p_end}"
    cmd.extend([str(pdf_path), "-"])
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return "ERROR: pdftotext binary is not installed.", note
    if cp.returncode != 0 and not cp.stdout:
        return f"ERROR: pdftotext failed: {cp.stderr.strip()[:300]}", note
    return cp.stdout, note


def has_user_deliverables(outputs_dir: Path) -> bool:
    if not outputs_dir.exists():
        return False
    for p in outputs_dir.rglob("*"):
        if p.is_file() and "mock_audit" not in p.parts and "mock_state" not in p.parts:
            return True
    return False


def _estimate_digest_raw_bytes(digest_text: str) -> int:
    m = _DIGEST_SIZE_RE.search(digest_text)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "KiB":
        return int(val * 1024)
    if unit == "MiB":
        return int(val * 1024 * 1024)
    return int(val)


class ToolHost:
    """Executes tool calls inside an isolated trial sandbox for `arm='sj'` or `arm='raw'`."""

    def __init__(
        self,
        config: BenchConfig,
        run_dir: Path,
        meta_dir: Path,
        port_map: dict[int, int] | CompiledPortMap,
        mock_env: dict[str, str],
        metrics: RunMetrics,
        browser: BrowserSession | None,
        *,
        arm: str = "sj",
        max_steps: int = 135,
        declared_deliverables: list[str] | None = None,
        task_markdown: str = "",
        use_bwrap: bool | None = None,
    ):
        arm_norm = arm.strip().lower()
        if arm_norm not in ("sj", "raw"):
            raise ValueError(f"Unsupported arm {arm!r}; expected 'sj' or 'raw'")

        self.config = config
        self.run_dir = run_dir.resolve()
        self.meta_dir = meta_dir.resolve()
        self.compiled_ports: CompiledPortMap = (
            port_map if isinstance(port_map, CompiledPortMap) else CompiledPortMap(dict(port_map or {}))
        )
        self.port_map: dict[int, int] = self.compiled_ports.mapping
        self.mock_env = dict(mock_env or {})
        self.metrics = metrics
        self.browser = browser
        self.arm = arm_norm
        self.is_sj = arm_norm == "sj"
        self.max_steps = max_steps

        self.completed: bool = False
        self.step: int = 0
        self.last_call_was_dup: bool = False
        self.mutation_epoch: int = 0
        self.seen: dict[tuple[int, str], tuple[int, dict[str, Any]]] = {}
        self.file_reads: dict[tuple[str, str, int, int, str], tuple[int, str]] = {}
        self.micro_py_streak: int = 0
        self.micro_hints: int = 0
        self.http400_streak: int = 0
        self.http400_hints: int = 0
        self.taught_bash_ctx: bool = False
        self.emission_gated_once: bool = False
        self.cli_enriched_cmds: set[str] = set()
        self.cli_ref_shown: set[str] = set()
        self._seen_jira_links: set[tuple[str, str, str]] = set()
        self._cli_loop_bin: str = ""
        self._cli_loop_streak: int = 0
        self._cli_loop_last_step: int = -1
        self._cli_loop_hints: dict[str, int] = {}
        self.declared_deliverables: list[str] = list(declared_deliverables or [])
        resolved_task_md = (task_markdown or "").strip()
        if not resolved_task_md:
            for cand in (
                self.run_dir / "TASK.md",
                self.run_dir / "task.md",
                self.run_dir / "mock_case" / "task.md",
            ):
                if cand.is_file():
                    try:
                        resolved_task_md = cand.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        resolved_task_md = ""
                    if resolved_task_md:
                        break
        self.task_markdown: str = resolved_task_md
        self.untrashed_untitled_ids: dict[str, str] = {}
        self.external_mutation_count: int = 0
        self.last_external_mutation_step: int = 0
        self.last_external_readback_step: int = 0
        self.mutated_services: set[str] = set()
        self.verified_services: set[str] = set()
        self._non_idempotent_cmd_counts: dict[str, int] = {}
        self._non_idempotent_receipt_counts: dict[str, int] = {}
        self.duplicate_external_mutations: dict[str, str] = {}

        self.bin_dir = self.meta_dir / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.sandbox_tmp = self.meta_dir / "sandbox_tmp"
        self.sandbox_tmp.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "tmp").mkdir(parents=True, exist_ok=True)
        self.ctx_ws = self.meta_dir / "ctxws"
        self.ctx_extract_dir = self.meta_dir / "extracts"
        self._ctx_cmd_prefix: list[str] = []
        bwrap_cand = bwrap_sandbox_executable() if use_bwrap is not False else None
        self._bwrap_bin: str | None = bwrap_cand if (use_bwrap is None or use_bwrap) else None
        self._use_bwrap: bool = self._bwrap_bin is not None

        raw_path_parts: list[str] = [str(self.bin_dir)]
        if self.is_sj:
            ctx_bin = resolve_ctx_bin(config.ctx_bin)
            if "python" in ctx_bin.name.lower():
                # `-P` (ctx.selfcmd): `python -m` would put the current directory -- the
                # run directory the model writes into -- first on sys.path, so a planted
                # `ctx.py` or `ctx/` would run as the harness's ctx.
                self._ctx_cmd_prefix = [str(ctx_bin), SAFE_PATH_FLAG, "-m", "ctx"]
                shim_exec = f'exec "{ctx_bin}" {SAFE_PATH_FLAG} -m ctx --workspace "{self.ctx_ws}" "$@"'
            else:
                self._ctx_cmd_prefix = [str(ctx_bin)]
                shim_exec = f'exec "{ctx_bin}" --workspace "{self.ctx_ws}" "$@"'
            if ctx_bin.parent.is_dir():
                raw_path_parts.append(str(ctx_bin.absolute().parent))

            self.ctx_ws.mkdir(parents=True, exist_ok=True)
            self.ctx_extract_dir.mkdir(parents=True, exist_ok=True)
            shim = self.bin_dir / "ctx"
            shim.write_text(f"#!/usr/bin/env bash\n{shim_exec}\n", encoding="utf-8")
            shim.chmod(0o755)
            (self.ctx_ws / "ctx.toml").write_text(
                'version = 1\n[workspace]\nallow_outside_root = true\n[guard]\nmode = "advisory"\n',
                encoding="utf-8",
            )
            config.ctx_state_home.mkdir(parents=True, exist_ok=True)

        for venv_bin in (Path(sys.executable).absolute().parent, Path(sys.prefix) / "bin"):
            if venv_bin.is_dir():
                raw_path_parts.append(str(venv_bin))
        if config.runtime_dir is not None and (config.runtime_dir / "bin").is_dir():
            raw_path_parts.append(str((config.runtime_dir / "bin").resolve()))
        for source_path in (self.mock_env.get("PATH", ""), os.environ.get("PATH", "/usr/bin:/bin")):
            for seg in source_path.split(os.pathsep):
                if seg:
                    raw_path_parts.append(seg)
        path_env = os.pathsep.join(dict.fromkeys(raw_path_parts))

        client_env = {
            k: v
            for k, v in self.mock_env.items()
            if k.startswith(("JIRA_", "TODOIST_TOKEN", "STRIPE_API_KEY"))
            and "STATE_DIR" not in k
            and "MOCK_HOME" not in k
            and "MOCK_DB" not in k
        }
        base_env = dict(os.environ)
        base_env.update(client_env)
        if "SJ_SLOT_TOKEN" in self.mock_env:
            base_env["SJ_SLOT_TOKEN"] = self.mock_env["SJ_SLOT_TOKEN"]
        no_proxy_items = [
            x.strip()
            for x in (
                self.mock_env.get("NO_PROXY")
                or base_env.get("NO_PROXY")
                or base_env.get("no_proxy")
                or ""
            ).split(",")
            if x.strip()
        ]
        for host_tok in ("127.0.0.1", "localhost", "0.0.0.0"):
            if host_tok not in no_proxy_items:
                no_proxy_items.append(host_tok)
        no_proxy_val = ",".join(no_proxy_items)
        base_env["NO_PROXY"] = no_proxy_val
        base_env["no_proxy"] = no_proxy_val
        base_env["PATH"] = path_env
        base_env["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "0"
        base_env["GIT_CEILING_DIRECTORIES"] = (
            f"{self.run_dir.parent}{os.pathsep}{self.run_dir.parent.parent}"
        )
        if self.is_sj:
            base_env["CTX_STATE_HOME"] = str(config.ctx_state_home)
        if not self._use_bwrap:
            # No mount namespace: point temp files at the run's private /tmp (see _rewrite_tmp).
            base_env["TMPDIR"] = str(self.sandbox_tmp)
        self.env = base_env
        support_dirs: list[Path] = [Path(sys.prefix).resolve(), _REPO_SRC.resolve()]
        for seg in dict.fromkeys(raw_path_parts):
            pseg = Path(seg)
            if pseg.is_dir():
                support_dirs.append(pseg.resolve())
        self._support_ro_dirs: tuple[Path, ...] = tuple(dict.fromkeys(support_dirs))
        watch: list[Path] = [
            config.dataset_root,
            config.workdir,
            _REPO_ROOT / "evals",
            _REPO_ROOT / "tests",
            config.dataset_root / "real_replica_bench" / "mock_services",
            self.meta_dir / "mock_services",
            self.meta_dir / "mock_state",
            self.ctx_extract_dir,
            *self._support_ro_dirs,
        ]
        if config.runtime_dir is not None:
            watch.append(config.runtime_dir)
        self._bwrap_watch_dirs: tuple[Path, ...] = tuple(watch)
        self._bwrap_args_cache: dict[tuple[str, tuple[bool, ...]], list[str]] = {}
        # Which path served each ctx call the model made (see _route_ctx_command).
        self.ctx_host_fastpath_calls: int = 0
        self.ctx_sandboxed_calls: int = 0
        self._service_baseline: dict[str, Any] = self._snapshot_service_baseline() if self.is_sj else {}

    @property
    def res(self) -> RunMetrics:
        return self.metrics

    def has_user_deliverables(self) -> bool:
        return has_user_deliverables(self.run_dir / "outputs")

    def update_window_telemetry(self, model_id: str, step: int, usage: dict[str, Any]) -> None:
        if not self.is_sj:
            return
        try:
            pdir = session_reads_path(self.ctx_ws, "proxy")
            pdir.mkdir(parents=True, exist_ok=True)
            prompt_tok = int(usage.get("promptTokenCount", 0) or 0)
            cached_tok = int(usage.get("cachedContentTokenCount", 0) or 0)
            thought_tok = int(usage.get("thoughtsTokenCount", 0) or 0)
            out_tok = int(usage.get("candidatesTokenCount", 0) or 0)
            limit = 1_048_576
            doc = {
                "model": model_id,
                "requests": step,
                "last_input_tokens": prompt_tok,
                "turn_carriage_tokens": prompt_tok,
                "cached_tokens": cached_tok,
                "last_thought_tokens": thought_tok,
                "last_output_tokens": out_tok,
                "context_limit": limit,
                "window_pct": round(100.0 * prompt_tok / limit, 2) if prompt_tok else 0.0,
                "cum_cache_read": self.metrics.cached_tokens,
                "cum_output": self.metrics.output_tokens + self.metrics.thought_tokens,
                "workspace_id": self.ctx_ws.name,
            }
            tmp = pdir / f"window.json.{os.getpid()}.{threading.get_ident()}.tmp"
            tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
            os.replace(tmp, pdir / "window.json")
        except Exception:
            pass

    def norm(self, raw: str) -> Path:
        p = (raw or "").strip()
        if p.startswith("/task/"):
            p = p[len("/task/") :]
        elif p == "/task":
            p = "."
        elif p == "/tmp":
            return self.sandbox_tmp
        elif p.startswith("/tmp/"):
            rel_tmp = p[len("/tmp/") :]
            cand_stmp = (self.sandbox_tmp / rel_tmp).resolve()
            cand_rtmp = (self.run_dir / "tmp" / rel_tmp).resolve()
            if not cand_stmp.exists() and cand_rtmp.exists():
                return cand_rtmp
            return cand_stmp
        elif p.startswith("/root/"):
            p = "tmp/" + Path(p).name
        elif p.startswith(("/workspace/", "/outputs/")) and not Path(p).exists():
            p = p.lstrip("/")
        elif p.startswith("/"):
            return Path(p).resolve()
        return (self.run_dir / p).resolve()

    def _is_blocked_host_path(self, resolved: Path) -> bool:
        rp = resolved.resolve()
        if rp == self.run_dir or rp.is_relative_to(self.run_dir):
            return False
        if rp == self.sandbox_tmp or rp.is_relative_to(self.sandbox_tmp):
            return False
        for secret_root in (
            self.config.dataset_root,
            self.config.workdir,
            _REPO_ROOT / "evals",
            _REPO_ROOT / "tests",
        ):
            try:
                sr = secret_root.resolve()
                if rp == sr or rp.is_relative_to(sr):
                    return True
            except OSError:
                continue
        return False

    def rewrite_ports(self, text: str) -> str:
        if self.compiled_ports:
            return rewrite_ports_surgical(text, self.compiled_ports, is_service_script=False)
        return text

    def _rewrite_tmp(self, text: str) -> str:
        """Fallback (no bwrap) only: give bash the same private ``/tmp`` the file tools use.

        Rule: a ``/tmp`` path (``/tmp`` as a whole leading path component) means the
        run's private directory, unless its first component below ``/tmp`` exists in the
        host's ``/tmp`` and not in the private directory -- then it is a host path (the
        ctx executable, the run directory or workdir when they live under ``/tmp``) and
        is left alone. ``TMPDIR`` points the child at the private directory too."""
        if "/tmp" not in text:
            return text
        private = str(self.sandbox_tmp)

        def _sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name is None:
                return private
            if name and os.path.lexists(f"/tmp/{name}") and not os.path.lexists(f"{private}/{name}"):
                return m.group(0)
            return f"{private}/{name}"

        return _TMP_PATH_RE.sub(_sub, text)

    def rewrite(self, cmd: str) -> str:
        c = _TASK_PATH_RE.sub(lambda m: f"{m.group(1)}{self.run_dir}", self._rewrite_tmp(cmd))
        return self.rewrite_ports(c)

    def rewrite_guest(self, text: str) -> str:
        return self.rewrite_ports(text) if self._use_bwrap else self.rewrite(text)

    def _guest_workdir(self, wd: Path) -> str:
        wd_res = wd.resolve()
        if wd_res == self.run_dir:
            return "/task"
        if wd_res.is_relative_to(self.run_dir):
            return f"/task/{wd_res.relative_to(self.run_dir).as_posix()}"
        if wd_res == self.sandbox_tmp:
            return "/tmp"
        if wd_res.is_relative_to(self.sandbox_tmp):
            return f"/tmp/{wd_res.relative_to(self.sandbox_tmp).as_posix()}"
        return "/task"

    def _bwrap_mount_args(self, guest_wd: str) -> list[str]:
        """Mount arguments for one sandboxed call, computed once per working directory.

        Path resolution and the containment checks below cost milliseconds and never
        change for a host; only whether a few directories exist can (mock state, staged
        services, extracts), so their existence is part of the cache key."""
        key = (guest_wd, tuple(p.is_dir() for p in self._bwrap_watch_dirs))
        cached = self._bwrap_args_cache.get(key)
        if cached is None:
            cached = self._compute_bwrap_mount_args(guest_wd)
            self._bwrap_args_cache[key] = cached
        return list(cached)

    def _compute_bwrap_mount_args(self, guest_wd: str) -> list[str]:
        args: list[str] = [
            "--die-with-parent",
            "--unshare-pid",
            *_root_bwrap_mounts(),
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            str(self.sandbox_tmp),
            "/tmp",
        ]
        tmp_root = Path("/tmp").resolve()
        mask_candidates: list[Path] = []
        for mdir in (
            self.config.dataset_root,
            self.config.workdir,
            _REPO_ROOT / "evals",
            _REPO_ROOT / "tests",
        ):
            try:
                mr = mdir.resolve()
                if mr.is_dir() and mr != Path("/"):
                    mask_candidates.append(mr)
            except OSError:
                continue
        mask_dirs: list[Path] = []
        for mr in sorted(mask_candidates, key=lambda p: (len(p.parts), str(p))):
            if mr not in mask_dirs:
                mask_dirs.append(mr)
                args.extend(["--tmpfs", str(mr)])

        def _under_any(p: Path, roots: list[Path]) -> bool:
            return any(p == r or p.is_relative_to(r) for r in roots)

        masked_or_tmp = [tmp_root, *mask_dirs]
        ds_domain = (self.config.dataset_root / "datasets_domain_v1").resolve()
        wd_resolved = self.config.workdir.resolve()

        ro_rebind: list[Path] = []
        if self.config.runtime_dir is not None and self.config.runtime_dir.is_dir():
            rt = self.config.runtime_dir.resolve()
            if (
                _under_any(rt, masked_or_tmp)
                and not rt.is_relative_to(wd_resolved)
                and not any(m == rt or m.is_relative_to(rt) for m in mask_dirs)
            ):
                ro_rebind.append(rt)
        cab_mocks = (self.config.dataset_root / "real_replica_bench" / "mock_services").resolve()
        if cab_mocks.is_dir():
            ro_rebind.append(cab_mocks)
        for pdir in self._support_ro_dirs:
            if (
                pdir.is_dir()
                and _under_any(pdir, masked_or_tmp)
                and not pdir.is_relative_to(wd_resolved)
                and not pdir.is_relative_to(ds_domain)
                and not any(m == pdir or m.is_relative_to(pdir) for m in mask_dirs)
            ):
                ro_rebind.append(pdir)
        ro_rebind.append(self.bin_dir)
        ms_dir = self.meta_dir / "mock_services"
        if ms_dir.is_dir():
            ro_rebind.append(ms_dir)
        if self.is_sj and self.ctx_extract_dir.is_dir():
            ro_rebind.append(self.ctx_extract_dir)

        seen_ro: set[Path] = set()
        for rp in sorted(ro_rebind, key=lambda p: (len(p.parts), str(p))):
            if rp not in seen_ro and not any(rp.is_relative_to(prev) for prev in seen_ro):
                seen_ro.add(rp)
                args.extend(["--ro-bind", str(rp), str(rp)])

        rw_binds: list[tuple[Path, str]] = [
            (self.run_dir, "/task"),
            (self.run_dir, str(self.run_dir)),
        ]
        mstate_dir = self.meta_dir / "mock_state"
        if mstate_dir.is_dir():
            rw_binds.append((mstate_dir, str(mstate_dir)))
        if self.is_sj:
            rw_binds.append((self.ctx_ws, str(self.ctx_ws)))
            shome = self.config.ctx_state_home.resolve()
            shome.mkdir(parents=True, exist_ok=True)
            rw_binds.append((shome, str(shome)))
        seen_dsts: set[str] = set()
        for src_p, dst_s in sorted(rw_binds, key=lambda item: (len(Path(item[1]).parts), item[1])):
            if dst_s not in seen_dsts:
                seen_dsts.add(dst_s)
                args.extend(["--bind", str(src_p), dst_s])

        args.extend(
            [
                "--chdir",
                guest_wd,
                "--setenv",
                "PWD",
                guest_wd,
                "--setenv",
                "HOME",
                "/task/tmp",
            ]
        )
        return args

    def _run_in_sandbox(
        self,
        wd: Path,
        inner_argv: list[str],
        *,
        timeout: float = 120.0,
        merge_stderr: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        stderr_dest = subprocess.STDOUT if merge_stderr else subprocess.PIPE
        if not self._use_bwrap or self._bwrap_bin is None:
            return subprocess.run(
                inner_argv,
                cwd=str(wd),
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=stderr_dest,
                text=True,
                errors="replace",
                timeout=timeout,
            )
        mount_args = self._bwrap_mount_args(self._guest_workdir(wd))
        payload = b"\0".join(a.encode("utf-8") for a in mount_args) + b"\0"
        r_fd, w_fd = os.pipe()
        try:
            os.write(w_fd, payload)
        finally:
            os.close(w_fd)
        try:
            return subprocess.run(
                [self._bwrap_bin, "--args", str(r_fd), "--", *inner_argv],
                pass_fds=(r_fd,),
                cwd=str(wd),
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=stderr_dest,
                text=True,
                errors="replace",
                timeout=timeout,
            )
        finally:
            os.close(r_fd)

    def _rewrite_browser_url(self, url: str) -> str:
        u = url.strip()
        if u.startswith("file:///task/"):
            u = f"file://{self.run_dir}/{u[len('file:///task/'):]}"
        elif u == "file:///task":
            u = f"file://{self.run_dir}"
        return u

    def ctx(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._ctx_cmd_prefix, "--workspace", str(self.ctx_ws), *args],
            cwd=str(self.run_dir),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _format_digest_delivery(
        self,
        digest_text: str,
        raw_bytes_est: int = 0,
        stderr_tail: str = "",
    ) -> str:
        self.metrics.digests_delivered += 1
        text = digest_text
        if stderr_tail:
            text += "\nstderr (tail): " + stderr_tail[-600:]
        est = raw_bytes_est if raw_bytes_est > 0 else _estimate_digest_raw_bytes(digest_text)
        if est > 0:
            self.metrics.bytes_kept_out += max(0, est - len(text.encode("utf-8")))
        m_ref = re.search(r"\b(run:[0-9a-f]{6,64})\b", digest_text)
        ref_ex = m_ref.group(1) if m_ref else "run:<id>"
        self.taught_bash_ctx = True
        text += (
            f"\n[ctx-retrieve] Full output stored at {ref_ex}; call "
            f"ctx_retrieve(handle={ref_ex!r}, mode='get', lines='1:120') "
            "(or json_pointer='/0', mode='search', pattern='...') to inspect omitted records."
        )
        return text

    def capture_large_text(self, label: str, content: str) -> str:
        if not self.is_sj:
            self.metrics.inline_delivered += 1
            return content[: self.config.baseline_raw_cap]
        raw_bytes = len(content.encode("utf-8"))
        if raw_bytes <= self.config.entry_gate_bytes:
            self.metrics.inline_delivered += 1
            return content
        tmp_f = self.ctx_extract_dir / f"{label}_s{self.step}.txt"
        tmp_f.write_text(content, encoding="utf-8")
        return self.ctx_run(f"cat {shlex.quote(str(tmp_f))}", raw_bytes_hint=raw_bytes)

    def _raw_shell_run(self, shell_cmd: str, cwd_override: Path | None = None) -> str:
        if _SHELL_HISTORY_RE.search(shell_cmd):
            return "ERROR: shell history outside /task is unavailable"
        wd_path = cwd_override or self.run_dir
        try:
            cp = self._run_in_sandbox(wd_path, ["bash", "-c", shell_cmd], timeout=120.0)
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out after 120 seconds."
        self.metrics.inline_delivered += 1
        out = cp.stdout or ""
        err = cp.stderr or ""
        text = out + (("\n" + err) if err.strip() else "")
        if cp.returncode != 0:
            text = f"[exit {cp.returncode}]\n" + text
        return (text.strip() or "(command executed with no output)")[: self.config.baseline_raw_cap]

    def ctx_run(
        self,
        shell_cmd: str,
        raw_bytes_hint: int = 0,
        cwd_override: Path | None = None,
    ) -> str:
        if _SHELL_HISTORY_RE.search(shell_cmd):
            return "[ctx-guard: host shell history outside /task is unavailable]"
        wd_path = cwd_override or self.run_dir
        cwd_arg = self._guest_workdir(wd_path) if self._use_bwrap else str(wd_path)
        ctx_argv = [
            *self._ctx_cmd_prefix,
            "--workspace",
            str(self.ctx_ws),
            "run",
            "--json",
            "--cwd",
            cwd_arg,
            "--shell",
            shell_cmd,
        ]
        try:
            cp = self._run_in_sandbox(wd_path, ctx_argv, timeout=120.0)
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out after 120 seconds."
        raw = cp.stdout.strip()
        try:
            d = json.loads(raw[raw.index("{") :]) if "{" in raw else {}
        except Exception:
            d = {}
        if not d:
            return (cp.stdout + cp.stderr).strip()[:12000] or "(command executed with no output)"
        code = d.get("command_exit_code")
        if d.get("delivery") == "digest":
            return self._format_digest_delivery(
                d.get("digest", ""),
                raw_bytes_est=raw_bytes_hint,
                stderr_tail=(d.get("stderr") or "").strip(),
            )
        self.metrics.inline_delivered += 1
        out = d.get("stdout") or ""
        err = d.get("stderr") or ""
        text = out + (("\n" + err) if err.strip() else "")
        if code not in (0, None):
            text = f"[exit {code}]\n" + text
        return text.strip() or "(command executed with no output)"

    def _ctx_in_sandbox(self, inner_argv: list[str], wd: Path) -> subprocess.CompletedProcess[str]:
        """Run a ctx invocation (or ``bash -c`` of one) inside the trial sandbox."""
        self.ctx_sandboxed_calls += 1
        return self._run_in_sandbox(wd, inner_argv, timeout=120.0)

    def _ctx_store_or_sandbox(self, args: list[str], wd: Path) -> subprocess.CompletedProcess[str]:
        """Run ctx with ``args`` (``[verb, ...]``) on the host fast path ONLY when the
        product's own grammar (``ctx.hook.ctx_read_kind``) says it reads nothing but
        ctx's store by ``run:`` handle; every other form runs inside the sandbox."""
        if ctx_read_kind(args) == "store":
            self.ctx_host_fastpath_calls += 1
            return self.ctx(args)
        guest_args = [self.rewrite_guest(a) for a in args]
        return self._ctx_in_sandbox(
            [*self._ctx_cmd_prefix, "--workspace", str(self.ctx_ws), *guest_args], wd
        )

    def _route_ctx_command(self, command: str, wd: Path | None = None) -> str:
        """A bash command that starts with ``ctx`` on the sj arm.

        Host fast path: exactly one plain ctx call (``ctx.hook.ctx_command_words``:
        no comment, substitution, redirection, assignment prefix, double quote or
        second command) whose arguments read only ctx's store by ``run:`` handle
        (``ctx get``/``search``/``stats``/``view`` of a run handle, ``diff`` of two,
        ``intake --chunks``). Any other plain ctx call runs inside the sandbox with
        the same argv; anything the grammar cannot read as one plain call runs in
        the sandbox under ``bash -c``, so the shell, not the harness, interprets it."""
        wd_path = wd or self.run_dir
        argv = ctx_command_words(command.strip())
        if argv is not None:
            args = argv[1:]
            verb = args[0] if args else ""
        else:
            args = []
            try:
                toks = shlex.split(command)
            except ValueError:
                toks = []
            verb = toks[1] if len(toks) > 1 else ""
        if verb and not verb.startswith("-"):
            self.metrics.ctx_retrievals[verb] = self.metrics.ctx_retrievals.get(verb, 0) + 1
        try:
            if argv is not None:
                if not args:
                    return "ERROR: empty ctx subcommand"
                cp = self._ctx_store_or_sandbox(args, wd_path)
            else:
                if _SHELL_HISTORY_RE.search(command):
                    return "[ctx-guard: host shell history outside /task is unavailable]"
                cp = self._ctx_in_sandbox(["bash", "-c", self.rewrite_guest(command)], wd_path)
        except subprocess.TimeoutExpired:
            return "ERROR: ctx command timed out."
        text = ((cp.stdout or "") + (("\n" + cp.stderr) if (cp.stderr or "").strip() else "")).strip()
        return text[:20000] or f"(ctx {verb}: no output, exit {cp.returncode})"

    def ctx_direct(self, args_str: str) -> str:
        """``ctx <args_str>`` as the model typed it; see :meth:`_route_ctx_command`."""
        return self._route_ctx_command(f"ctx {args_str}".strip())

    def _maybe_enrich_cli_help(self, cmd_rw: str, out: str) -> str:
        if not self.is_sj:
            return out
        s = cmd_rw.strip()
        if (
            s in self.cli_enriched_cmds
            or any(op in s for op in ("&&", "||", ";", "\n", "|", ">"))
            or out.startswith("[exit ")
            or len(out) > 8000
        ):
            return out
        m_help = re.match(r"^(.+?)\s+(?:--help|-h)$", s)
        if not m_help:
            return out
        base_cmd = m_help.group(1).strip()
        self.cli_enriched_cmds.add(s)

        subcmds: list[str] = []
        for grp in re.findall(r"\{([a-zA-Z0-9_,-]{3,240})\}", out):
            for tok in grp.split(","):
                tok = tok.strip()
                if re.match(r"^[a-zA-Z][a-zA-Z0-9_:-]{1,32}$", tok) and tok not in subcmds:
                    subcmds.append(tok)
        in_cmds = False
        for ln in out.splitlines():
            if re.match(r"^\s*(?:available\s+)?(?:sub)?commands\s*:", ln, re.I):
                in_cmds = True
                continue
            if in_cmds:
                if ln.strip() == "" or re.match(r"^[A-Za-z].*:\s*$", ln):
                    if subcmds:
                        in_cmds = False
                    continue
                m_sc = re.match(r"^\s{2,8}([a-z][a-z0-9_:-]{1,30})(?:\s{2,}|\t|$)", ln)
                if m_sc and m_sc.group(1) not in subcmds:
                    subcmds.append(m_sc.group(1))

        if not (1 <= len(subcmds) <= 10):
            return out

        targets = subcmds[:8]

        def _probe(sc: str) -> str | None:
            probe_sh = f"{base_cmd} {shlex.quote(sc)} --help"
            try:
                cp = self._run_in_sandbox(
                    self.run_dir,
                    ["bash", "-c", probe_sh],
                    timeout=2.5,
                    merge_stderr=True,
                )
                if cp.returncode != 0 or not cp.stdout:
                    return None
                usage_ln = ""
                for hl in cp.stdout.splitlines():
                    if hl.lower().strip().startswith("usage:"):
                        usage_ln = hl.strip()
                        break
                flags = list(dict.fromkeys(re.findall(r"--[a-zA-Z0-9][a-zA-Z0-9_-]*", cp.stdout)))
                flags = [f for f in flags if f != "--help"]
                if usage_ln or flags:
                    fl_s = ", ".join(flags[:14])
                    entry = f"  {sc}: {usage_ln}" + (f" | flags: {fl_s}" if fl_s else "")
                    return entry[:260]
            except Exception:
                return None
            return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            probed = list(pool.map(_probe, targets))
        lines = [ln for ln in probed if ln]
        if not lines:
            return out
        self.metrics.cli_lsp_enrichments += 1
        return out + ("\n\n[cli-lsp subcommand signatures]\n" + "\n".join(lines))[:2400]

    def _workspace_fs_fingerprint(self) -> dict[str, tuple[int, int]]:
        """Capture fast ``(size, mtime_ns)`` snapshot of ``outputs/`` and ``workspace/``."""
        snap: dict[str, tuple[int, int]] = {}
        count = 0
        for sub in ("outputs", "workspace"):
            root = self.run_dir / sub
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if count >= 600:
                    return snap
                if not p.is_file():
                    continue
                if any(ig in p.parts for ig in ("mock_audit", "mock_state", ".git", "__pycache__")):
                    continue
                try:
                    st = p.stat()
                    snap[p.relative_to(self.run_dir).as_posix()] = (st.st_size, st.st_mtime_ns)
                    count += 1
                except OSError:
                    continue
        return snap

    def _track_untitled_creations(self, out_txt: str) -> None:
        if not out_txt:
            return
        if self.untrashed_untitled_ids and _RESOURCE_CLEANUP_RE.search(out_txt):
            for rid in list(self.untrashed_untitled_ids):
                if rid in out_txt:
                    self.untrashed_untitled_ids.pop(rid, None)
        for m in _UNTITLED_CREATE_RE.finditer(out_txt):
            rid = m.group(1)
            self.untrashed_untitled_ids[rid] = " ".join(m.group(0).split())[:90]

    def _name_exists_in_workspace(self, name_lower: str) -> bool:
        """Return True if a file or directory matching ``name_lower`` exists inside ``workspace/`` or ``outputs/``."""
        for sub in ("workspace", "outputs"):
            root = self.run_dir / sub
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if p.name.lower() == name_lower:
                    return True
        return False

    def _stripe_trigger_requested(self) -> bool:
        """Return True if ``stripe trigger`` is mentioned in task_markdown or workspace docs."""
        if "stripe trigger" in (self.task_markdown or "").lower():
            return True
        ws_dir = self.run_dir / "workspace"
        if ws_dir.is_dir():
            for md_file in ws_dir.rglob("*.md"):
                try:
                    if "stripe trigger" in md_file.read_text(encoding="utf-8", errors="ignore").lower():
                        return True
                except OSError:
                    continue
        return False

    def _check_bash_entry_gate(self, cmd_rw: str) -> str | None:
        """Block synthetic scratch probes, unrequested ``stripe trigger``, and duplicate Jira links."""
        if not self.is_sj or not cmd_rw:
            return None
        invocations = _extract_cli_invocations(cmd_rw)
        for cli_bin, rest, _ in invocations:
            if not rest or "--help" in rest or re.search(r"(?:^|\s)-h(?:\s|$)", rest) or "--dry-run" in rest:
                continue
            rest_l = rest.lower()
            if (
                cli_bin == "stripe"
                and _STRIPE_TRIGGER_REST_RE.match(rest)
                and not self._stripe_trigger_requested()
            ):
                return (
                    "[ctx-entry-gate] Blocked: 'stripe trigger' creates synthetic 'myproduct' fixture "
                    "pollution in stripe_mock.sqlite that fails clean-account graders. Inspect or mutate "
                    "the specific Stripe resources directly via stripe CLI or curl instead."
                )
            is_persistent_probe_target = (
                (cli_bin == "ntn" and rest_l.startswith("files "))
                or cli_bin in ("box", "dws")
            )
            if is_persistent_probe_target and _SCRATCH_PROBE_CLI_CREATES_RE.search(rest):
                try:
                    toks = shlex.split(rest)
                except ValueError:
                    toks = rest.split()
                for cand_tok in _resolve_inline_probe_tokens(cmd_rw, toks):
                    if _is_synthetic_probe_name(cand_tok):
                        base_l = Path(cand_tok.strip("\"'`")).name.lower()
                        if not self._name_exists_in_workspace(base_l):
                            return (
                                f"[ctx-entry-gate] Blocked synthetic scratch-probe creation `{cli_bin} {rest[:90]}` "
                                f"(`{cand_tok}` does not exist in /task/workspace). Do not create throwaway or 'Untitled' "
                                "probe items on persistent external services; query existing state or create final named deliverables."
                            )
            if cli_bin == "jira":
                jlink = _parse_jira_link_call(rest)
                if jlink is not None and jlink in self._seen_jira_links:
                    src, tgt, ltype = jlink
                    return (
                        f"[ctx-entry-gate] Blocked duplicate `jira issue link` ({src}, {tgt}, {ltype!r}): "
                        "that exact issue link was already created earlier in this run."
                    )
        return None

    def _maybe_append_cli_subcommand_ref(self, cmd_rw: str, out: str) -> str:
        """Append a 1-time compact ``[cli-subcommand-ref]`` card per domain CLI on first ``bash`` use."""
        if not self.is_sj or not cmd_rw or out.startswith("[ctx-entry-gate]"):
            return out
        invoked: list[str] = []
        for cli_bin, _, _ in _extract_cli_invocations(cmd_rw):
            if cli_bin in _CLI_SUBCOMMAND_REFS and cli_bin not in self.cli_ref_shown and cli_bin not in invoked:
                invoked.append(cli_bin)
        if not invoked:
            return out
        cards: list[str] = []
        for cli_bin in invoked:
            self.cli_ref_shown.add(cli_bin)
            cards.append(_CLI_SUBCOMMAND_REFS[cli_bin])
        return out + "\n\n[cli-subcommand-ref]\n" + "\n".join(cards)

    @staticmethod
    def _append_cli_subprocess_error(cmd_rw: str, out: str) -> str:
        """Tag embedded CLI subprocess errors (``command not found``, ``Usage:``, ``HTTP 4xx/5xx``)."""
        if not out or "[cli-subprocess-error]" in out:
            return out
        explicit_help = bool(re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", cmd_rw))
        clean_help = (
            explicit_help
            and not out.startswith("[exit ")
            and not re.search(
                r"(?:Unknown command|unknown (?:flag|command|option)|command not found|"
                r"\bError:|invalid argument|missing .*argument|\bHTTP(?:/[\d.]+)?\s+[45]\d\d\b)",
                out,
                re.I,
            )
        )
        matched_lines: list[str] = []
        for ln in out.splitlines():
            s_ln = ln.strip()
            if not s_ln:
                continue
            if _EMBEDDED_CLI_ERR_RE.search(ln):
                if clean_help and re.match(r"^\s*Usage:\s", ln, re.I):
                    continue
                matched_lines.append(s_ln[:140])
                if len(matched_lines) >= 2:
                    break
        if not matched_lines:
            return out
        snippet = " | ".join(matched_lines)[:240]
        return out + f"\n[cli-subprocess-error] Embedded CLI failure detected: {snippet}"

    def _apply_structured_shape(self, text: str, raw_for_detection: str | None = None) -> str:
        """Prepend ``ctx.toolresult.prepend_structured_data_shape`` in ``sj`` mode with zero character loss."""
        if not self.is_sj:
            return text
        probe = raw_for_detection if raw_for_detection is not None else text
        if len(probe) < 1500:
            return text
        try:
            shaped = str(prepend_structured_data_shape(probe))
        except Exception:
            return text
        if not shaped or shaped == probe:
            return text
        if raw_for_detection is None:
            return shaped
        if shaped.endswith(probe):
            prefix = shaped[: len(shaped) - len(probe)]
            return prefix + text
        first_ln = shaped.splitlines()[0].strip()
        if first_ln.startswith("[data-shape:"):
            return first_ln + "\n" + text
        return text

    def unverified_mutated_services(self) -> set[str]:
        """Return mutated external services that have not had a subsequent readback verification."""
        return set(self.mutated_services - self.verified_services)

    def _track_external_service_activity(self, cmd: str, out_txt: str) -> None:
        """Track external service mutations, non-idempotent duplicates, and per-service readbacks."""
        if not cmd or out_txt.startswith(("[exit ", "ERROR:", "[ctx-entry-gate]")):
            return
        receipts = extract_mutation_receipts(out_txt)
        invocations = _extract_cli_invocations(cmd)
        has_service_target = (
            bool(_EXT_SERVICE_TARGET_RE.search(cmd))
            or bool(invocations)
            or bool(receipts)
        )
        if not has_service_target:
            return

        has_ext_mut = bool(receipts)
        has_ext_read = False
        last_mut_pos: dict[str, int] = {}
        last_read_pos: dict[str, int] = {}
        port_rx = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):(\d+)")

        if _MCP_MUTATING_CALL_RE.search(cmd):
            has_ext_mut = True
            for m in _MCP_MUTATING_CALL_RE.finditer(cmd):
                last_mut_pos["mcp"] = max(last_mut_pos.get("mcp", -1), m.start())
        if _MCP_READONLY_CALL_RE.search(cmd):
            has_ext_read = True
            for m in _MCP_READONLY_CALL_RE.finditer(cmd):
                last_read_pos["mcp"] = max(last_read_pos.get("mcp", -1), m.start())

        for m in _HTTP_MUTATION_RE.finditer(cmd):
            window = cmd[max(0, m.start() - 40) : min(len(cmd), m.end() + 140)]
            pm = port_rx.search(window)
            svc = f"http:{pm.group(1)}" if pm else "http"
            has_ext_mut = True
            last_mut_pos[svc] = max(last_mut_pos.get(svc, -1), m.start())
        for m in _HTTP_READBACK_RE.finditer(cmd):
            window = cmd[max(0, m.start() - 40) : min(len(cmd), m.end() + 140)]
            pm = port_rx.search(window)
            svc = f"http:{pm.group(1)}" if pm else "http"
            has_ext_read = True
            last_read_pos[svc] = max(last_read_pos.get(svc, -1), m.start())

        cleaned_up = bool(_RESOURCE_CLEANUP_RE.search(out_txt))
        for cli_bin, rest, pos in invocations:
            if not rest or "--help" in rest or re.search(r"(?:^|\s)-h(?:\s|$)", rest):
                continue
            if _CLEANUP_CLI_VERB_RE.search(rest):
                has_ext_mut = True
                cleaned_up = True
                last_mut_pos[cli_bin] = max(last_mut_pos.get(cli_bin, -1), pos)
                for dkey in list(self.duplicate_external_mutations):
                    if any(tok in rest or tok in out_txt for tok in dkey.split() if len(tok) >= 4):
                        self.duplicate_external_mutations.pop(dkey, None)
                if cli_bin == "jira" and "unlink" in rest.lower():
                    for jkey in list(self._seen_jira_links):
                        if jkey[0] in rest.upper() and jkey[1] in rest.upper():
                            self._seen_jira_links.discard(jkey)
            elif _NON_IDEMPOTENT_CLI_VERB_RE.search(rest) and "--dry-run" not in rest:
                has_ext_mut = True
                last_mut_pos[cli_bin] = max(last_mut_pos.get(cli_bin, -1), pos)
                if cli_bin == "jira":
                    jlink = _parse_jira_link_call(rest)
                    if jlink is not None:
                        self._seen_jira_links.add(jlink)
                norm_sig = f"{cli_bin} {rest}"[:140]
                cnt = self._non_idempotent_cmd_counts.get(norm_sig, 0) + 1
                self._non_idempotent_cmd_counts[norm_sig] = cnt
                if cnt >= 2:
                    self.duplicate_external_mutations[norm_sig] = (
                        f"Duplicate non-idempotent service command ran {cnt}x (`{norm_sig}`) "
                        "— verify no extra duplicate resource, link, or uploaded file remains."
                    )
            elif _MUTATING_CLI_VERB_RE.search(rest) and "--dry-run" not in rest:
                has_ext_mut = True
                last_mut_pos[cli_bin] = max(last_mut_pos.get(cli_bin, -1), pos)
            elif _READBACK_CLI_VERB_RE.search(rest):
                has_ext_read = True
                last_read_pos[cli_bin] = max(last_read_pos.get(cli_bin, -1), pos)

        if receipts and not last_mut_pos:
            last_mut_pos["external"] = len(cmd)

        for svc, m_pos in last_mut_pos.items():
            self.mutated_services.add(svc)
            if last_read_pos.get(svc, -1) > m_pos:
                self.verified_services.add(svc)
            else:
                self.verified_services.discard(svc)
        for svc in last_read_pos:
            if svc not in last_mut_pos and svc in self.mutated_services:
                self.verified_services.add(svc)

        if cleaned_up and self.duplicate_external_mutations:
            for dkey in list(self.duplicate_external_mutations):
                quoted = re.findall(r'"([^"]+)"', dkey)
                if quoted and any(q in cmd or q in out_txt for q in quoted):
                    self.duplicate_external_mutations.pop(dkey, None)

        for m_rcpt in _NAMED_CREATE_RECEIPT_RE.finditer(out_txt):
            verb, kind, title = m_rcpt.group(1), m_rcpt.group(2), m_rcpt.group(3).strip()
            if title.lower() == "untitled":
                continue
            rkey = f'{verb.lower()} {kind.lower()} "{title}"'
            rcnt = self._non_idempotent_receipt_counts.get(rkey, 0) + 1
            self._non_idempotent_receipt_counts[rkey] = rcnt
            if rcnt >= 2:
                self.duplicate_external_mutations[rkey] = (
                    f'Duplicate creation/upload receipt `{verb} {kind} "{title}"` appeared {rcnt}x '
                    "— verify total remote file/item count and remove any accidental duplicate."
                )

        if has_ext_mut:
            self.external_mutation_count += 1
            self.last_external_mutation_step = self.step
        elif has_ext_read:
            self.last_external_readback_step = self.step

    def external_state_advisories(self) -> list[str]:
        """Return one-time pre-exit advisories for scratch probes, duplicate creations, or unverified mutations."""
        adv: list[str] = []
        if self.untrashed_untitled_ids:
            items_s = ", ".join(
                f"{rid} ({desc})"
                for rid, desc in list(self.untrashed_untitled_ids.items())[:3]
            )
            adv.append(
                f"Untrashed 'Untitled' creation(s) detected in workspace state: {items_s} "
                "— delete/trash accidental probe creations or confirm they are required."
            )
        for dup_msg in list(self.duplicate_external_mutations.values())[:3]:
            adv.append(dup_msg)
        unverified = sorted(self.unverified_mutated_services())
        if unverified:
            adv.append(
                f"External service state was mutated without a subsequent read-only check for: "
                f"{', '.join(unverified)} — run one consolidated list/get readback for each "
                "to confirm final remote item/file/link counts and statuses before finalizing."
            )
        elif (
            self.external_mutation_count > 0
            and self.last_external_readback_step < self.last_external_mutation_step
        ):
            adv.append(
                f"External service state was mutated at step {self.last_external_mutation_step} "
                "without a subsequent read-only check — run one consolidated list/get readback "
                "to confirm final remote item/file/link counts and statuses before finalizing."
            )
        return adv

    @staticmethod
    def _open_ro_sqlite(db_path: Path) -> sqlite3.Connection | None:
        if not db_path.is_file():
            return None
        try:
            return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        except sqlite3.Error:
            return None

    @staticmethod
    def _sqlite_table_count(con: sqlite3.Connection, table: str, *, active_only: bool = False) -> int:
        try:
            cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
            if not cols:
                return 0
            if active_only and "is_deleted" in cols:
                row = con.execute(f"SELECT COUNT(*) FROM {table} WHERE is_deleted = 0").fetchone()
            else:
                row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def _find_service_state_dbs(self) -> dict[str, Path]:
        """Locate live SQLite mock DBs under ``mock_env``, ``meta_dir/mock_state``, or ``run_dir/mock_state``."""
        specs: tuple[tuple[str, tuple[str, ...], str, tuple[str, ...]], ...] = (
            ("jira", ("JIRA_MOCK_STATE_DIR", "JIRA_MOCK_HOME"), "jira_cli", ("jira_mock.db",)),
            ("ntn", ("NTN_MOCK_STATE_DIR", "NTN_MOCK_DATA_DIR"), "notion_cli", ("mock.db",)),
            ("box", ("BOX_MOCK_STATE_DIR", "BOX_MOCK_HOME"), "box_cli", (".box/box_mock.db", "box_mock.db")),
            ("dws", ("DWS_MOCK_STATE_DIR", "DWS_MOCK_HOME"), "dws_doc_cli", ("mock.db",)),
            (
                "gws",
                ("GWS_MOCK_STATE_DIR", "GWS_MOCK_HOME"),
                "google_workspace_cli",
                (".cache/gws/mock.db", "mock.db"),
            ),
            (
                "todoist",
                ("TODOIST_MOCK_STATE_DIR", "TODOIST_MOCK_HOME"),
                "todoist_cli",
                (".cache/todoist/mock.db", "mock.db"),
            ),
            (
                "stripe",
                ("STRIPE_MOCK_DB", "STRIPE_MOCK_STATE_DIR", "STRIPE_MOCK_HOME"),
                "stripe_cli",
                ("stripe_mock.sqlite",),
            ),
        )
        found: dict[str, Path] = {}
        state_roots = [self.meta_dir / "mock_state", self.run_dir / "mock_state"]
        for svc, env_keys, subdir, rel_dbs in specs:
            cand_dirs: list[Path] = []
            for ek in env_keys:
                ev = self.mock_env.get(ek) or ""
                if ev:
                    ep = Path(ev)
                    if ep.is_file():
                        found[svc] = ep
                        break
                    cand_dirs.append(ep)
            if svc in found:
                continue
            for sr in state_roots:
                cand_dirs.append(sr / subdir)
            for base in cand_dirs:
                if svc in found:
                    break
                for rel in rel_dbs:
                    p = base / rel
                    if p.is_file():
                        found[svc] = p
                        break
        return found

    def _load_json_service_states(self) -> dict[str, Any]:
        """Load optional JSON mock-state snapshots under ``mock_state``, ``runtime_mock_state``, or service dirs."""
        out: dict[str, Any] = {}
        svc_aliases = {
            "jira": ("jira", "jira_cli"),
            "ntn": ("notion", "notion_cli", "ntn"),
            "box": ("box", "box_cli"),
            "dws": ("dws", "dws_doc_cli"),
            "gws": ("gws", "google_workspace_cli"),
            "todoist": ("todoist", "todoist_cli"),
            "google_docs": ("google_docs_mock", "google_docs", "gdocs"),
        }
        skip_names = frozenset({"credentials.json", "package.json", "package-lock.json", "tsconfig.json"})
        cand_files: list[tuple[Path, str]] = []
        scan_roots = (
            self.meta_dir / "mock_state",
            self.run_dir / "mock_state",
            self.meta_dir / "mock_workdir" / "tmp" / "runtime_mock_state",
            self.meta_dir / "mock_workdir" / "tmp" / "mock_state",
            self.run_dir / "mock_workdir" / "tmp" / "runtime_mock_state",
            self.run_dir / "mock_workdir" / "tmp" / "mock_state",
        )
        for root in scan_roots:
            if root.is_dir():
                for p in sorted(
                    root.rglob("*.json"),
                    key=lambda item: (0 if item.name.lower() == "state.json" else 1, str(item)),
                ):
                    if p.name.lower() not in skip_names:
                        cand_files.append((p, str(p.relative_to(root)).lower()))
        if self.run_dir.is_dir():
            for p in sorted(self.run_dir.iterdir()):
                if p.is_file() and p.suffix.lower() == ".json" and p.name.lower() not in skip_names:
                    cand_files.append((p, p.name.lower()))
                elif p.is_dir() and p.name.lower() in {a for aliases in svc_aliases.values() for a in aliases}:
                    for sp in sorted(
                        p.rglob("*.json"),
                        key=lambda item: (0 if item.name.lower() == "state.json" else 1, str(item)),
                    ):
                        if sp.name.lower() not in skip_names:
                            cand_files.append((sp, str(sp.relative_to(self.run_dir)).lower()))

        for p, rel_l in cand_files:
            try:
                if p.stat().st_size > 2_000_000:
                    continue
            except OSError:
                continue
            for svc, keys in svc_aliases.items():
                if svc in out:
                    continue
                if any(k in rel_l for k in keys):
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            out[svc] = data
                    except (OSError, json.JSONDecodeError):
                        pass
        return out

    @staticmethod
    def _extract_gws_presentation_summary(pres_data: dict[str, Any]) -> tuple[int, list[str], bool]:
        """Return ``(slide_count, labeled_counts_sample, has_stale_template)`` for one GWS presentation."""
        slides = pres_data.get("slides") or []
        if not isinstance(slides, list):
            return 0, [], False
        texts: list[str] = []

        def _collect_runs(obj: Any) -> None:
            if isinstance(obj, dict):
                if isinstance(obj.get("content"), str):
                    texts.append(obj["content"])
                for k, v in obj.items():
                    if k != "content" and isinstance(v, (dict, list)):
                        _collect_runs(v)
            elif isinstance(obj, list):
                for item in obj:
                    _collect_runs(item)

        _collect_runs(slides)
        joined = "\n".join(texts)
        pairs = re.findall(r"\b([A-Za-z][A-Za-z0-9 _/-]{1,22})\s*[:=]\s*(\d+(?:\.\d+)?)\b", joined)
        labeled: list[str] = []
        seen_l: set[str] = set()
        for k, v in pairs:
            item = f"{' '.join(k.split())}={v}"
            if item.lower() not in seen_l:
                seen_l.add(item.lower())
                labeled.append(item)
                if len(labeled) >= 6:
                    break
        title = str(pres_data.get("title") or "")
        stale_template = bool(
            "AcmeCloud Launch Plan" in joined and title and "AcmeCloud Launch Plan" not in title
        )
        return len(slides), labeled, stale_template

    def _snapshot_service_baseline(self) -> dict[str, Any]:
        """Capture initial seeded counts/IDs across live service mock DBs before step 1."""
        snap: dict[str, Any] = {}
        dbs = self._find_service_state_dbs()

        if "jira" in dbs:
            con = self._open_ro_sqlite(dbs["jira"])
            if con is not None:
                try:
                    links = con.execute(
                        "SELECT UPPER(inward_key), UPPER(outward_key), LOWER(link_type) FROM issue_links"
                    ).fetchall()
                    link_counts: dict[tuple[str, str, str], int] = {}
                    for r in links:
                        key = (str(r[0]), str(r[1]), str(r[2]))
                        link_counts[key] = link_counts.get(key, 0) + 1
                    done_row = con.execute(
                        "SELECT COUNT(*) FROM issues i JOIN issue_statuses s ON i.status_id = s.id "
                        "WHERE LOWER(s.category) = 'done' OR LOWER(s.name) = 'done'"
                    ).fetchone()
                    snap["jira"] = {
                        "link_counts": link_counts,
                        "total_links": len(links),
                        "done": int(done_row[0]) if done_row else 0,
                    }
                except sqlite3.Error:
                    pass
                finally:
                    con.close()

        if "ntn" in dbs:
            con = self._open_ro_sqlite(dbs["ntn"])
            if con is not None:
                try:
                    page_ids = {
                        str(r[0]) for r in con.execute("SELECT id FROM entities WHERE type = 'pages'").fetchall()
                    }
                    file_ids = {
                        str(r[0]) for r in con.execute("SELECT id FROM entities WHERE type = 'files'").fetchall()
                    }
                    snap["ntn"] = {
                        "page_ids": page_ids,
                        "file_ids": file_ids,
                        "pages": len(page_ids),
                        "files": len(file_ids),
                    }
                except sqlite3.Error:
                    pass
                finally:
                    con.close()

        if "box" in dbs:
            con = self._open_ro_sqlite(dbs["box"])
            if con is not None:
                try:
                    snap["box"] = {
                        "folders": self._sqlite_table_count(con, "folders", active_only=True),
                        "files": self._sqlite_table_count(con, "files", active_only=True),
                        "collabs": self._sqlite_table_count(con, "collaborations", active_only=True),
                        "tasks": self._sqlite_table_count(con, "tasks", active_only=True),
                    }
                finally:
                    con.close()

        if "dws" in dbs:
            con = self._open_ro_sqlite(dbs["dws"])
            if con is not None:
                try:
                    file_row = con.execute(
                        "SELECT COUNT(*) FROM documents WHERE type = 'file'"
                    ).fetchone()
                    snap["dws"] = {
                        "docs": self._sqlite_table_count(con, "documents"),
                        "files": int(file_row[0]) if file_row else 0,
                        "blocks": self._sqlite_table_count(con, "blocks"),
                        "comments": self._sqlite_table_count(con, "comments"),
                        "exports": self._sqlite_table_count(con, "exports"),
                    }
                except sqlite3.Error:
                    pass
                finally:
                    con.close()

        if "gws" in dbs:
            con = self._open_ro_sqlite(dbs["gws"])
            if con is not None:
                try:
                    pres_hashes = {
                        str(r[0]): hashlib.sha256(str(r[1]).encode("utf-8")).hexdigest()
                        for r in con.execute("SELECT id, data FROM presentations").fetchall()
                    }
                    sheet_hashes = {
                        str(r[0]): hashlib.sha256(str(r[1]).encode("utf-8")).hexdigest()
                        for r in con.execute("SELECT id, data FROM spreadsheets").fetchall()
                    }
                    snap["gws"] = {"pres_hashes": pres_hashes, "sheet_hashes": sheet_hashes}
                except sqlite3.Error:
                    pass
                finally:
                    con.close()

        if "todoist" in dbs:
            con = self._open_ro_sqlite(dbs["todoist"])
            if con is not None:
                try:
                    cols = {str(r[1]) for r in con.execute("PRAGMA table_info(items)").fetchall()}
                    if cols:
                        total_i = self._sqlite_table_count(con, "items", active_only=True)
                        checked_col = "checked" if "checked" in cols else ("is_completed" if "is_completed" in cols else "")
                        done_i = 0
                        if checked_col:
                            r = con.execute(f"SELECT COUNT(*) FROM items WHERE {checked_col} != 0").fetchone()
                            done_i = int(r[0]) if r else 0
                        snap["todoist"] = {"items": total_i, "completed": done_i}
                except sqlite3.Error:
                    pass
                finally:
                    con.close()

        return snap

    @staticmethod
    def _extract_nested_text(
        obj: Any,
        *,
        skip_keys: frozenset[str] = frozenset(
            {
                "id",
                "nodeId",
                "blockId",
                "parentId",
                "createTime",
                "lastEditTime",
                "commentKey",
                "spreadsheetId",
                "sheetId",
                "timestamp",
                "created_at",
                "updated_at",
                "created",
            }
        ),
    ) -> list[str]:
        """Collect human-readable string leaf values from nested JSON payloads while skipping ID/timestamp keys."""
        parts: list[str] = []
        if isinstance(obj, str):
            if obj.strip():
                parts.append(obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if str(k) in skip_keys:
                    continue
                parts.extend(ToolHost._extract_nested_text(v, skip_keys=skip_keys))
        elif isinstance(obj, list):
            for item in obj:
                parts.extend(ToolHost._extract_nested_text(item, skip_keys=skip_keys))
        return parts

    def _workspace_csv_files(self) -> list[Path]:
        """Return non-internal workspace ``.csv`` files under ``run_dir/workspace`` or ``run_dir``."""
        ws = self.run_dir / "workspace"
        if ws.is_dir():
            return sorted(p for p in ws.rglob("*.csv") if p.is_file())
        if not self.run_dir.is_dir():
            return []
        skip_dirs = frozenset(
            {"outputs", "mock_case", "mock_services", "mock_state", "mock_workdir", "ctxws", "sandbox_tmp", "tmp", ".git"}
        )
        out: list[Path] = []
        for p in sorted(self.run_dir.rglob("*.csv")):
            if p.is_file() and not (skip_dirs & set(p.relative_to(self.run_dir).parts)):
                out.append(p)
        return out

    def _collect_fk_sources(self, dbs: dict[str, Path], json_states: dict[str, Any]) -> list[tuple[str, str]]:
        """Collect ``(source_label, text)`` across outputs, GWS, DWS, Jira, and Todoist for FK checks."""
        sources: list[tuple[str, str]] = []
        text_exts = frozenset({".txt", ".md", ".json", ".csv", ".tsv", ".yaml", ".yml", ".html"})

        outputs_dir = self.run_dir / "outputs"
        if outputs_dir.is_dir():
            for p in sorted(outputs_dir.rglob("*")):
                if (
                    not p.is_file()
                    or p.suffix.lower() not in text_exts
                    or p.name.lower().endswith("_final_state.json")
                ):
                    continue
                try:
                    if p.stat().st_size <= 2_000_000:
                        sources.append((f"outputs/{p.relative_to(outputs_dir).as_posix()}", p.read_text("utf-8", "ignore")))
                except OSError:
                    pass

        if "gws" in dbs:
            con = self._open_ro_sqlite(dbs["gws"])
            if con is not None:
                try:
                    for sid, raw in con.execute("SELECT id, data FROM spreadsheets").fetchall():
                        try:
                            pd = json.loads(str(raw))
                        except json.JSONDecodeError:
                            sources.append((f"gws:{sid}", str(raw)))
                        else:
                            txt = "\n".join(self._extract_nested_text(pd))
                            if txt:
                                sources.append((f"gws:{sid}", txt))
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        elif "gws" in json_states:
            txt = "\n".join(self._extract_nested_text(json_states["gws"].get("spreadsheets") or json_states["gws"]))
            if txt:
                sources.append(("gws:state.json", txt))

        if "dws" in dbs:
            con = self._open_ro_sqlite(dbs["dws"])
            if con is not None:
                try:
                    for nid, dname, content in con.execute(
                        "SELECT nodeId, COALESCE(name,''), COALESCE(content,'') FROM documents"
                    ).fetchall():
                        blob = f"{dname}\n{content}".strip()
                        if blob:
                            sources.append((f"dws:{dname or nid}", blob))
                    for nid, cj in con.execute("SELECT nodeId, COALESCE(contentJson,'') FROM blocks").fetchall():
                        try:
                            txt = "\n".join(self._extract_nested_text(json.loads(str(cj))))
                        except json.JSONDecodeError:
                            txt = str(cj)
                        if txt:
                            sources.append((f"dws_block:{nid}", txt))
                    for nid, body in con.execute("SELECT nodeId, COALESCE(content,'') FROM comments").fetchall():
                        if str(body).strip():
                            sources.append((f"dws_comment:{nid}", str(body)))
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        elif "dws" in json_states:
            txt = "\n".join(self._extract_nested_text(json_states["dws"]))
            if txt:
                sources.append(("dws:state.json", txt))

        if "jira" in dbs:
            con = self._open_ro_sqlite(dbs["jira"])
            if con is not None:
                try:
                    cols = {str(r[1]) for r in con.execute("PRAGMA table_info(issues)").fetchall()}
                    del_keys: set[str] = set()
                    if "is_deleted" in cols:
                        for (dk,) in con.execute("SELECT UPPER(key) FROM issues WHERE COALESCE(is_deleted, 0) != 0").fetchall():
                            del_keys.add(str(dk))
                        issue_q = "SELECT key, COALESCE(summary,''), COALESCE(description,'') FROM issues WHERE COALESCE(is_deleted, 0) = 0"
                    else:
                        issue_q = "SELECT key, COALESCE(summary,''), COALESCE(description,'') FROM issues"
                    for ikey, summ, desc in con.execute(issue_q).fetchall():
                        blob = f"{summ}\n{desc}".strip()
                        if blob:
                            sources.append((f"jira:{ikey}", blob))
                    for ikey, body in con.execute("SELECT UPPER(issue_key), COALESCE(body,'') FROM comments").fetchall():
                        if str(ikey) not in del_keys and str(body).strip():
                            sources.append((f"jira_comment:{ikey}", str(body)))
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        elif "jira" in json_states:
            for it in json_states["jira"].get("issues") or []:
                if isinstance(it, dict) and not it.get("is_deleted") and not it.get("deleted"):
                    k = str(it.get("key") or it.get("id") or "")
                    blob = f"{it.get('summary') or ''}\n{it.get('description') or ''}".strip()
                    if blob:
                        sources.append((f"jira:{k}", blob))
            for cm in json_states["jira"].get("comments") or []:
                if isinstance(cm, dict) and str(cm.get("body") or "").strip():
                    sources.append((f"jira_comment:{cm.get('issue_key') or ''}", str(cm.get("body"))))

        if "todoist" in dbs:
            con = self._open_ro_sqlite(dbs["todoist"])
            if con is not None:
                try:
                    cols = {str(r[1]) for r in con.execute("PRAGMA table_info(items)").fetchall()}
                    where_act = " WHERE COALESCE(is_deleted, 0) = 0" if "is_deleted" in cols else ""
                    for tid, cont, desc in con.execute(
                        f"SELECT id, COALESCE(content,''), COALESCE(description,'') FROM items{where_act}"
                    ).fetchall():
                        blob = f"{cont}\n{desc}".strip()
                        if blob:
                            sources.append((f"todoist:{tid}", blob))
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        elif "todoist" in json_states:
            for it in (json_states["todoist"].get("items") or json_states["todoist"].get("tasks") or []):
                if isinstance(it, dict) and not it.get("is_deleted") and not it.get("deleted"):
                    blob = f"{it.get('content') or ''}\n{it.get('description') or ''}".strip()
                    if blob:
                        sources.append((f"todoist:{it.get('id') or ''}", blob))

        return sources

    def _audit_cross_service_foreign_keys(
        self,
        dbs: dict[str, Path],
        json_states: dict[str, Any],
        issues: list[str],
    ) -> None:
        """Check referenced ``PROJ-\\d+`` keys and ``17943*`` Box IDs against active Jira/Box records."""
        active_jira: set[str] | None = None
        deleted_jira: set[str] = set()
        if "jira" in dbs:
            con = self._open_ro_sqlite(dbs["jira"])
            if con is not None:
                try:
                    cols = {str(r[1]) for r in con.execute("PRAGMA table_info(issues)").fetchall()}
                    if "is_deleted" in cols:
                        active_jira = {
                            str(r[0]).upper()
                            for r in con.execute(
                                "SELECT key FROM issues WHERE COALESCE(is_deleted, 0) = 0"
                            ).fetchall()
                        }
                        deleted_jira = {
                            str(r[0]).upper()
                            for r in con.execute(
                                "SELECT key FROM issues WHERE COALESCE(is_deleted, 0) != 0"
                            ).fetchall()
                        }
                    else:
                        active_jira = {str(r[0]).upper() for r in con.execute("SELECT key FROM issues").fetchall()}
                except sqlite3.Error:
                    active_jira = None
                finally:
                    con.close()
        elif "jira" in json_states:
            active_jira = set()
            for it in json_states["jira"].get("issues") or []:
                if isinstance(it, dict) and (it.get("key") or it.get("id")):
                    k = str(it.get("key") or it.get("id")).upper()
                    if it.get("is_deleted") or it.get("deleted"):
                        deleted_jira.add(k)
                    else:
                        active_jira.add(k)

        active_box: dict[str, str] | None = None
        deleted_box: set[str] = set()
        if "box" in dbs:
            con = self._open_ro_sqlite(dbs["box"])
            if con is not None:
                try:
                    active_box = {}
                    for tbl in ("folders", "files"):
                        cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()}
                        if not cols:
                            continue
                        if "is_deleted" in cols:
                            for bid, bname in con.execute(
                                f"SELECT id, name FROM {tbl} WHERE COALESCE(is_deleted, 0) = 0"
                            ).fetchall():
                                active_box[str(bid)] = str(bname or "")
                            for (bid,) in con.execute(
                                f"SELECT id FROM {tbl} WHERE COALESCE(is_deleted, 0) != 0"
                            ).fetchall():
                                deleted_box.add(str(bid))
                        else:
                            for bid, bname in con.execute(f"SELECT id, name FROM {tbl}").fetchall():
                                active_box[str(bid)] = str(bname or "")
                    for extra_tbl in ("shared_links", "collaborations", "tasks"):
                        cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({extra_tbl})").fetchall()}
                        if not cols or "id" not in cols:
                            continue
                        where_ex = " WHERE COALESCE(is_deleted, 0) = 0" if "is_deleted" in cols else ""
                        for (bid,) in con.execute(f"SELECT id FROM {extra_tbl}{where_ex}").fetchall():
                            active_box.setdefault(str(bid), extra_tbl)
                except sqlite3.Error:
                    active_box = None
                finally:
                    con.close()
        elif "box" in json_states:
            active_box = {}
            for tbl in ("folders", "files"):
                for item in json_states["box"].get(tbl) or []:
                    if isinstance(item, dict) and item.get("id") is not None:
                        bid = str(item["id"])
                        if item.get("is_deleted") or item.get("deleted"):
                            deleted_box.add(bid)
                        else:
                            active_box[bid] = str(item.get("name") or "")
            for extra_tbl in ("shared_links", "collaborations", "tasks"):
                for item in json_states["box"].get(extra_tbl) or []:
                    if isinstance(item, dict) and item.get("id") is not None:
                        if not item.get("is_deleted") and not item.get("deleted"):
                            active_box.setdefault(str(item["id"]), extra_tbl)

        if active_jira is None and active_box is None:
            return

        sources = self._collect_fk_sources(dbs, json_states)
        bad_jira: dict[str, list[str]] = {}
        bad_box: dict[str, list[str]] = {}
        deleted_17943_ids = sorted(bid for bid in deleted_box if bid.startswith("17943") and len(bid) >= 8)
        del_box_re = (
            re.compile(r"\b(?:" + "|".join(re.escape(b) for b in deleted_17943_ids) + r")\b")
            if deleted_17943_ids
            else None
        )

        for src_label, txt in sources:
            if active_jira is not None:
                for key in set(_PROJ_KEY_RE.findall(txt)):
                    ukey = key.upper()
                    if ukey not in active_jira:
                        bad_jira.setdefault(ukey, [])
                        if src_label not in bad_jira[ukey]:
                            bad_jira[ukey].append(src_label)
            if active_box is not None:
                found_box = set(_BOX_17943_ID_RE.findall(txt))
                if del_box_re is not None:
                    found_box.update(del_box_re.findall(txt))
                for bid in found_box:
                    if bid not in active_box:
                        bad_box.setdefault(bid, [])
                        if src_label not in bad_box[bid]:
                            bad_box[bid].append(src_label)

        if bad_jira and active_jira is not None:
            detail = "; ".join(
                f"{k} ({'deleted' if k in deleted_jira else 'nonexistent'}, found in {', '.join(locs[:3])})"
                for k, locs in sorted(bad_jira.items())[:5]
            )
            proj_keys = sorted(
                (k for k in active_jira if k.startswith("PROJ-")),
                key=lambda s: int(s.split("-", 1)[1]) if s.split("-", 1)[1].isdigit() else 0,
            )
            active_s = ", ".join(proj_keys[-20:]) if proj_keys else "(none)"
            issues.append(
                f"Cross-service Jira foreign-key mismatch: referenced stale/nonexistent key(s) [{detail}]. "
                f"Active PROJ-* keys in jira_mock.db: {active_s}. Update all outputs/GWS/DWS/Jira/Todoist references to active keys."
            )

        if bad_box and active_box is not None:
            detail = "; ".join(
                f"{bid} ({'deleted' if bid in deleted_box else 'nonexistent'}, found in {', '.join(locs[:3])})"
                for bid, locs in sorted(bad_box.items())[:5]
            )
            ordered_box = sorted(
                active_box.items(),
                key=lambda kv: (0 if kv[0].startswith("17943") else 1, kv[0]),
            )
            active_s = ", ".join(f"{bid} ({bname!r})" for bid, bname in ordered_box[:18]) or "(none)"
            issues.append(
                f"Cross-service Box foreign-key mismatch: referenced stale/deleted Box ID(s) [{detail}]. "
                f"Active Box folders/files in box_mock.db: {active_s}. Replace stale Box IDs across outputs/GWS/DWS/Jira."
            )

    def _audit_stripe_state(
        self,
        db_path: Path,
        issues: list[str],
        delta_parts: list[str],
    ) -> None:
        """Audit ``stripe_mock.sqlite`` for ``myproduct`` pollution and under-configured webhook endpoints."""
        con = self._open_ro_sqlite(db_path)
        if con is None:
            return
        try:
            tables = {str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            prod_rows: list[tuple[str, dict[str, Any]]] = []
            wh_rows: list[tuple[str, dict[str, Any]]] = []

            if "objects" in tables:
                for rid, resource, raw in con.execute(
                    "SELECT id, resource, data_json FROM objects "
                    "WHERE COALESCE(deleted, 0) = 0 AND resource IN ('products', 'webhook_endpoints')"
                ).fetchall():
                    try:
                        data = json.loads(str(raw))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        if resource == "products":
                            prod_rows.append((str(rid), data))
                        elif resource == "webhook_endpoints":
                            wh_rows.append((str(rid), data))
            else:
                for tbl, bucket in (("products", prod_rows), ("webhook_endpoints", wh_rows)):
                    if tbl not in tables:
                        continue
                    cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()}
                    col = "data" if "data" in cols else ("data_json" if "data_json" in cols else "")
                    if not col:
                        continue
                    where = " WHERE COALESCE(deleted, 0) = 0" if "deleted" in cols else ""
                    for rid, raw in con.execute(f"SELECT id, {col} FROM {tbl}{where}").fetchall():
                        try:
                            data = json.loads(str(raw))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, dict):
                            bucket.append((str(rid), data))

            myprods = [
                pid
                for pid, pd in prod_rows
                if str(pd.get("name") or "").strip().lower() == "myproduct"
            ]
            if myprods:
                issues.append(
                    f"Stripe contains synthetic 'myproduct' fixture pollution ({', '.join(myprods[:4])}) "
                    "created by `stripe trigger`; delete the synthetic 'myproduct' product(s) and any "
                    "associated triggered price/customer records from Stripe."
                )

            task_l = (self.task_markdown or "").lower()
            wants_lifecycle = any(
                k in task_l
                for k in (
                    "lifecycle",
                    "trial",
                    "invoice",
                    "subscription",
                    "生命周期",
                    "试用",
                    "发票",
                    "账单",
                    "订阅",
                )
            )
            if wants_lifecycle and wh_rows:
                thin_wh: list[str] = []
                for wid, wd in wh_rows:
                    evs = wd.get("enabled_events")
                    ev_list = evs if isinstance(evs, list) else []
                    if len(ev_list) <= 1:
                        url = str(wd.get("url") or "")
                        thin_wh.append(f"{wid} ({url or 'no-url'}, enabled_events={ev_list!r})")
                if thin_wh:
                    issues.append(
                        f"Stripe webhook endpoint only enables <= 1 event: {'; '.join(thin_wh[:3])}. "
                        "Include all relevant lifecycle events (e.g. customer.subscription.trial_will_end, "
                        "customer.subscription.updated, invoice.paid, invoice.payment_failed)."
                    )

            if myprods or wh_rows or "stripe" in self.mutated_services:
                delta_parts.append(f"stripe: products={len(prod_rows)} webhooks={len(wh_rows)}")
        except sqlite3.Error:
            pass
        finally:
            con.close()

    def _audit_dws_csv_uploads(
        self,
        cur_files: int,
        doc_rows: list[tuple[str, str]],
        has_json_dws: bool,
        issues: list[str],
    ) -> None:
        """Check that workspace CSV files are uploaded into DWS when requested."""
        csv_files = self._workspace_csv_files()
        if not csv_files:
            return
        task_l = (self.task_markdown or "").lower()
        has_file0 = any(
            str(dname or "").strip().lower() == "file_0"
            for dname, _ in (doc_rows or [])
        )
        wants_upload = has_file0 or (
            ("upload" in task_l or "上传" in task_l)
            and any(k in task_l for k in ("dws", "dossier", "档案"))
        )
        if not wants_upload:
            return
        csv_names = [p.name for p in csv_files]
        if cur_files == 0 and (has_json_dws or doc_rows is not None):
            issues.append(
                f"DWS has files=0 (no CSV/attachment files uploaded to DWS); upload the required workspace CSV files "
                f"({', '.join(csv_names[:6])}) via `dws`."
            )
            return

        if has_json_dws:
            return
        uploaded_names: set[str] = set()
        has_placeholder = False
        for dname, dext in doc_rows:
            nm = str(dname or "").strip().lower()
            ext = str(dext or "").strip().lower()
            if nm == "file_0" or (nm.startswith("file_") and ext == "file"):
                has_placeholder = True
            uploaded_names.add(nm)
            if ext and ext not in ("file", "folder", "adoc") and not nm.endswith("." + ext):
                uploaded_names.add(f"{nm}.{ext}")
        missing = [p.name for p in csv_files if p.name.lower() not in uploaded_names]
        if missing:
            extra = (
                " (`file_0` placeholder found — pass `--name <filename.csv>` to `dws doc upload`)"
                if has_placeholder
                else ""
            )
            issues.append(
                f"DWS uploaded file(s) missing required workspace CSVs: {', '.join(missing[:6])}{extra}; "
                "upload each required CSV via `dws doc upload` with `--name <filename.csv>`."
            )

    def _audit_todoist_state(
        self,
        dbs: dict[str, Path],
        json_states: dict[str, Any],
        issues: list[str],
        delta_parts: list[str],
    ) -> None:
        """Audit Todoist handoff labels and ``Re-source``/``capability-gap`` label completeness."""
        items: list[tuple[str, str, str, list[str]]] = []
        completed_count = 0

        if "todoist" in dbs:
            con = self._open_ro_sqlite(dbs["todoist"])
            if con is not None:
                try:
                    cols = {str(r[1]) for r in con.execute("PRAGMA table_info(items)").fetchall()}
                    if not cols:
                        return
                    base_t = self._service_baseline.get("todoist") or {}
                    cur_i = self._sqlite_table_count(con, "items", active_only=True)
                    checked_col = "checked" if "checked" in cols else ("is_completed" if "is_completed" in cols else "")
                    if checked_col:
                        r = con.execute(f"SELECT COUNT(*) FROM items WHERE {checked_col} != 0").fetchone()
                        completed_count = int(r[0]) if r else 0
                    di = cur_i - int(base_t.get("items", cur_i))
                    dc = completed_count - int(base_t.get("completed", completed_count))
                    if di or dc or "todoist" in self.mutated_services:
                        delta_parts.append(f"todoist: items={cur_i}({di:+d}) completed={completed_count}({dc:+d})")

                    sec_map: dict[str, str] = {}
                    try:
                        sec_cols = {str(r[1]) for r in con.execute("PRAGMA table_info(sections)").fetchall()}
                        if sec_cols:
                            w_sec = " WHERE COALESCE(is_deleted, 0) = 0" if "is_deleted" in sec_cols else ""
                            for sid, sname in con.execute(f"SELECT id, name FROM sections{w_sec}").fetchall():
                                sec_map[str(sid)] = str(sname or "")
                    except sqlite3.Error:
                        pass

                    l_col = "labels_json" if "labels_json" in cols else ("labels" if "labels" in cols else "")
                    w_item = " WHERE COALESCE(is_deleted, 0) = 0" if "is_deleted" in cols else ""
                    s_col = "section_id" if "section_id" in cols else "''"
                    d_col = "description" if "description" in cols else "''"
                    l_expr = l_col or "'[]'"
                    for cont, desc, sec_id, raw_lbls in con.execute(
                        f"SELECT COALESCE(content,''), COALESCE({d_col},''), COALESCE({s_col},''), COALESCE({l_expr},'[]') FROM items{w_item}"
                    ).fetchall():
                        lbls: list[str] = []
                        if isinstance(raw_lbls, str) and raw_lbls.strip().startswith("["):
                            try:
                                parsed = json.loads(raw_lbls)
                                if isinstance(parsed, list):
                                    lbls = [str(x) for x in parsed if x]
                            except json.JSONDecodeError:
                                pass
                        elif raw_lbls:
                            lbls = [s.strip() for s in str(raw_lbls).split(",") if s.strip()]
                        items.append((str(cont), str(desc), sec_map.get(str(sec_id), ""), lbls))
                except sqlite3.Error:
                    return
                finally:
                    con.close()
        elif "todoist" in json_states:
            tdata = json_states["todoist"]
            sec_map_j: dict[str, str] = {}
            for s in tdata.get("sections") or []:
                if isinstance(s, dict) and s.get("id") is not None:
                    sec_map_j[str(s["id"])] = str(s.get("name") or "")
            raw_items = tdata.get("items") or tdata.get("tasks") or []
            for it in raw_items:
                if not isinstance(it, dict) or it.get("is_deleted") or it.get("deleted"):
                    continue
                if it.get("checked") or it.get("is_completed"):
                    completed_count += 1
                lbls_raw = it.get("labels") or []
                lbls = [str(x) for x in lbls_raw if x] if isinstance(lbls_raw, list) else []
                sec_nm = str(it.get("section_name") or it.get("section") or sec_map_j.get(str(it.get("section_id") or ""), ""))
                items.append((str(it.get("content") or ""), str(it.get("description") or ""), sec_nm, lbls))
            if items:
                delta_parts.append(f"todoist: items={len(items)} completed={completed_count}")
        else:
            return

        handoff_missing: list[str] = []
        proc_missing: list[str] = []
        spec_missing: list[str] = []

        for cont, desc, sec_nm, lbls in items:
            cont_l = cont.lower()
            lbls_l = {l.lower() for l in lbls}
            if any(k in cont_l for k in ("handoff", "standup", "summary")) and lbls:
                if not any(any(k in l for k in ("handoff", "summary", "meta")) for l in lbls_l):
                    handoff_missing.append(f"{cont[:45]!r} (labels={lbls})")

            in_resource = "re-source" in sec_nm.lower() or "capability-gap" in lbls_l
            if not in_resource:
                continue
            blob = f"{cont}\n{desc}"
            blob_l = blob.lower()
            has_process_contrast = any(
                re.search(p1, blob, re.I) and re.search(p2, blob, re.I)
                for p1, p2 in (
                    (r"\bcompression[- ]mould\w*", r"(?:\binjection[- ]mould\w*|\bliquid silicone rubber\b|\bLSR\b)"),
                    (r"(?:\bflexible printed circuit\b|\bFPC\b)", r"\brigid FR-?4\b"),
                    (r"\bdiamond[- ]polish\w*", r"\btumble finish\w*"),
                    (r"(?:\bmetal[- ]injection[- ]mould\w*|\bMIM\b)", r"\binvestment casting\b"),
                    (r"\bUSB-C cable assembly\b", r"\bcrimped wire harness\w*"),
                )
            )
            if has_process_contrast and "process-gap" not in lbls_l:
                proc_missing.append(cont[:60])

            has_spec_gap = (
                bool(re.search(r"\bClass\s+2A\b", blob, re.I))
                or bool(
                    re.search(r"\boptical[- ]grade\b", blob, re.I)
                    and re.search(r"(?:\bopaque\b|\btumble finish\w*|\bdiamond[- ]polish\w*|\blight[- ]pipe\b)", blob, re.I)
                )
                or ("e-marker" in blob_l and bool(re.search(r"\b5\s*A\b", blob)))
                or bool(re.search(r"(?:\bEMI gaskets?\b|conductive-fabric-over-foam)", blob, re.I))
            )
            if has_spec_gap and "spec-gap" not in lbls_l:
                spec_missing.append(cont[:60])

        if handoff_missing:
            issues.append(
                f"Todoist handoff/summary task(s) missing a handoff/summary label: {'; '.join(handoff_missing[:3])} "
                "— add label 'handoff' (or 'summary'/'meta')."
            )
        if proc_missing:
            issues.append(
                f"Todoist Re-source card(s) with process contrast missing label 'process-gap': "
                f"{'; '.join(proc_missing[:4])} — add 'process-gap'."
            )
        if spec_missing:
            issues.append(
                f"Todoist Re-source card(s) with missing/unverified spec or rating missing label 'spec-gap': "
                f"{'; '.join(spec_missing[:4])} — add 'spec-gap'."
            )

    @staticmethod
    def _doc_full_text(doc: dict[str, Any]) -> str:
        """Extract plain text lines from a ``google_docs_mock`` document record."""
        content = doc.get("content")
        if isinstance(content, dict) and isinstance(content.get("blocks"), list):
            lines = [str(b.get("text") or "").strip() for b in content["blocks"] if isinstance(b, dict)]
            return "\n".join(ln for ln in lines if ln)
        for key in ("body", "text", "content"):
            val = doc.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return ""

    @staticmethod
    def _extract_high_open_findings(doc: dict[str, Any]) -> list[str]:
        """Return High-severity Open finding descriptions from a Google Docs audit document."""
        blocks = ((doc.get("content") or {}).get("blocks")) if isinstance(doc.get("content"), dict) else None
        entries: list[tuple[str, str]] = []
        if isinstance(blocks, list) and blocks:
            for b in blocks:
                if isinstance(b, dict):
                    t = str(b.get("text") or "").strip()
                    if t:
                        entries.append((str(b.get("type") or "").lower(), t))
        else:
            for ln in ToolHost._doc_full_text(doc).splitlines():
                if ln.strip():
                    entries.append(("paragraph", ln.strip()))

        findings: dict[str, str] = {}
        order: list[str] = []
        in_reassess = False
        for btype, txt in entries:
            if txt.lower().startswith("re-assessment"):
                in_reassess = True
                continue
            if in_reassess:
                if btype.startswith("heading"):
                    in_reassess = False
                else:
                    m_f = re.search(r"\bFinding\s+(\d+)\b", txt, re.I)
                    fid = f"Finding {m_f.group(1)}" if m_f else f"Re-assessment Finding ({len(order) + 1})"
                    if fid not in findings:
                        order.append(fid)
                    findings[fid] = txt
                    continue
            m = re.match(r"^(?:[-*]\s*)?(Finding\s+\d+|F-\d+)\b[.:|\s-]*(.*)$", txt, re.I)
            if m:
                fid = m.group(1)
                if fid not in findings:
                    order.append(fid)
                findings[fid] = txt

        def _is_resolved(t: str) -> bool:
            tl = t.lower()
            if re.search(
                r"\b(?:status:\s*open|finding\s+remains?\s+open|finding\s+is\s+open|"
                r"no\s+remediation\b.*?\b(?:agreed|approved|verified)|not\s+yet\s+(?:issued|approved|been\s+received)|"
                r"no\s+verified\s+(?:correction|completion|record)|promises?\s+to\s+complete|remain\s+scheduled|"
                r"pending\s+a?\s*certified)\b",
                tl,
            ):
                return False
            return bool(
                re.search(
                    r"(?:\b(?:confirmed\s+resolved|status:\s*(?:resolved|closed)|no\s+residual\s+issue\s+remains|"
                    r"issued\s+a?\s*(?:written\s+)?clearance|quarantined\s+and\s+destroyed|"
                    r"replaced\s+it\b.*\bconfirmed|repaired\s+it\b.*\bconfirmed|"
                    r"completed\s+installation\b.*\bverified|upgraded\b.*\bcompleted\s+and\s+verified|"
                    r"auditor\s+confirmed\b.*\bcomplete|reviewed\s+and\s+accepted|"
                    r"formal\s+acknowledgement\b.*\bno\s+recurrence|"
                    r"documented\b.*\breviewed\b.*\bconfirmed\s+it\s+was\s+captured)\b|"
                    r"\b(?:closed|resolved)\b(?=\s*(?:$|[|)\],;])))",
                    tl,
                )
            )

        def _is_high(t: str) -> bool:
            tl = t.lower()
            if re.search(
                r"\b(?:severity:\s*high|with\s+high\s+severity|classified\s+(?:the\s+issue|this)\s+as\s+high|\[\s*high\s*\])\b",
                tl,
            ):
                return True
            if re.search(
                r"\b(?:severity:\s*(?:medium|low)|classified\s+this\s+as\s+a\s+documentation|\bmedium\b|\blow\b)\b",
                tl,
            ) and "high" not in tl:
                return False
            if re.search(
                r"\b(?:regulatory\s+(?:violation|threshold|permit\s+violation)|permit\s+violation|"
                r"environmental\s+contamination\s+risk|exceeding\s+the\s+regulatory\s+threshold|"
                r"eu\s+rohs|eu\s+reach|green\s+claims\s+directive|statutory\b.*\bcap)\b",
                tl,
            ):
                return True
            if re.search(
                r"\b(?:worker-safety\s+hazard|direct\s+product-safety\s+risk|catastrophic\s+press\s+failure|"
                r"fire\s+suppression\s+system\b.*\bnon-operational|respiratory\s+ppe|osha-aligned)\b",
                tl,
            ):
                return True
            for m_usd in re.finditer(r"(?:USD|\$)\s*([\d,\s]+)", t, re.I):
                num_s = re.sub(r"[\s,]", "", m_usd.group(1))
                if num_s.isdigit() and int(num_s) > 50_000:
                    return True
            return False

        return [f"{fid} ({findings[fid][:85]})" for fid in order if _is_high(findings[fid]) and not _is_resolved(findings[fid])]

    def _audit_google_docs_rollup(
        self,
        gdocs_state: dict[str, Any],
        issues: list[str],
        delta_parts: list[str],
    ) -> None:
        """Cross-check ``<Supplier Name> | High+Open: <N>`` rollup lines against audit docs in ``state.json``."""
        docs = [
            f
            for f in (gdocs_state.get("files") or gdocs_state.get("documents") or [])
            if isinstance(f, dict) and not f.get("trashed") and str(f.get("type") or "document") in ("document", "doc", "")
        ]
        if not docs:
            return

        audit_docs: list[tuple[str, str, list[str]]] = []
        rollup_docs: list[tuple[str, list[tuple[str, int]]]] = []
        for doc in docs:
            title = str(doc.get("name") or doc.get("title") or doc.get("id") or "").strip()
            txt = self._doc_full_text(doc)
            matches = _GDOCS_ROLLUP_LINE_RE.findall(txt)
            if matches:
                rollup_docs.append((title, [(m[0].strip(), int(m[1])) for m in matches]))
                continue
            high_open = self._extract_high_open_findings(doc)
            supplier_key = re.sub(r"\s*[—–-]\s*(?:Q\d.*|Audit.*)$", "", title, flags=re.I).strip()
            if supplier_key and ("audit" in title.lower() or "finding" in txt.lower() or high_open):
                audit_docs.append((supplier_key, supplier_key.lower(), high_open))

        if not rollup_docs or not audit_docs:
            return

        for rtitle, entries in rollup_docs:
            reported_names: set[str] = set()
            for sup_name, claimed_n in entries:
                sup_l = sup_name.lower()
                matched = next(
                    (ad for ad in audit_docs if ad[1] == sup_l or sup_l in ad[1] or ad[1] in sup_l),
                    None,
                )
                if matched is None:
                    continue
                canon_name, canon_l, high_open = matched
                reported_names.add(canon_l)
                if claimed_n != len(high_open):
                    f_list = "; ".join(high_open[:5]) or "(none)"
                    issues.append(
                        f"Google Docs roll-up {rtitle!r} reports `{sup_name} | High+Open: {claimed_n}` "
                        f"but {canon_name!r} audit doc has {len(high_open)} High+Open finding(s): {f_list}."
                    )
            missing_qualifying = [
                f"{canon_name} (High+Open: {len(high_open)}: {'; '.join(high_open[:3])})"
                for canon_name, canon_l, high_open in audit_docs
                if high_open and canon_l not in reported_names
            ]
            if missing_qualifying:
                issues.append(
                    f"Google Docs roll-up {rtitle!r} omits qualifying High+Open supplier(s): "
                    f"{'; '.join(missing_qualifying[:5])}."
                )
        delta_parts.append(f"google_docs: docs={len(docs)} rollups={len(rollup_docs)}")

    def audit_service_state_delta(self) -> tuple[bool, list[str], str]:
        """Audit live mock service state deltas before exit and emit ``(blocking, issues, [service-delta])``."""
        if not self.is_sj:
            return False, [], ""

        issues: list[str] = []
        delta_parts: list[str] = []
        dbs = self._find_service_state_dbs()
        json_states = self._load_json_service_states()

        # --- Jira ---
        if "jira" in dbs:
            con = self._open_ro_sqlite(dbs["jira"])
            if con is not None:
                try:
                    base_j = self._service_baseline.get("jira") or {}
                    base_lc: dict[tuple[str, str, str], int] = base_j.get("link_counts") or {}
                    links = con.execute(
                        "SELECT UPPER(inward_key), UPPER(outward_key), link_type FROM issue_links"
                    ).fetchall()
                    cur_lc: dict[tuple[str, str, str], tuple[str, int]] = {}
                    for inw, outw, lt in links:
                        key = (str(inw), str(outw), str(lt).lower())
                        orig_lt, cnt = cur_lc.get(key, (str(lt), 0))
                        cur_lc[key] = (orig_lt, cnt + 1)

                    dup_msgs: list[str] = []
                    for key, (orig_lt, cnt) in sorted(cur_lc.items()):
                        if cnt > max(1, int(base_lc.get(key, 0))):
                            dup_msgs.append(f"{key[0]}->{key[1]} ({orig_lt} x{cnt})")
                    if dup_msgs:
                        issues.append(
                            f"Jira duplicate issue link(s) found: {', '.join(dup_msgs[:4])} "
                            "— remove extra duplicate links so each pair has count=1."
                        )

                    dup_rows = con.execute(
                        "SELECT DISTINCT l.inward_key, l.outward_key, l.link_type, s.name, s.category "
                        "FROM issue_links l "
                        "JOIN issues i ON UPPER(i.key) = UPPER(l.inward_key) "
                        "JOIN issue_statuses s ON i.status_id = s.id "
                        "WHERE LOWER(l.link_type) LIKE '%duplicate%'"
                    ).fetchall()
                    todo_dups: list[str] = []
                    for inw, outw, lt, st_name, st_cat in dup_rows:
                        key = (str(inw).upper(), str(outw).upper(), str(lt).lower())
                        if key in base_lc:
                            continue
                        is_done = str(st_cat or "").strip().lower() == "done" or str(st_name or "").strip().lower() == "done"
                        if not is_done:
                            todo_dups.append(f"{inw} ({st_name})")
                    if todo_dups:
                        issues.append(
                            f"Jira issue(s) linked as 'Duplicates' are not transitioned to 'Done': "
                            f"{', '.join(todo_dups[:6])} — move duplicate issues to 'Done'."
                        )

                    done_row = con.execute(
                        "SELECT COUNT(*) FROM issues i JOIN issue_statuses s ON i.status_id = s.id "
                        "WHERE LOWER(s.category) = 'done' OR LOWER(s.name) = 'done'"
                    ).fetchone()
                    cur_done = int(done_row[0]) if done_row else 0
                    cur_links = len(links)
                    dup_link_cnt = sum(1 for _, _, lt in links if "duplicate" in str(lt).lower())
                    task_l = (self.task_markdown or "").lower()
                    wants_dups = any(k in task_l for k in ("duplicate", "duplicates", "查重"))
                    wants_triage_only = (
                        not wants_dups
                        and "triage" in task_l
                        and "jira" in task_l
                        and "jira 里原有的 issue 也别碰" not in task_l
                    )
                    if wants_dups and dup_link_cnt == 0:
                        issues.append(
                            f"Jira issue_links has 0 'Duplicates' links (total issue_links={cur_links}); "
                            "identify duplicate defects and link them using the 'Duplicates' link type."
                        )
                    elif wants_triage_only and cur_links == 0:
                        issues.append(
                            "Jira issue_links table has 0 links; link related/duplicate defects using "
                            "`issue_links` (e.g. 'Duplicates')."
                        )
                    d_links = cur_links - int(base_j.get("total_links", cur_links))
                    d_done = cur_done - int(base_j.get("done", cur_done))
                    if d_links or d_done or dup_msgs or todo_dups or "jira" in self.mutated_services:
                        delta_parts.append(f"jira: links={cur_links}({d_links:+d}) done={cur_done}({d_done:+d})")
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        elif "jira" in json_states:
            jdata = json_states["jira"]
            jlinks = jdata.get("issue_links") or jdata.get("links") or []
            jissues = {
                str(it.get("key") or it.get("id") or "").upper(): str(it.get("status") or "")
                for it in (jdata.get("issues") or [])
                if isinstance(it, dict)
            }
            lc: dict[tuple[str, str, str], int] = {}
            todo_dups_j: list[str] = []
            for lk in jlinks:
                if not isinstance(lk, dict):
                    continue
                src = str(lk.get("inward_key") or lk.get("source") or "").upper()
                tgt = str(lk.get("outward_key") or lk.get("target") or "").upper()
                ltype = str(lk.get("link_type") or lk.get("type") or "")
                k = (src, tgt, ltype.lower())
                lc[k] = lc.get(k, 0) + 1
                if "duplicate" in k[2] and src in jissues and jissues[src].strip().lower() != "done":
                    todo_dups_j.append(f"{src} ({jissues[src]})")
            dup_j = [f"{k[0]}->{k[1]} (x{c})" for k, c in lc.items() if c > 1]
            if dup_j:
                issues.append(f"Jira duplicate issue link(s) found: {', '.join(dup_j[:4])}.")
            if todo_dups_j:
                issues.append(f"Jira 'Duplicates' issue(s) still not 'Done': {', '.join(todo_dups_j[:6])}.")
            dup_link_cnt_j = sum(c for k, c in lc.items() if "duplicate" in k[2])
            task_l = (self.task_markdown or "").lower()
            wants_dups = any(k in task_l for k in ("duplicate", "duplicates", "查重"))
            wants_triage_only = (
                not wants_dups
                and "triage" in task_l
                and "jira" in task_l
                and "jira 里原有的 issue 也别碰" not in task_l
            )
            if wants_dups and dup_link_cnt_j == 0:
                issues.append(
                    f"Jira issue_links has 0 'Duplicates' links (total issue_links={len(jlinks)}); "
                    "identify duplicate defects and link them using the 'Duplicates' link type."
                )
            elif wants_triage_only and not jlinks:
                issues.append(
                    "Jira issue_links has 0 links; link related/duplicate defects using 'Duplicates'."
                )
            if jlinks or jissues:
                done_j = sum(1 for st in jissues.values() if st.strip().lower() == "done")
                delta_parts.append(f"jira: links={len(jlinks)} done={done_j}")

        # --- Notion ---
        if "ntn" in dbs:
            con = self._open_ro_sqlite(dbs["ntn"])
            if con is not None:
                try:
                    base_n = self._service_baseline.get("ntn") or {}
                    base_pids: set[str] = base_n.get("page_ids") or set()
                    base_fids: set[str] = base_n.get("file_ids") or set()
                    page_rows = con.execute("SELECT id, data FROM entities WHERE type = 'pages'").fetchall()
                    all_pids = {str(r[0]) for r in page_rows}
                    has_root = "page_root" in all_pids
                    orphan_pages: list[str] = []
                    new_active_pages = 0
                    for pid, raw in page_rows:
                        pid_s = str(pid)
                        if pid_s in base_pids or pid_s == "page_root":
                            continue
                        try:
                            pd = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if pd.get("archived") or pd.get("inTrash"):
                            continue
                        new_active_pages += 1
                        if has_root and not pd.get("parentId"):
                            title = str(pd.get("title") or pid_s)[:60]
                            orphan_pages.append(f"{title!r} ({pid_s})")
                    if orphan_pages:
                        issues.append(
                            f"Notion newly created page(s) have parentId=None instead of 'page_root' "
                            f"(or workspace parent): {', '.join(orphan_pages[:3])} — recreate or attach under 'page_root'."
                        )

                    file_rows = con.execute("SELECT id, data FROM entities WHERE type = 'files'").fetchall()
                    new_files: list[tuple[str, str]] = []
                    for fid, raw in file_rows:
                        fid_s = str(fid)
                        if fid_s in base_fids:
                            continue
                        try:
                            fd = json.loads(raw)
                            fname = str(fd.get("filename") or fd.get("name") or fid_s)
                        except json.JSONDecodeError:
                            fname = fid_s
                        new_files.append((fid_s, fname))

                    probe_files = [
                        f"{fn} ({fid})"
                        for fid, fn in new_files
                        if _is_synthetic_probe_name(fn) and not self._name_exists_in_workspace(Path(fn).name.lower())
                    ]
                    if probe_files:
                        issues.append(
                            f"Notion has synthetic probe uploaded file(s): {', '.join(probe_files[:3])} "
                            "— clean up stray test/sample uploads."
                        )
                    task_l = (self.task_markdown or "").lower()
                    wants_upload = any(
                        tok in task_l
                        for tok in ("ntn files", "files create", "files upload", "上传", "upload", "evidence file", "certificate packet")
                    )
                    single_upload = (
                        "恰好一个" in self.task_markdown
                        or "exactly one" in task_l
                        or "only one" in task_l
                    )
                    if new_files and not wants_upload:
                        issues.append(
                            f"Notion has {len(new_files)} unrequested uploaded file(s) "
                            f"({', '.join(fn for _, fn in new_files[:3])}) when task did not ask for file uploads."
                        )
                    elif single_upload and len(new_files) > 1:
                        issues.append(
                            f"Notion has {len(new_files)} newly uploaded files "
                            f"({', '.join(fn for _, fn in new_files[:4])}) when exactly 1 was requested."
                        )

                    if new_active_pages or new_files or orphan_pages or "ntn" in self.mutated_services:
                        fnames_s = ",".join(fn for _, fn in new_files[:3])
                        suffix = f" [{fnames_s}]" if fnames_s else ""
                        delta_parts.append(
                            f"ntn: +pages={new_active_pages}(orphan={len(orphan_pages)}) "
                            f"files={len(file_rows)}(+{len(new_files)}{suffix})"
                        )
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        elif "ntn" in json_states:
            ndata = json_states["ntn"]
            npages = ndata.get("pages") or []
            nfiles = ndata.get("files") or []
            has_root = any(isinstance(p, dict) and p.get("id") == "page_root" for p in npages)
            orphan_j = [
                f"{p.get('title', p.get('id'))!r}"
                for p in npages
                if isinstance(p, dict)
                and p.get("id") != "page_root"
                and p.get("is_new", True)
                and not p.get("archived")
                and has_root
                and not p.get("parentId")
            ]
            if orphan_j:
                issues.append(f"Notion newly created page(s) have parentId=None: {', '.join(orphan_j[:3])}.")
            extra_f = [f for f in nfiles if isinstance(f, dict) and f.get("is_new")]
            if extra_f:
                task_l = (self.task_markdown or "").lower()
                wants_up = any(k in task_l for k in ("ntn files", "files create", "上传", "upload", "evidence"))
                if not wants_up or (
                    ("恰好一个" in self.task_markdown or "exactly one" in task_l) and len(extra_f) > 1
                ):
                    issues.append(f"Notion has extra uploaded file(s) ({len(extra_f)}).")
            if npages or nfiles:
                delta_parts.append(f"ntn: pages={len(npages)}(orphan={len(orphan_j)}) files={len(nfiles)}")

        # --- Box ---
        if "box" in dbs:
            con = self._open_ro_sqlite(dbs["box"])
            if con is not None:
                try:
                    base_b = self._service_baseline.get("box") or {}
                    cur_b = {
                        "folders": self._sqlite_table_count(con, "folders", active_only=True),
                        "files": self._sqlite_table_count(con, "files", active_only=True),
                        "collabs": self._sqlite_table_count(con, "collaborations", active_only=True),
                        "tasks": self._sqlite_table_count(con, "tasks", active_only=True),
                    }
                    deltas = {k: cur_b[k] - int(base_b.get(k, cur_b[k])) for k in cur_b}
                    if any(deltas.values()) or "box" in self.mutated_services:
                        delta_parts.append(
                            f"box: folders={cur_b['folders']}({deltas['folders']:+d}) "
                            f"files={cur_b['files']}({deltas['files']:+d}) "
                            f"collabs={cur_b['collabs']}({deltas['collabs']:+d}) "
                            f"tasks={cur_b['tasks']}({deltas['tasks']:+d})"
                        )
                finally:
                    con.close()

        # --- DWS ---
        if "dws" in dbs:
            con = self._open_ro_sqlite(dbs["dws"])
            if con is not None:
                try:
                    base_d = self._service_baseline.get("dws") or {}
                    file_row = con.execute("SELECT COUNT(*) FROM documents WHERE type = 'file'").fetchone()
                    cur_d = {
                        "docs": self._sqlite_table_count(con, "documents"),
                        "files": int(file_row[0]) if file_row else 0,
                        "blocks": self._sqlite_table_count(con, "blocks"),
                        "comments": self._sqlite_table_count(con, "comments"),
                        "exports": self._sqlite_table_count(con, "exports"),
                    }
                    try:
                        doc_rows = [
                            (str(r[0] or ""), str(r[1] or ""))
                            for r in con.execute(
                                "SELECT COALESCE(name, ''), COALESCE(extension, '') "
                                "FROM documents WHERE type = 'file'"
                            ).fetchall()
                        ]
                    except sqlite3.Error:
                        doc_rows = [
                            (str(r[0] or ""), "")
                            for r in con.execute(
                                "SELECT COALESCE(name, '') FROM documents WHERE type = 'file'"
                            ).fetchall()
                        ]
                    self._audit_dws_csv_uploads(cur_d["files"], doc_rows, False, issues)
                    deltas = {k: cur_d[k] - int(base_d.get(k, cur_d[k])) for k in cur_d}
                    if any(deltas.values()) or "dws" in self.mutated_services:
                        delta_parts.append(
                            f"dws: docs={cur_d['docs']}({deltas['docs']:+d}, files={deltas['files']:+d}) "
                            f"blocks={cur_d['blocks']}({deltas['blocks']:+d}) "
                            f"comments={cur_d['comments']}({deltas['comments']:+d})"
                        )
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        elif "dws" in json_states:
            dws_j = json_states["dws"]
            fval = dws_j.get("files")
            cur_f = len(fval) if isinstance(fval, list) else int(fval or 0)
            self._audit_dws_csv_uploads(cur_f, [], True, issues)
            if cur_f or "dws" in self.mutated_services:
                delta_parts.append(f"dws: files={cur_f}")

        # --- GWS ---
        if "gws" in dbs:
            con = self._open_ro_sqlite(dbs["gws"])
            if con is not None:
                try:
                    base_g = self._service_baseline.get("gws") or {}
                    base_ph = base_g.get("pres_hashes") or {}
                    base_sh = base_g.get("sheet_hashes") or {}
                    pres_rows = con.execute("SELECT id, data FROM presentations").fetchall()
                    sheet_rows = con.execute("SELECT id, data FROM spreadsheets").fetchall()
                    mod_pres_summaries: list[str] = []
                    for pid, raw in pres_rows:
                        pid_s = str(pid)
                        raw_s = str(raw)
                        h = hashlib.sha256(raw_s.encode("utf-8")).hexdigest()
                        if pid_s not in base_ph or h != base_ph[pid_s]:
                            try:
                                pd = json.loads(raw_s)
                            except json.JSONDecodeError:
                                continue
                            nslides, counts, stale = self._extract_gws_presentation_summary(pd)
                            title = str(pd.get("title") or pid_s)[:36]
                            extra = ", stale_template_slide0" if stale else ""
                            cnt_s = f", counts={counts}" if counts else ", counts=[]"
                            mod_pres_summaries.append(f"{title!r}(slides={nslides}{cnt_s}{extra})")
                    sheets_mod = sum(
                        1
                        for sid, raw in sheet_rows
                        if str(sid) not in base_sh
                        or hashlib.sha256(str(raw).encode("utf-8")).hexdigest() != base_sh[str(sid)]
                    )
                    if mod_pres_summaries or sheets_mod or "gws" in self.mutated_services:
                        pres_s = "; ".join(mod_pres_summaries[:2]) if mod_pres_summaries else "pres_mod=0"
                        delta_parts.append(f"gws: {pres_s}, sheets_mod={sheets_mod}")
                except sqlite3.Error:
                    pass
                finally:
                    con.close()

        # --- Todoist ---
        self._audit_todoist_state(dbs, json_states, issues, delta_parts)

        # --- Stripe ---
        if "stripe" in dbs:
            self._audit_stripe_state(dbs["stripe"], issues, delta_parts)

        # --- Google Docs Rollup ---
        if "google_docs" in json_states:
            self._audit_google_docs_rollup(json_states["google_docs"], issues, delta_parts)

        # --- Cross-Service Foreign Keys (Jira PROJ-* & Box 17943*) ---
        self._audit_cross_service_foreign_keys(dbs, json_states, issues)

        receipt = f"[service-delta: {' | '.join(delta_parts)}]" if delta_parts else ""
        if len(receipt) > 650:
            receipt = receipt[:646] + "...]"
        return bool(issues), issues, receipt

    def _track_cli_loop_streak(self, cmd_rw: str, out: str) -> str:
        """Append ``[batch-compute-hint]`` after ``>= 3`` consecutive single-item turns on one domain CLI."""
        if (
            not self.is_sj
            or not cmd_rw
            or out.startswith(("[ctx-entry-gate]", "[straitjacket]", "ERROR:"))
            or "--help" in cmd_rw
            or re.search(r"(?:^|\s)-h(?:\s|$)", cmd_rw)
        ):
            return out

        invs = _extract_cli_invocations(cmd_rw)
        dom_invs = [
            b
            for b, rest, _ in invs
            if b in _DOMAIN_CLIS
            and rest
            and "--help" not in rest
            and not re.search(r"(?:^|\s)-h(?:\s|$)", rest)
        ]
        is_batched = len(dom_invs) != 1 or bool(re.search(r"\b(?:for|while)\b", cmd_rw))
        if is_batched:
            self._cli_loop_bin = ""
            self._cli_loop_streak = 0
            return out

        cli_bin = dom_invs[0]
        if self.step > 0 and self.step == self._cli_loop_last_step and cli_bin == self._cli_loop_bin:
            return out
        self._cli_loop_last_step = self.step

        if cli_bin == self._cli_loop_bin:
            self._cli_loop_streak += 1
        else:
            self._cli_loop_bin = cli_bin
            self._cli_loop_streak = 1

        if self._cli_loop_streak >= 3 and self._cli_loop_hints.get(cli_bin, 0) < 2:
            self._cli_loop_hints[cli_bin] = self._cli_loop_hints.get(cli_bin, 0) + 1
            self._cli_loop_streak = 0
            out += (
                "\n[batch-compute-hint: 3 single-item CLI turns detected; "
                "batch remaining CLI calls/mutations in one python3/bash loop to avoid per-turn prefix re-billing]"
            )
        return out

    def _do_ctx_retrieve(self, args: dict[str, Any]) -> str:
        if not self.is_sj:
            return "ERROR: ctx_retrieve is unavailable in baseline arm"
        raw_handle = str(args.get("handle") or "").strip()
        if not raw_handle:
            return "ERROR: ctx_retrieve requires a non-empty 'handle' (e.g. 'run:8d8335db6848')"
        handle = raw_handle  # guest paths are rewritten only on the sandboxed route
        stream = str(args.get("stream") or "stdout").strip().lower()
        if stream not in ("stdout", "stderr"):
            stream = "stdout"
        mode = str(args.get("mode") or "").strip().lower()
        if not mode:
            if args.get("pattern"):
                mode = "search"
            elif args.get("query"):
                mode = "query"
            else:
                mode = "get"

        target_with_stream = (
            f"{handle}#{stream}"
            if (handle.startswith("run:") and "#" not in handle)
            else handle
        )
        if mode == "get":
            cmd_args = ["get", target_with_stream]
            if args.get("json_pointer") is not None and str(args.get("json_pointer")).strip() != "":
                cmd_args.extend(["--json-pointer", str(args["json_pointer"]).strip()])
            elif args.get("records"):
                cmd_args.extend(["--records", str(args["records"]).strip()])
            elif args.get("lines"):
                cmd_args.extend(["--lines", str(args["lines"]).strip()])
            else:
                cmd_args.extend(["--lines", "1:120"])
            metric_verb = "get"
        elif mode == "search":
            pat = str(args.get("pattern") or args.get("query") or "").strip()
            if not pat:
                return "ERROR: ctx_retrieve mode='search' requires 'pattern'"
            cmd_args = ["search", target_with_stream, pat]
            if bool(args.get("fixed", False)):
                cmd_args.append("--fixed")
            ctx_lines = int(args.get("context") if args.get("context") is not None else 2)
            if ctx_lines > 0:
                cmd_args.extend(["--context", str(min(ctx_lines, 20))])
            metric_verb = "search"
        elif mode == "stats":
            cmd_args = ["stats", handle]
            metric_verb = "stats"
        elif mode in ("query", "q"):
            q_expr = str(args.get("query") or "").strip()
            if not q_expr:
                q_expr = f"records {target_with_stream} | count"
            elif target_with_stream not in q_expr and handle not in q_expr:
                if re.match(r"^(?:records|lines)\b", q_expr):
                    parts = q_expr.split("|", 1)
                    head_tok = parts[0].strip()
                    rest = f" | {parts[1].strip()}" if len(parts) > 1 else ""
                    q_expr = f"{head_tok} {target_with_stream}{rest}"
                else:
                    q_expr = f"records {target_with_stream} | {q_expr}"
            cmd_args = ["q", q_expr]
            metric_verb = "q"
        else:
            return (
                f"ERROR: unsupported ctx_retrieve mode={mode!r}; "
                "expected 'get', 'search', 'stats', or 'query'"
            )

        self.metrics.ctx_retrievals[metric_verb] = self.metrics.ctx_retrievals.get(metric_verb, 0) + 1
        # The handle, pattern, query and flag values are model-supplied: only a call the
        # product classifies as a run-handle store read may run on the host; a path, a
        # non-run handle, a pattern that parses as a flag, or a `q` expression (which can
        # name files) runs inside the sandbox like any other command.
        try:
            cp = self._ctx_store_or_sandbox(cmd_args, self.run_dir)
        except subprocess.TimeoutExpired:
            return "ERROR: ctx_retrieve timed out."
        text = (cp.stdout + (("\n" + cp.stderr) if cp.stderr.strip() else "")).strip()
        return text[:24000] or f"(ctx {metric_verb}: no output, exit {cp.returncode})"

    def execute_part(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
        self.last_call_was_dup = False
        self.metrics.tool_calls[name] = self.metrics.tool_calls.get(name, 0) + 1
        if self.is_sj and str((args or {}).get("state_note") or "").strip():
            self.metrics.state_notes_emitted += 1

        sig = ""
        if self.is_sj:
            dedup_args = args
            if name == "bash":
                norm_cmd = _normalize_bash_cmd_for_dedup(str((args or {}).get("command") or ""))
                dedup_args = {**(args or {}), "command": norm_cmd}
            sig = canonical_tool_sig(name, dedup_args, normalize_meta=True)
            dedup_key = (self.mutation_epoch, sig)
            if name in READONLY_TOOLS and dedup_key in self.seen:
                first_step, prev_resp = self.seen[dedup_key]
                self.metrics.dup_blocked += 1
                self.last_call_was_dup = True
                prev_txt = str((prev_resp.get("response") or {}).get("output", ""))
                m = re.search(r"\brun:([0-9a-f]{6,64})\b", prev_txt)
                handle = (
                    f" Its evidence handle is run:{m.group(1)} (call ctx_retrieve with handle='run:{m.group(1)}')."
                    if m
                    else ""
                )
                msg = (
                    f"[straitjacket] Identical call already ran at step {first_step} with no intervening "
                    f"state changes; its result is already in context.{handle} Do not repeat it."
                )
                fr = {"functionResponse": {"name": name, "response": {"output": msg}}}
                return fr, len(msg), msg[:300]

        fs_before = (
            self._workspace_fs_fingerprint()
            if (self.is_sj and name == "bash")
            else None
        )
        fr, n_chars, head = self._dispatch(name, args)

        if self.is_sj:
            out_txt = str(((fr.get("functionResponse") or {}).get("response") or {}).get("output", ""))
            entry_gated = out_txt.startswith("[ctx-entry-gate]")
            is_mutating = False
            if entry_gated:
                is_mutating = False
            elif name in ("write", "edit") or name in MUTATING_BROWSER_TOOLS:
                is_mutating = True
            elif name == "bash":
                cmd = str((args or {}).get("command") or "")
                self._track_untitled_creations(out_txt)
                self._track_external_service_activity(cmd, out_txt)
                fs_after = self._workspace_fs_fingerprint()
                if (
                    (fs_before is not None and fs_after != fs_before)
                    or bool(extract_mutation_receipts(out_txt))
                    or bool(MUTATING_SHELL_RE.search(cmd))
                ):
                    is_mutating = True
            elif name not in ("bash", "ctx_retrieve"):
                self._cli_loop_bin = ""
                self._cli_loop_streak = 0

            if is_mutating:
                self.mutation_epoch += 1
            elif name in READONLY_TOOLS and not self.last_call_was_dup and not entry_gated:
                self.seen[(self.mutation_epoch, sig)] = (self.step, fr["functionResponse"])

        return fr, n_chars, head

    def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
        if name == "read":
            return self._do_read(args)
        if name == "browser_screenshot" and self.browser is not None:
            return self._do_screenshot(args)
        try:
            out = self._do_text_tool(name, args)
        except Exception as e:
            out = f"ERROR in {name}: {e}"
        fr = {"functionResponse": {"name": name, "response": {"output": out}}}
        return fr, len(out), out[:350]

    def _do_screenshot(self, args: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
        assert self.browser is not None
        sp = self.norm(str(args["savePath"])) if args.get("savePath") else None
        try:
            data, mime = self.browser.screenshot(args.get("targetId"), str(args.get("format") or "png"), sp)
        except Exception as e:
            err = f"ERROR taking screenshot: {e}"
            return {"functionResponse": {"name": "browser_screenshot", "response": {"output": err}}}, len(err), err
        b64 = base64.b64encode(data).decode("ascii")
        msg = f"Captured screenshot ({mime}, {len(data):,} bytes)" + (
            f" saved to {args.get('savePath')}" if sp else ""
        )
        fr = {
            "functionResponse": {
                "name": "browser_screenshot",
                "response": {"output": msg},
                "parts": [{"inlineData": {"mimeType": mime, "data": b64}}],
            }
        }
        return fr, len(msg) + (len(data) // 3), msg

    def _do_read(self, args: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
        raw = str(args.get("file_path") or args.get("path") or "")
        if raw.strip() in ("/task/task.md", "task.md"):
            tp = self.meta_dir / "mock_case" / "task.md"
            if not tp.exists():
                nested = sorted((self.meta_dir / "mock_case").glob("**/task.md"))
                tp = nested[0] if nested else (self.run_dir / "task.md")
            if tp.exists():
                txt = rewrite_ports_surgical(
                    tp.read_text(encoding="utf-8"),
                    self.compiled_ports,
                    is_service_script=False,
                )
                lines = txt.splitlines()
                out = format_numbered_lines(lines, 0, len(lines))[: self.config.baseline_raw_cap]
                return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]

        p = self.norm(raw)
        if self._is_blocked_host_path(p) or not p.exists() or not p.is_file():
            err = f"ERROR: File not found: {raw}"
            return {"functionResponse": {"name": "read", "response": {"output": err}}}, len(err), err

        ext = p.suffix.lower()
        if ext in IMAGE_EXTS:
            data = p.read_bytes()
            mime = mimetypes.guess_type(str(p))[0] or f"image/{ext.lstrip('.')}"
            if mime == "image/jpg":
                mime = "image/jpeg"
            b64 = base64.b64encode(data).decode("ascii")
            msg = f"Loaded image {raw} ({mime}, {len(data):,} bytes)."
            fr = {
                "functionResponse": {
                    "name": "read",
                    "response": {"output": msg},
                    "parts": [{"inlineData": {"mimeType": mime, "data": b64}}],
                }
            }
            return fr, len(msg) + (len(data) // 3), msg

        rkey: tuple[str, str, int, int, str] | None = None
        if self.is_sj:
            try:
                file_sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                rkey = (
                    str(p),
                    str(args.get("pages") or ""),
                    int(args.get("offset") or 0),
                    int(args.get("limit") or -1),
                    file_sha,
                )
                if rkey in self.file_reads:
                    prev_step, prev_handle = self.file_reads[rkey]
                    self.metrics.dup_blocked += 1
                    self.metrics.file_dedup_hits += 1
                    self.last_call_was_dup = True
                    if prev_handle:
                        msg = (
                            f"[straitjacket] Unchanged file {raw} (sha256:{file_sha[:8]}) was already captured at step "
                            f"{prev_step} as {prev_handle}; call ctx_retrieve(handle={prev_handle!r}, mode='get', lines='1:120') "
                            "or mode='search' instead of re-reading."
                        )
                    else:
                        msg = (
                            f"[straitjacket] Unchanged file slice {raw} (sha256:{file_sha[:8]}) was already read in full "
                            f"at step {prev_step} and remains verbatim in context above; do not re-read unchanged files."
                        )
                    return {"functionResponse": {"name": "read", "response": {"output": msg}}}, len(msg), msg[:300]
            except Exception:
                rkey = None

        if ext == ".pdf":
            pages_spec = args.get("pages")
            extracted, note = extract_pdf_text(p, str(pages_spec) if pages_spec is not None else None)
            if extracted.startswith("ERROR:"):
                return (
                    {"functionResponse": {"name": "read", "response": {"output": extracted}}},
                    len(extracted),
                    extracted,
                )
            all_lines = extracted.splitlines()
            off = max(0, int(args.get("offset") or 0))
            lim = int(args.get("limit") or len(all_lines))
            sel_lines = all_lines[off : off + lim]
            formatted = format_numbered_lines(sel_lines, off, len(all_lines), note)
            raw_bytes = len(formatted.encode("utf-8"))
            if self.is_sj and raw_bytes > self.config.entry_gate_bytes:
                tmp_txt = self.ctx_extract_dir / f"pdf_s{self.step}_{p.stem}.txt"
                tmp_txt.write_text(formatted, encoding="utf-8")
                out = self.ctx_run(f"cat {shlex.quote(str(tmp_txt))}", raw_bytes_hint=raw_bytes)
                if rkey is not None:
                    m = re.search(r"\brun:([0-9a-f]{6,64})\b", out)
                    self.file_reads[rkey] = (self.step, f"run:{m.group(1)}" if m else "")
                return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]
            self.metrics.inline_delivered += 1
            out = formatted[: self.config.baseline_raw_cap]
            if rkey is not None:
                self.file_reads[rkey] = (self.step, "")
            return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]

        content = p.read_text(encoding="utf-8", errors="replace")
        all_lines = content.splitlines()
        off = max(0, int(args.get("offset") or 0))
        lim = int(args.get("limit") or max(1, len(all_lines)))
        sel_lines = all_lines[off : off + lim]
        raw_slice = "\n".join(sel_lines)
        raw_bytes = len(raw_slice.encode("utf-8"))
        if self.is_sj and raw_bytes > self.config.entry_gate_bytes:
            end_ln = off + len(sel_lines)
            sed_target = (
                f"/task/{p.relative_to(self.run_dir).as_posix()}"
                if (self._use_bwrap and p.is_relative_to(self.run_dir))
                else str(p)
            )
            out = self.ctx_run(
                f"sed -n '{off + 1},{max(off + 1, end_ln)}p' {shlex.quote(sed_target)}",
                raw_bytes_hint=raw_bytes,
            )
            if rkey is not None:
                m = re.search(r"\brun:([0-9a-f]{6,64})\b", out)
                self.file_reads[rkey] = (self.step, f"run:{m.group(1)}" if m else "")
            return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]

        self.metrics.inline_delivered += 1
        formatted = format_numbered_lines(sel_lines, off, len(all_lines))
        out = formatted[: self.config.baseline_raw_cap]
        if self.is_sj:
            out = self._apply_structured_shape(out, raw_for_detection=raw_slice)
        if rkey is not None:
            self.file_reads[rkey] = (self.step, "")
        return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]

    def _do_text_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "ctx_retrieve":
            return self._do_ctx_retrieve(args)

        if name == "bash":
            cmd = str(args.get("command") or "")
            stripped = cmd.strip()
            wd = self.norm(str(args["workdir"])) if args.get("workdir") else self.run_dir
            if self._is_blocked_host_path(wd) or not wd.is_dir():
                wd = self.run_dir
            if (
                self.is_sj
                and (stripped.startswith("ctx ") or stripped == "ctx")
                and not any(op in stripped for op in ("&&", "||", ";", "\n", "|", ">"))
            ):
                return self._route_ctx_command(stripped, wd)
            cmd_rw = self.rewrite_guest(cmd)
            if not self.is_sj:
                return self._raw_shell_run(cmd_rw, cwd_override=wd)

            gated = self._check_bash_entry_gate(cmd_rw)
            if gated is not None:
                return gated

            out = self.ctx_run(cmd_rw, cwd_override=wd)
            out = self._maybe_enrich_cli_help(cmd_rw, out)
            if not out.startswith(("[exit ", "[ctx run:", "[ctx-guard:", "ERROR:")):
                out = self._apply_structured_shape(out)
                sec_rcpt = _format_section_receipt(out)
                if sec_rcpt:
                    out = out + "\n\n" + sec_rcpt
            out = self._maybe_append_cli_subcommand_ref(cmd_rw, out)
            out = self._append_cli_subprocess_error(cmd_rw, out)
            out = self._track_cli_loop_streak(cmd_rw, out)
            if (
                out.startswith("[exit ")
                and _INLINE_PY_C_RE.search(cmd)
                and _PY_PARSE_ERR_RE.search(out)
            ):
                out += (
                    "\n[ctx-hint: bash quote/variable expansion broke inline python -c; "
                    "use a single-quoted heredoc (`python3 - <<'PY' ... PY`) or write a .py file]"
                )
            if re.search(
                r"(?:\bHTTP[/ ]1\.[01]\s+4\d\d\b|\"status\"\s*:\s*4\d\d|\b400 Bad Request\b|\bValidationError\b|validation failed)",
                out,
                re.I,
            ):
                self.http400_streak += 1
                if self.http400_streak >= 2 and self.http400_hints < 2:
                    out += "\n[ctx-hint: repeated validation/4xx error — inspect exact required schema/fields in the API docs or --help before retrying]"
                    self.http400_hints += 1
                    self.http400_streak = 0
            else:
                self.http400_streak = 0
            return out

        if name == "write":
            raw = str(args.get("file_path") or args.get("path") or "")
            p = self.norm(raw)
            if self._is_blocked_host_path(p):
                return f"ERROR: Path not found: {raw}"
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(args.get("content") or "")
            out_root = (self.run_dir / "outputs").resolve()
            if p.suffix.lower() in {".py", ".sh", ".bash", ".js", ".mjs"} and not p.is_relative_to(out_root):
                content = self.rewrite_guest(content)
            p.write_text(content, encoding="utf-8")
            msg = f"Successfully wrote {len(content)} characters to {raw}"
            if self.is_sj:
                msg += f"\n{_format_write_receipt(p, content)}"
            return msg

        if name == "edit":
            raw = str(args.get("file_path") or args.get("path") or "")
            p = self.norm(raw)
            if self._is_blocked_host_path(p) or not p.is_file():
                return f"ERROR: File not found: {raw}"
            out_root = (self.run_dir / "outputs").resolve()
            is_helper_script = p.suffix.lower() in {".py", ".sh", ".bash", ".js", ".mjs"} and not p.is_relative_to(
                out_root
            )
            old_s = str(args.get("old_string") or "")
            new_s = str(args.get("new_string") or "")
            if is_helper_script:
                old_s = self.rewrite_guest(old_s)
                new_s = self.rewrite_guest(new_s)
            txt = p.read_text(encoding="utf-8")
            if old_s not in txt:
                return f"ERROR: old_string not found in {raw}"
            updated_txt = txt.replace(old_s, new_s, 1)
            p.write_text(updated_txt, encoding="utf-8")
            msg = f"Updated {raw}"
            if self.is_sj:
                msg += f"\n{_format_write_receipt(p, updated_txt)}"
            return msg

        if name == "list":
            raw_p = str(args.get("path") or ".")
            p = self.norm(raw_p)
            if self._is_blocked_host_path(p) or not p.exists():
                return f"ERROR: Path not found: {raw_p}"
            if p.is_file():
                return raw_p
            show_hid = bool(args.get("show_hidden", False))
            entries = sorted(e for e in os.listdir(p) if show_hid or not e.startswith("."))
            listing = (
                "\n".join(("[dir] " if (p / e).is_dir() else "[file] ") + e for e in entries[:300])
                or "(empty directory)"
            )
            return listing

        if name == "glob":
            base = self.norm(str(args.get("path") or "."))
            if self._is_blocked_host_path(base) or not base.exists():
                return "(no matches)"
            pattern = str(args.get("pattern") or "*")
            hits = sorted(pyglob.glob(str(base / pattern), recursive=True))
            rel = [
                str(Path(h).resolve())
                .replace(str(self.run_dir), "/task")
                .replace(str(self.sandbox_tmp), "/tmp")
                for h in hits
                if not self._is_blocked_host_path(Path(h))
            ]
            return "\n".join(rel[:300]) or "(no matches)"

        if name == "grep":
            target = self.norm(str(args.get("path") or "/task/workspace"))
            if self._is_blocked_host_path(target) or not target.exists():
                return "(no matches)"
            pat = str(args.get("pattern") or "")
            flags = re.I if args.get("ignore_case") else 0
            rx = re.compile(pat, flags)
            files = [target] if target.is_file() else sorted(f for f in target.rglob("*") if f.is_file())
            matches: list[str] = []
            for f in files[:250]:
                if self._is_blocked_host_path(f):
                    continue
                if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".db", ".sqlite"}:
                    continue
                try:
                    for idx, ln in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(ln):
                            rel_f = (
                                str(f.resolve())
                                .replace(str(self.run_dir), "/task")
                                .replace(str(self.sandbox_tmp), "/tmp")
                            )
                            matches.append(f"{rel_f}:{idx}: {ln[:220]}")
                            if len(matches) >= 200:
                                break
                except Exception:
                    continue
                if len(matches) >= 200:
                    break
            return self.capture_large_text("grep", "\n".join(matches) or "(no matches)")

        if name == "present_files":
            files = args.get("files") or []
            paths = [f.get("path") if isinstance(f, dict) else str(f) for f in files]
            if self.is_sj and not self.emission_gated_once:
                audit = audit_deliverables(
                    self.run_dir,
                    [str(p or "") for p in paths],
                    declared_paths=self.declared_deliverables,
                    task_markdown=self.task_markdown,
                )
                ext_adv = self.external_state_advisories()
                if audit.should_gate or ext_adv:
                    self.emission_gated_once = True
                    self.metrics.emission_gate_hits += 1
                    msg = audit.format_gate_message()
                    if ext_adv:
                        if not audit.should_gate:
                            msg = "[ctx-emission-gate] Pre-finalization verification check found items to confirm:"
                        for ea in ext_adv:
                            msg += f"\n- [CHECK] {ea}"
                    return msg
            self.completed = True
            return "Deliverables presented: " + ", ".join(str(x) for x in paths)

        if self.browser is not None:
            tid = args.get("targetId")
            if name == "browser_open":
                raw_snap = self.browser.open_tab(self._rewrite_browser_url(str(args.get("url") or "")))
                return self.capture_large_text("bsnap", raw_snap)
            if name == "browser_navigate":
                raw_snap = self.browser.navigate(tid, self._rewrite_browser_url(str(args.get("url") or "")))
                return self.capture_large_text("bsnap", raw_snap)
            if name == "browser_snapshot":
                raw_snap = self.browser.snapshot(tid)
                return self.capture_large_text("bsnap", raw_snap)
            if name == "browser_click":
                raw_snap = self.browser.click(tid, str(args.get("ref") or ""), str(args.get("button") or "left"))
                return self.capture_large_text("bsnap", raw_snap)
            if name == "browser_batch":
                raw_batch = self.browser.batch(
                    tid,
                    args.get("steps") or [],
                    bool(args.get("stopOnNavigation", False)),
                    stop_on_error=bool(args.get("stopOnError", True)),
                    include_step_text=bool(args.get("include_step_text", True)),
                )
                return self.capture_large_text("bbatch", raw_batch)
            if name == "browser_fill":
                return self.browser.fill_fields(tid, args.get("fields") or [])
            if name == "browser_type":
                raw_type = self.browser.type_text(
                    tid,
                    str(args.get("ref") or ""),
                    str(args.get("text") or ""),
                    bool(args.get("clearFirst", True)),
                    bool(args.get("pressEnter", False)),
                )
                return self.capture_large_text("bsnap", raw_type)
            if name == "browser_act":
                raw_act = self.browser.act(
                    tid,
                    str(args.get("kind") or ""),
                    args.get("ref"),
                    args.get("values"),
                    args.get("fn"),
                )
                return self.capture_large_text("bact", raw_act)
            if name == "browser_console":
                expr = str(args.get("expression") or "")
                if args.get("scriptPath"):
                    sp = self.norm(str(args["scriptPath"]))
                    if sp.is_file():
                        expr = sp.read_text(encoding="utf-8")
                save_p = self.norm(str(args["savePath"])) if args.get("savePath") else None
                raw_con = self.browser.console(tid, expr, save_p)
                return self.capture_large_text("bcon", raw_con)
            if name == "browser_utility":
                paths = [str(self.norm(str(x))) for x in (args.get("paths") or [])]
                return self.browser.utility(
                    tid,
                    str(args.get("kind") or ""),
                    paths,
                    args.get("element"),
                    args.get("operation"),
                )
            if name == "browser_wait":
                return self.browser.wait(
                    tid,
                    args.get("condition"),
                    args.get("timeMs"),
                    args.get("loadState"),
                    args.get("url"),
                )
            if name == "browser_press":
                return self.browser.press(tid, str(args.get("key") or "Enter"))
            if name == "browser_scroll":
                return self.browser.scroll(tid, str(args.get("direction") or "down"))
            if name == "browser_info":
                return self.browser.info()

        if name == "web_fetch":
            chunks: list[str] = []
            for u in (args.get("urls") or [])[:6]:
                fu = rewrite_ports_surgical(str(u), self.compiled_ports, is_service_script=False)
                try:
                    req = urllib.request.Request(fu, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        body = r.read(300_000).decode("utf-8", errors="replace")
                        chunks.append(f"=== {u} (HTTP {r.status}) ===\n{body[:40000]}")
                except Exception as e:
                    chunks.append(f"=== {u} ===\nERROR: {e}")
            return self.capture_large_text("wfetch", "\n\n".join(chunks))

        if name == "web_search":
            q = str(args.get("query") or "")
            ddg = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
            try:
                req = urllib.request.Request(ddg, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    html_txt = r.read(200_000).decode("utf-8", errors="replace")
                titles = re.findall(r'class="result__a"[^>]* href="([^"]+)"[^>]*>(.*?)</a>', html_txt, re.S)
                snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', html_txt, re.S)
                items = []
                for idx, (href, tit) in enumerate(titles[:10], 1):
                    clean_t = re.sub(r"<[^>]+>", "", htmlmod.unescape(tit)).strip()
                    clean_s = (
                        re.sub(r"<[^>]+>", "", htmlmod.unescape(snips[idx - 1])).strip()
                        if idx - 1 < len(snips)
                        else ""
                    )
                    items.append(f"{idx}. {clean_t}\n   URL: {href}\n   {clean_s}")
                return ("\n\n".join(items) or "(no search results returned)")[: self.config.baseline_raw_cap]
            except Exception as e:
                return f"Search error: {e}"

        return f"ERROR: Unknown tool {name}"
