"""capped/v1: the digest of a capture that passed ``[store] max_capture_bytes``.

A capped stream is stored as its head and its tail with the middle dropped
(:mod:`ctx.capture`). The structured profiles number lines by their position
in what they parse, which for a capped stream is not the original line
number past the head, and their counts would describe the stored part as if
it were the whole. So a capped capture gets this one rendering instead: the
exact counts, what was kept and what was not, the first and last lines of
each stream under their real line numbers, and the retrieval verbs that
work on the stored part. Nothing here claims coverage of the dropped middle.
"""

from __future__ import annotations

from ctx.capture import RUNAWAY_FACTOR, CapMap, command_cap
from ctx.digest.base import DigestContext, Profile
from ctx.textutil import (
    EVIDENCE_LINE_CHARS,
    clip_redacting,
    fmt_bytes,
    fmt_int,
    index_lines,
)

#: Bytes read from the end of a stored stream for its last lines.
_TAIL_READ = 64 * 1024


def capped_streams(manifest: dict) -> dict[str, CapMap]:
    out = {}
    for name in ("stdout", "stderr"):
        cm = CapMap.of((manifest.get("streams") or {}).get(name))
        if cm is not None:
            out[name] = cm
    return out


class CappedProfile(Profile):
    version = "capped/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        return "capture capped" if capped_streams(ctx.manifest) else None

    def _tail_lines(self, ctx: DigestContext, name: str, n: int) -> list[str]:
        meta = ctx.manifest["streams"][name]
        store = ctx.store
        if store is None or n <= 0:
            return []
        blob = str(meta["blob"]).removeprefix("sha256:")
        path = store.blob_path(store.resolve_id(blob, kinds=("blob",)))
        size = int(meta["bytes"])
        with path.open("rb") as fh:
            fh.seek(max(0, size - _TAIL_READ))
            data = fh.read()
        lines = index_lines(data.decode("utf-8", "replace"))
        if size > _TAIL_READ and lines:
            lines = lines[1:]  # the first may be a fragment of a longer line
        return lines[-n:]

    def render(self, ctx: DigestContext) -> str:
        m = ctx.manifest
        r = m["result"]
        caps = capped_streams(m)
        status = (f"exit {r['exitCode']}" if r["exitCode"] is not None
                  else f"signal {r['signal']}")
        if r.get("timedOut"):
            status += " · timed out"
        cap_total = sum(int(s.get("capture", {}).get("keptBytes", s["bytes"]))
                        for s in m["streams"].values())
        all_total = sum(int(s.get("capture", {}).get("totalBytes", s["bytes"]))
                        for s in m["streams"].values())
        out = [f"cwd: {m['cwd']}", f"command: {ctx.command_line()}",
               f"status: {status}" if not status.startswith("exit ")
               else f"exit: {status.removeprefix('exit ')}"]
        if r.get("outputLimited"):
            limit = command_cap(ctx.ws.config.store.max_capture_bytes) * RUNAWAY_FACTOR
            out.append("stopped: output passed the runaway limit "
                       f"({fmt_int(limit)} bytes, {RUNAWAY_FACTOR} x [store] "
                       "max_capture_bytes); its process group was killed")
        out.append(f"capture: capped · captured {fmt_int(cap_total)} of "
                   f"{fmt_int(all_total)} bytes "
                   f"({fmt_bytes(cap_total)} of {fmt_bytes(all_total)})")
        head_n = max(1, int(ctx.ws.config.budgets.digest_head_lines))
        tail_n = max(1, int(ctx.ws.config.budgets.digest_tail_lines))
        for view in (ctx.stdout, ctx.stderr):
            cm = caps.get(view.name)
            if cm is None:
                out.append(f"{view.name}: {fmt_int(view.lines)} lines · "
                           f"{fmt_int(view.bytes)} bytes · complete")
                continue
            kept = cm.head_bytes + cm.tail_bytes
            out.append(f"{view.name}: captured {fmt_int(kept)} of {fmt_int(cm.total_bytes)} "
                       f"bytes · {fmt_int(cm.total_lines)} lines in all")
            out.append(f"  kept: {cm.kept_text()} (first {fmt_int(cm.head_bytes)} and "
                       f"last {fmt_int(cm.tail_bytes)} bytes)")
            out.append(f"  not stored: {cm.gap_text()}")
        for view in (ctx.stdout, ctx.stderr):
            if not view.bytes:
                continue
            if view.media_type.startswith("application/octet-stream"):
                out.append(f"{view.name}: binary ({view.media_type}); "
                           f"read it with ctx get run:PENDING#{view.name} --bytes A:B")
                continue
            cm = caps.get(view.name)
            lines = view.text_lines
            if cm is None:
                shown = lines[:head_n]
                out.append(f"first lines ({view.name}):")
                out.extend(f"  L{i}: {clip_redacting(t, EVIDENCE_LINE_CHARS)}"
                           for i, t in enumerate(shown, start=1))
                continue
            head = lines[:min(head_n, cm.head_lines)]
            out.append(f"first lines ({view.name}):")
            out.extend(f"  L{i}: {clip_redacting(t, EVIDENCE_LINE_CHARS)}"
                       for i, t in enumerate(head, start=1))
            out.append(f"  … not stored: {cm.gap_text()} …")
            tail = self._tail_lines(ctx, view.name, min(tail_n, cm.tail_lines))
            if tail:
                first = cm.last_line - len(tail) + 1
                out.append(f"last lines ({view.name}):")
                out.extend(f"  L{first + i}: {clip_redacting(t, EVIDENCE_LINE_CHARS)}"
                           for i, t in enumerate(tail))
        out.append("coverage:")
        out.append("  summarized: the stored head and tail only; the dropped middle "
                   "was not stored, searched or summarized")
        out.append("  line numbers: the original stream's, in this digest and in "
                   "ctx get/search on this run")
        name = next(iter(caps))
        cm = caps[name]
        sug = []
        if cm.tail_lines:
            a = max(cm.tail_first, cm.last_line - 39)
            sug.append(f"ctx get run:PENDING#{name} --lines {a}:{cm.last_line}")
        sug.append("ctx search run:PENDING '<pattern>' --context 3")
        out.extend(self.next_lines(ctx, sug))
        return "\n".join(out)
