"""Provider-independent command result contract and delivery selection.

Capture, profile extraction and emission policy remain the existing mechanisms.
The public result carries command termination explicitly, so integrations never
infer it from ctx's process exit or from a human-facing header.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import csv
import hashlib
import io
import json
import re
from typing import Any

from ctx._proc import native_exit_status
from ctx.execution import update_manifest_digest, focus_hash
from ctx.textutil import sanitize_for_model
from ctx import POLICY_VERSION

_DATA_SHAPE_MIN_CHARS = 1500
_DATA_SHAPE_MIN_JSON_ROWS = 3
_DATA_SHAPE_MIN_CSV_ROWS = 5
_DATA_SHAPE_MAX_FIELDS = 16
_ERROR_TAIL_LINES = 25
_DEFAULT_COMPRESS_CHARS = 4000
_NUMBERED_LINE_PREFIX_RE = re.compile(r"^\d{5}\|\s?")
_NUMBERED_TOTAL_FOOTER_RE = re.compile(r"\n*\(Total:\s*\d+\s*lines[^)]*\)\s*$")
_PLAIN_NUMERIC_CELL_RE = re.compile(
    r"^[+-]?\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$"
)
_ERROR_SIGNAL_RE = re.compile(
    r"(?:Traceback \(most recent call last\):|\b[A-Za-z_]*Error:|\b[A-Za-z_]*Exception:|"
    r"\[exit_code:\s*(?!0\b)-?\d+\]|\[exit\s+(?!0\b)-?\d+\]|\bexit=(?!0\b)-?\d+\b)",
    re.I,
)
_OMISSION_MARKER_RE = re.compile(
    r"(?:\[ctx:omitted|\blines omitted\b|\brecords omitted\b|\[truncated|"
    r"\.\.\.\s*(?:middle-truncated|omitted|truncated)|\[ctx-retrieve\]|\bnext:\s*ctx\s+get\b)",
    re.I,
)


def _strip_numbered_line_decorations(text: str) -> str:
    cleaned = _NUMBERED_TOTAL_FOOTER_RE.sub("", text)
    lines = cleaned.splitlines()
    if lines and _NUMBERED_LINE_PREFIX_RE.match(lines[0]):
        lines = [_NUMBERED_LINE_PREFIX_RE.sub("", ln) for ln in lines]
    return "\n".join(lines).strip()


def _is_numeric_scalar(val: Any) -> bool:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return True
    if isinstance(val, str):
        s = val.strip()
        if s and _PLAIN_NUMERIC_CELL_RE.match(s):
            return True
    return False


def _extract_json_dict_rows(parsed: Any) -> list[dict[str, Any]] | None:
    if isinstance(parsed, list):
        if len(parsed) >= _DATA_SHAPE_MIN_JSON_ROWS and all(isinstance(x, dict) for x in parsed):
            return parsed
        return None
    if isinstance(parsed, dict):
        best: list[dict[str, Any]] | None = None
        for v in parsed.values():
            if isinstance(v, list) and len(v) >= _DATA_SHAPE_MIN_JSON_ROWS and all(isinstance(x, dict) for x in v):
                if best is None or len(v) > len(best):
                    best = v
        return best
    return None


def summarize_data_shape_header(text: str) -> str:
    """Return a 2-line ``[data-shape]`` + ``[compute-hint]`` header for structured JSON/CSV outputs >= 1,500 chars."""
    if not text or len(text) < _DATA_SHAPE_MIN_CHARS:
        return ""
    raw = text.strip()
    if raw.startswith("[data-shape:"):
        parts = raw.split("\n", 2)
        raw = parts[-1].strip() if len(parts) >= 3 else ""
        if len(raw) < _DATA_SHAPE_MIN_CHARS:
            return ""

    candidate = _strip_numbered_line_decorations(raw)
    if not candidate:
        return ""

    # 1. Try JSON array / wrapped object array / JSONL.
    records: list[dict[str, Any]] | None = None
    if candidate.startswith(("[", "{")):
        try:
            parsed = json.loads(candidate)
            records = _extract_json_dict_rows(parsed)
        except Exception:
            records = None
        if records is None and candidate.startswith("{"):
            non_empty_lns = [ln.strip() for ln in candidate.splitlines() if ln.strip()]
            if len(non_empty_lns) >= _DATA_SHAPE_MIN_JSON_ROWS and all(
                ln.startswith("{") and ln.endswith("}") for ln in non_empty_lns
            ):
                jsonl_rows: list[dict[str, Any]] = []
                ok_jsonl = True
                for ln in non_empty_lns:
                    try:
                        obj = json.loads(ln)
                    except Exception:
                        ok_jsonl = False
                        break
                    if not isinstance(obj, dict):
                        ok_jsonl = False
                        break
                    jsonl_rows.append(obj)
                if ok_jsonl and len(jsonl_rows) >= _DATA_SHAPE_MIN_JSON_ROWS:
                    records = jsonl_rows

    if records is not None and len(records) >= _DATA_SHAPE_MIN_JSON_ROWS:
        rows_cnt = len(records)
        fields: list[str] = []
        seen_f: set[str] = set()
        for r in records:
            for k in r:
                ks = str(k)
                if ks not in seen_f:
                    seen_f.add(ks)
                    fields.append(ks)
        shown_fields = fields[:_DATA_SHAPE_MAX_FIELDS]
        numeric_fields: list[str] = []
        for k in shown_fields:
            vals = [
                r.get(k)
                for r in records
                if r.get(k) is not None and not (isinstance(r.get(k), str) and not str(r.get(k)).strip())
            ]
            if vals and sum(1 for v in vals if _is_numeric_scalar(v)) * 5 >= len(vals) * 3:
                numeric_fields.append(k)
        fields_s = ", ".join(shown_fields)
        num_s = ", ".join(numeric_fields)
        return (
            f"[data-shape: format=json, rows={rows_cnt}, fields=[{fields_s}], numeric_fields=[{num_s}]]\n"
            f"[compute-hint: process all {rows_cnt} rows in one python3 script with [section: name] markers; "
            "avoid mental math or multi-turn 1-liners]"
        )

    # 2. Try CSV / TSV (>= 5 rows).
    non_empty = [ln for ln in candidate.splitlines() if ln.strip()]
    if len(non_empty) < _DATA_SHAPE_MIN_CSV_ROWS:
        return ""
    first_ln = non_empty[0]
    delim = "\t" if ("\t" in first_ln and first_ln.count("\t") >= first_ln.count(",")) else ","
    if delim not in first_ln:
        return ""
    try:
        csv_rows = [r for r in csv.reader(io.StringIO("\n".join(non_empty)), delimiter=delim) if any(c.strip() for c in r)]
    except Exception:
        return ""
    if len(csv_rows) < _DATA_SHAPE_MIN_CSV_ROWS:
        return ""
    header = [c.strip() for c in csv_rows[0]]
    ncols = len(header)
    if ncols < 2 or any(len(h) > 64 or "\n" in h for h in header if h):
        return ""
    consistent = sum(1 for r in csv_rows[1:] if len(r) == ncols)
    data_rows = csv_rows[1:]
    if not data_rows or consistent * 5 < len(data_rows) * 4:
        return ""

    shown_cols = [h for h in header[:_DATA_SHAPE_MAX_FIELDS] if h]
    if len(shown_cols) < 2:
        return ""
    numeric_cols: list[str] = []
    for ci, col_name in enumerate(header[:_DATA_SHAPE_MAX_FIELDS]):
        if not col_name:
            continue
        cells = [r[ci].strip() for r in data_rows if ci < len(r) and r[ci].strip()]
        if cells and sum(1 for c in cells if _PLAIN_NUMERIC_CELL_RE.match(c)) * 5 >= len(cells) * 3:
            numeric_cols.append(col_name)

    rows_cnt = len(data_rows)
    fields_s = ", ".join(shown_cols)
    num_s = ", ".join(numeric_cols)
    return (
        f"[data-shape: format=csv, rows={rows_cnt}, fields=[{fields_s}], numeric_fields=[{num_s}]]\n"
        f"[compute-hint: process all {rows_cnt} rows in one python3 script with [section: name] markers; "
        "avoid mental math or multi-turn 1-liners]"
    )


def prepend_structured_data_shape(text: str) -> str:
    """Prepend the 2-line ``[data-shape]`` + ``[compute-hint]`` header to ``text`` with 0 character loss."""
    if not text or text.lstrip().startswith("[data-shape:"):
        return text
    header = summarize_data_shape_header(text)
    if not header:
        return text
    return f"{header}\n{text}"


def extract_error_tail_block(text: str, *, exit_code: int | None = None) -> str:
    """Return ``[error-tail-first]`` with the final 25 lines when ``text`` contains an error/traceback."""
    if not text:
        return ""
    has_err = (exit_code is not None and exit_code != 0) or bool(_ERROR_SIGNAL_RE.search(text))
    if not has_err:
        return ""
    lines = [ln for ln in text.splitlines() if not ln.startswith(("[ctx-retrieve]", "next: ctx get"))]
    if not lines:
        return ""
    tail_slice = lines[-_ERROR_TAIL_LINES:]
    return "[error-tail-first]\n" + "\n".join(tail_slice)


def compress_tool_result(
    first: str = "",
    second: str | None = None,
    *,
    output: str | None = None,
    text: str | None = None,
    tool_name: str = "Bash",
    max_chars: int = _DEFAULT_COMPRESS_CHARS,
    exit_code: int | None = None,
    ref: str = "",
) -> str:
    """Bound tool output while prepending ``[error-tail-first]`` (final 25 lines) when a Bash error is truncated."""
    if second is not None:
        eff_tool = str(first or tool_name)
        raw_text = str(second)
    elif output is not None:
        eff_tool = tool_name
        raw_text = str(output)
    elif text is not None:
        eff_tool = tool_name
        raw_text = str(text)
    else:
        eff_tool = tool_name
        raw_text = str(first or "")

    if not raw_text or raw_text.lstrip().startswith("[error-tail-first]"):
        return raw_text

    is_bash_like = eff_tool.strip().lower() in ("bash", "shell", "run", "cmd", "command", "")
    has_err = (exit_code is not None and exit_code != 0) or bool(_ERROR_SIGNAL_RE.search(raw_text))
    already_truncated = bool(_OMISSION_MARKER_RE.search(raw_text))
    needs_truncation = len(raw_text) > max_chars

    if not needs_truncation and not already_truncated:
        return raw_text

    err_header = extract_error_tail_block(raw_text, exit_code=exit_code) if (is_bash_like and has_err) else ""

    if needs_truncation and not already_truncated:
        half = max(256, max_chars // 2)
        head_part = raw_text[:half]
        tail_start = max(half, len(raw_text) - half)
        tail_part = raw_text[tail_start:]
        omitted = len(raw_text) - len(head_part) - len(tail_part)
        ref_suffix = f" (full output in {ref})" if ref else ""
        body = f"{head_part}\n[... {omitted} chars middle-truncated{ref_suffix} ...]\n{tail_part}"
    else:
        body = raw_text

    if err_header:
        return f"{err_header}\n{body}"
    return body


@dataclass(frozen=True)
class CommandResult:
    ref: str
    command_exit_code: int | None
    signal: str | None
    timed_out: bool
    cancelled: bool
    delivery: str
    stdout: str = ""
    stderr: str = ""
    digest: str = ""
    redactions: tuple[str, ...] = ()
    output_limited: bool = False

    @property
    def native_exit_code(self) -> int:
        return native_exit_status({"exitCode": self.command_exit_code,
            "signal": self.signal, "timedOut": self.timed_out})

    def as_dict(self) -> dict:
        value = {"schema": "ctx.command-result/v1", "ref": self.ref,
            "command_exit_code": self.command_exit_code, "signal": self.signal,
            "timed_out": self.timed_out, "cancelled": self.cancelled,
            "delivery": self.delivery}
        if self.delivery == "inline":
            value.update(stdout=self.stdout, stderr=self.stderr)
        else:
            value["digest"] = self.digest
        if self.redactions:
            value["redactions"] = list(self.redactions)
        if self.output_limited:
            value["output_limited"] = True
        return value

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value):
        return cls(**{k: (tuple(v) if k == "redactions" else v)
                      for k, v in value.items() if k != "schema"})

    def to_model(self, *, max_bytes=4800) -> str:
        """Compact human/model view; ``to_json`` remains the machine contract.

        Always retain the canonical handle and termination, even if the body
        needs another budget cut. Stream labels prevent stderr becoming stdout.
        """
        flags = [f"exit={self.command_exit_code if self.command_exit_code is not None else 'null'}"]
        if self.signal:
            flags.append("signal=" + self.signal)
        for name in ("timed_out", "cancelled", "output_limited"):
            if getattr(self, name):
                flags.append(name + "=true")
        if self.redactions:
            flags.append("redactions=" + ",".join(self.redactions))
        head = "[ctx " + self.ref + " " + " ".join(flags) + "]\n"
        body = self.digest if self.delivery == "digest" else "\n".join(
            name + ":\n" + value for name, value in (("stdout", self.stdout), ("stderr", self.stderr)) if value)
        return fit_model_view(head, body or "[no output]", self.ref, max_bytes)


def fit_model_view(head: str, body: str, ref: str, max_bytes: int) -> str:
    """Bound UTF-8 delivery, declaring the exact body-byte omission."""
    cap = max(512, max_bytes)
    if len((head + body).encode()) <= cap:
        return head + body
    total = len(body.encode())
    low, high = 0, len(body)
    while low < high:
        n = (low + high + 1) // 2
        notice = f"\n[ctx:omitted {total - len(body[:n].encode())} body bytes]\nnext: ctx get {ref}"
        if len((head + body[:n] + notice).encode()) <= cap:
            low = n
        else:
            high = n - 1
    notice = f"\n[ctx:omitted {total - len(body[:low].encode())} body bytes]\nnext: ctx get {ref}"
    return head + body[:low] + notice


_PROJECTION_PROGS = frozenset({
    "python", "python3", "jq", "awk", "gawk", "pdftotext", "perl", "ruby", "node",
})
_FLOOD_PROGS = frozenset({
    "ps", "top", "env", "printenv", "dmesg", "journalctl", "find",
})
_DEFAULT_INLINE_BYTES = 16384
_DEFAULT_INLINE_LINES = 240
_DEFAULT_RESULT_TOKENS = 1200


def _command_intent(cmd_str: str, _depth: int = 0) -> str:
    """Classify shell command intent: ``'projection'``, ``'flood'``, or ``'standard'``.

    Explicit data projections (``python -c``, ``jq``, ``awk``, ``pdftotext``)
    carry near-certain follow-up retrieval when digested at moderate sizes,
    costing an extra model turn; ambient host/history dumps (``ps``, ``git log``,
    ``find``, ``ls /tmp``) rarely warrant wide inline passthrough.
    Fail-open to ``'standard'``.
    """
    try:
        import os
        import shlex

        if _depth > 4:
            return "standard"
        s = (cmd_str or "").strip()
        if not s:
            return "standard"
        try:
            lead_argv = shlex.split(s)
        except ValueError:
            lead_argv = None
        if lead_argv:
            try:
                from ctx.hook import _unwrap

                unwrapped = _unwrap(lead_argv)
            except Exception:
                unwrapped = lead_argv
            if unwrapped:
                uprog = os.path.basename(unwrapped[0])
                if (
                    uprog in ("bash", "sh", "zsh", "dash")
                    and len(unwrapped) >= 3
                    and unwrapped[1] in ("-c", "-lc")
                ):
                    return _command_intent(unwrapped[2], _depth + 1)
                if unwrapped != lead_argv:
                    s = shlex.join(unwrapped)
        for sep in ("&&", ";"):
            if s.startswith("cd ") and sep in s:
                head_seg, rest_seg = s.split(sep, 1)
                if head_seg.strip().startswith("cd ") and rest_seg.strip():
                    s = rest_seg.strip()
                    break
        if "|" in s:
            tail_seg = s.rsplit("|", 1)[-1].strip()
            try:
                tail_argv = shlex.split(tail_seg)
            except ValueError:
                tail_argv = tail_seg.split()
            if tail_argv:
                tprog = os.path.basename(tail_argv[0])
                if tprog in _PROJECTION_PROGS or tprog in ("head", "tail", "wc"):
                    return "projection"
        try:
            argv = shlex.split(s)
        except ValueError:
            argv = s.split()
        if not argv:
            return "standard"
        prog = os.path.basename(argv[0])
        if prog in ("bash", "sh", "zsh", "dash") and len(argv) >= 3 and argv[1] in ("-c", "-lc"):
            return _command_intent(argv[2], _depth + 1)
        if prog in _PROJECTION_PROGS:
            return "projection"
        if prog in _FLOOD_PROGS:
            return "flood"
        if prog == "git" and len(argv) >= 2 and argv[1] in (
            "log", "diff", "show", "status", "branch", "rev-parse", "remote"
        ):
            return "flood"
        if prog == "ls" and any(
            a == "/tmp" or a.startswith("/tmp/") or a.startswith("..") or "R" in a
            for a in argv[1:]
            if a
        ):
            return "flood"
        return "standard"
    except Exception:
        return "standard"


def _inline_thresholds(ws, cmd_str: str, dense: bool) -> tuple[int, int]:
    """Turn-carriage break-even and command-intent inline thresholds ``(bytes, lines)``.

    Honours explicitly tightened ``ctx.toml`` or ``ToolSession(max_tokens=...)``
    caps without widening them. Fail-open to configured ``(max_inline_bytes,
    max_inline_lines)``.
    """
    from ctx import bounds

    budgets = ws.config.budgets
    base_bytes = bounds.count(
        bounds.explicit(getattr(budgets, "max_inline_bytes", None), _DEFAULT_INLINE_BYTES)
    )
    base_lines = bounds.count(
        bounds.explicit(getattr(budgets, "max_inline_lines", None), _DEFAULT_INLINE_LINES)
    )
    rtok = bounds.count(
        bounds.explicit(getattr(budgets, "result_tokens", None), _DEFAULT_RESULT_TOKENS)
    )
    try:
        from ctx.proxywindow import read_window_doc

        doc = read_window_doc(getattr(ws, "root", None))
        pct = doc.get("window_pct")
        raw_pressure = bounds.explicit(getattr(budgets, "window_pressure_pct", None), 70)
        pressure_pct = float(raw_pressure) if isinstance(raw_pressure, (int, float)) else 70.0
        if isinstance(pct, (int, float)) and not isinstance(pct, bool) and float(pct) >= pressure_pct:
            return min(base_bytes, 8192), min(base_lines, 120)
        raw_c = doc.get("turn_carriage_tokens")
        if raw_c is None:
            raw_c = doc.get("last_input_tokens")
        carriage = int(raw_c) if isinstance(raw_c, (int, float)) and not isinstance(raw_c, bool) else 0
    except Exception:
        carriage = 0

    intent = _command_intent(cmd_str)
    if intent == "flood" and not dense:
        return min(base_bytes, 8192), min(base_lines, 120)

    # Never widen an explicitly tightened caller/config cap.
    if base_bytes < _DEFAULT_INLINE_BYTES or base_lines < _DEFAULT_INLINE_LINES or rtok < _DEFAULT_RESULT_TOKENS:
        return base_bytes, base_lines

    if dense:
        return max(base_bytes * 2, 32768), max(base_lines * 2, 500)
    if intent == "projection":
        eff_bytes = min(32768, max(24576, base_bytes, int(carriage * 1.5)))
        return eff_bytes, max(base_lines, 420)
    if carriage > 0:
        eff_bytes = min(24576, max(base_bytes, int(carriage * 0.8)))
        return eff_bytes, max(base_lines, 280)
    return base_bytes, base_lines


def render_command_result(ws, store, capture, *, focus=None, delivery="auto") -> CommandResult:
    """Complete small/break-even text streams inline; otherwise use the profile/resolver.

    Check serialized size after sanitization: control characters and escaping
    can make even a small raw stream expensive. Stream identity is retained in
    JSON, including empty output and output without a final newline.
    """
    from ctx import bounds

    if delivery not in {"auto", "digest"}:
        raise ValueError("delivery must be auto or digest")
    manifest = capture.manifest
    status = manifest["result"]
    values = dict(ref="run:" + capture.manifest_id,
        command_exit_code=status.get("exitCode"), signal=status.get("signal"),
        timed_out=bool(status.get("timedOut")), cancelled=bool(status.get("cancelled")),
        output_limited=bool(status.get("outputLimited")))
    streams = manifest["streams"]
    budgets = ws.config.budgets

    cmd_str = ""
    sig = None
    dense = False
    try:
        import shlex
        from ctx import reflex

        argv = manifest.get("argv") or []
        cmd_str = str(argv[0]) if manifest.get("shell") and argv else shlex.join([str(a) for a in argv])
        sig = reflex.command_signature(cmd_str)
        if sig:
            dense = reflex.check_command(ws.root, cmd_str) == "densify" or (
                reflex.densify_latched(ws.root, sig)
            )
    except Exception:
        sig, dense = None, False

    eff_bytes, eff_lines = _inline_thresholds(ws, cmd_str, dense)
    raw = sum(s["bytes"] for s in streams.values())
    inline = delivery == "auto" and raw <= eff_bytes and (
        sum(s["lines"] for s in streams.values()) <= eff_lines
    ) and not any(status.get(k) for k in ("timedOut", "cancelled", "outputLimited")) and all(
        not s["bytes"] or s["mediaType"].startswith("text/") or s["mediaType"] == "application/json"
        for s in streams.values())
    if inline:
        views, redactions = {}, set()
        for name in ("stdout", "stderr"):
            data = store.get_blob(streams[name]["blob"].removeprefix("sha256:"))
            views[name], removed = sanitize_for_model(data.decode("utf-8", "replace"), ws.config.redaction)
            redactions.update(removed)
        candidate = CommandResult(**values, delivery="inline", **views, redactions=tuple(sorted(redactions)))
        # Honour explicit tight token caps (e.g. ToolSession(max_tokens=64)); under
        # standard budgets, measure UTF-8 payload against eff_bytes (avoiding the
        # 4,800-byte result_tokens*4 clamp overriding max_inline_bytes=16,384) while
        # still catching pathological control-character expansion on the wire.
        if budgets.result_tokens < _DEFAULT_RESULT_TOKENS:
            fits_wire = len(candidate.to_json().encode()) <= bounds.budget_bytes(budgets.result_tokens)
        else:
            utf8_json = json.dumps(candidate.as_dict(), ensure_ascii=False, separators=(",", ":"))
            fits_wire = (
                len(utf8_json.encode("utf-8")) <= eff_bytes + 1024
                and len(candidate.to_json().encode()) <= eff_bytes * 2 + 1024
            )
        if fits_wire:
            payload = json.dumps(views, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            mid, _ = update_manifest_digest(store, manifest, {
                "profile": "inline/v1", "policy": POLICY_VERSION,
                "focusHash": focus_hash(focus),
                "bytesHash": "sha256:" + hashlib.sha256(payload.encode()).hexdigest()})
            return CommandResult(**{**values, "ref": "run:" + mid}, delivery="inline",
                **views, redactions=candidate.redactions)

    from ctx.digest import render_run_digest
    from ctx.commands.emit import _delivery_plan, render_bounded_digest
    from ctx.textutil import short_id

    outcome = "success" if status.get("exitCode") == 0 else "failure"
    plan = _delivery_plan(ws, outcome=outcome, family="run", base_tokens=budgets.digest_tokens, signature=sig)
    digest, final = render_run_digest(
        store, ws, manifest, focus=focus, dense=dense, plan=plan, record_delivery=False
    )
    try:
        from ctx import engagement, reflex

        if reflex.has_omissions(digest):
            if ws.config.engagement.mode == "auto":
                engagement.note_truncation(ws.root)
            if sig:
                reflex.note_intervention(
                    ws.root, sig, short_id(final.get("id", "")), hints=reflex.count_hints(digest)
                )
        if dense:
            digest = reflex.DENSIFY_HEADER + "\n" + digest
    except Exception:
        pass
    digest = render_bounded_digest(ws, store, digest, plan)
    result = CommandResult(**{**values, "ref": "run:" + final["id"].removeprefix("sha256:")},
        delivery="digest", digest=digest)
    if budgets.result_tokens < _DEFAULT_RESULT_TOKENS:
        wire_cap = max(512, bounds.budget_bytes(budgets.result_tokens))
    else:
        plan_tok = bounds.count(bounds.explicit(getattr(plan, "token_budget", None), budgets.result_tokens))
        wire_cap = max(512, bounds.budget_bytes(max(budgets.result_tokens, plan_tok)))
    return _fit_wire(result, wire_cap)


def _fit_wire(result: CommandResult, max_bytes: int) -> CommandResult:
    """Bound the JSON representation too, with a resolvable omission notice.

    A Unicode-heavy or quoted digest can expand during JSON serialization.
    A 512-byte floor keeps status plus an omission address available even
    under a caller's tiny content budget. No captured stream is truncated.
    """
    if len(result.to_json().encode()) <= max_bytes:
        return result
    notice = "\n[ctx:omitted digest tail]\nnext: ctx get " + result.ref
    low, high = 0, len(result.digest)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = replace(result, digest=result.digest[:middle] + notice)
        if len(candidate.to_json().encode()) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return replace(result, digest=result.digest[:low] + notice)
