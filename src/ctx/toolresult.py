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


def render_command_result(ws, store, capture, *, focus=None, delivery="auto") -> CommandResult:
    """Complete small text streams inline; otherwise use the profile/resolver.

    Check serialized size after sanitization: control characters and escaping
    can make even a small raw stream expensive. Stream identity is retained in
    JSON, including empty output and output without a final newline.
    """
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
    raw = sum(s["bytes"] for s in streams.values())
    inline = delivery == "auto" and raw <= budgets.max_inline_bytes and (
        sum(s["lines"] for s in streams.values()) <= budgets.max_inline_lines
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
        # Include the envelope itself in the result budget. The provisional and
        # final refs have the same length, so publication cannot grow it.
        if len(candidate.to_json().encode()) <= budgets.result_tokens * 4:
            payload = json.dumps(views, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            mid, _ = update_manifest_digest(store, manifest, {
                "profile": "inline/v1", "policy": POLICY_VERSION,
                "focusHash": focus_hash(focus),
                "bytesHash": "sha256:" + hashlib.sha256(payload.encode()).hexdigest()})
            return CommandResult(**{**values, "ref": "run:" + mid}, delivery="inline",
                **views, redactions=candidate.redactions)

    from ctx.digest import render_run_digest
    from ctx.commands.emit import _delivery_plan, render_bounded_digest
    outcome = "success" if status.get("exitCode") == 0 else "failure"
    plan = _delivery_plan(ws, outcome=outcome, family="run", base_tokens=budgets.digest_tokens)
    digest, final = render_run_digest(store, ws, manifest, focus=focus, plan=plan, record_delivery=False)
    digest = render_bounded_digest(ws, store, digest, plan)
    result = CommandResult(**{**values, "ref": "run:" + final["id"].removeprefix("sha256:")},
        delivery="digest", digest=digest)
    return _fit_wire(result, max(512, budgets.result_tokens * 4))


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
