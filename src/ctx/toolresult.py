"""Provider-independent command result contract and delivery selection.

Capture, profile extraction and emission policy remain the existing mechanisms.
The public result carries command termination explicitly, so integrations never
infer it from ctx's process exit or from a human-facing header.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json

from ctx._proc import native_exit_status
from ctx.execution import update_manifest_digest, focus_hash
from ctx.textutil import sanitize_for_model
from ctx import POLICY_VERSION


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


def _command_intent(cmd_str: str) -> str:
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

        s = (cmd_str or "").strip()
        if not s:
            return "standard"
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
        if prog in ("bash", "sh", "zsh") and len(argv) >= 3 and argv[1] in ("-c", "-lc"):
            return _command_intent(argv[2])
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
