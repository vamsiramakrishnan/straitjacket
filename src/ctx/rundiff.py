"""Run-to-run regression digest: ``ctx diff run:A run:B`` (ROADMAP M-D).

Deltas are computed structurally — result, stream sizes, pytest failure
signatures, log templates — and every new-in-B claim carries a line
coordinate plus a minted span, so evidence resolves via ``ctx get`` instead
of re-dumping streams (SPEC §6.4). Output is deterministic and bounded by
the result budget.
"""

from __future__ import annotations

import re
from typing import Any

from ctx.digest.logprof import mine_templates
from ctx.refs import RefError, parse_ref
from ctx.retrieval import RetrievalError, _emit, record_telemetry
from ctx.store import Store, StoreError
from ctx.textutil import fmt_bytes, fmt_int, short_id
from ctx.workspace import Workspace

_MINE_CAP = 200_000  # lines template-mined per side
_TOP = 5
_SPAN_LINES = 12  # display budget for a quoted traceback

# pytest-style output signatures (mirrors digest/pytestprof).
_SESSION_RE = re.compile(r"=+ test session starts =+")
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR) (?P<nodeid>\S+)(?: - (?P<msg>.*))?$")
_FAIL_HEADER_RE = re.compile(r"^_{3,} (?P<nodeid>.+?) _{3,}$")


def _manifest(store: Store, ref_text: str) -> dict[str, Any]:
    try:
        ref = parse_ref(ref_text)
    except RefError as e:
        raise RetrievalError(str(e)) from None
    if ref.kind != "run":
        raise RetrievalError(f"diff compares run: references, got {ref_text!r}")
    try:
        manifest = store.get_manifest(ref.id or "")
    except StoreError as e:
        raise RetrievalError(
            f"cannot resolve {ref_text}: {e}; capture runs with 'ctx run -- <command>'"
        ) from None
    if manifest.get("schema") != "ctx.invocation/v1":
        raise RetrievalError(f"{ref_text} is not a captured invocation")
    return manifest


def _stream_text(store: Store, meta: dict[str, Any] | None) -> str | None:
    """Decoded stream text; '' for empty, None for binary (analysis skipped)."""
    if not meta or not meta.get("bytes"):
        return ""
    if str(meta.get("mediaType", "")).startswith("application/octet-stream"):
        return None
    data = store.get_blob(str(meta["blob"]).removeprefix("sha256:"))
    return data.decode("utf-8", "replace")


def _pytest_failures(text: str) -> dict[str, int] | None:
    """nodeid -> line number of its FAILED/ERROR summary line; None when the
    text does not parse as pytest-style output."""
    failures: dict[str, int] = {}
    for i, ln in enumerate(text.splitlines(), start=1):
        m = _FAILED_LINE_RE.match(ln.strip())
        if m:
            failures.setdefault(m.group("nodeid"), i)
    if not failures and not _SESSION_RE.search(text[:4000]):
        return None
    return failures


def _fail_blocks(text: str) -> list[tuple[str, int]]:
    blocks: list[tuple[str, int]] = []
    for i, ln in enumerate(text.splitlines(), start=1):
        m = _FAIL_HEADER_RE.match(ln.strip())
        if m:
            blocks.append((m.group("nodeid").strip(), i))
    return blocks


def _block_start(blocks: list[tuple[str, int]], nodeid: str) -> int | None:
    """First traceback block whose header names this nodeid (headers carry
    the bare test name; summary lines carry the full path::name)."""
    tail = nodeid.split("::")[-1]
    # pytest writes a class-scoped failure header in DOTTED form
    # ("TestFoo.test_bar") while the summary line is "::"-qualified
    # ("tests/t.py::TestFoo::test_bar"), so a "::"-only comparison never
    # matched for ANY class-based test and every one of them silently lost
    # its traceback block.
    dotted = ".".join(nodeid.split("::")[1:]) if "::" in nodeid else nodeid
    for name, line_no in blocks:
        if name in (nodeid, tail, dotted):
            return line_no
        if name.endswith("::" + tail) or name.endswith("." + tail):
            return line_no
    return None


def _span_end(start: int, anchors: list[int], total: int) -> int:
    """Where this claim's evidence stops -- whichever comes first.

    ``start + _SPAN_LINES`` is a *display* budget: how much of a traceback is
    worth quoting. The next anchor is a *truth* boundary: past it the lines
    belong to a different failure, and a span minted for nodeid X that runs
    into nodeid Y's traceback attributes Y's evidence to X. A bug bash found
    the fixed window doing exactly that on adjacent failures.

    Anchors are every other claim coordinate in this stream -- traceback
    headers and summary lines alike -- because either kind starts evidence
    that is not ours.
    """
    hi = start + _SPAN_LINES
    nxt = min((a for a in anchors if a > start), default=None)
    if nxt is not None:
        hi = min(hi, nxt - 1)
    return max(start, min(hi, max(total, start)))


def run_diff(store: Store, ws: Workspace, ref_a_text: str, ref_b_text: str) -> str:
    man_a = _manifest(store, ref_a_text)
    man_b = _manifest(store, ref_b_text)
    a12 = short_id(man_a["id"])
    b12 = short_id(man_b["id"])
    budget = ws.config.budgets

    streams_a: dict[str, Any] = man_a["streams"]
    streams_b: dict[str, Any] = man_b["streams"]
    raw_scanned = sum(
        int(m.get("bytes", 0)) for m in [*streams_a.values(), *streams_b.values()]
    )

    out = [f"[ctx diff run:{a12} → run:{b12}]"]
    cmd_same = man_a["argv"] == man_b["argv"] and man_a.get("shell") == man_b.get("shell")
    cwd_same = man_a["cwd"] == man_b["cwd"]
    out.append(
        f"command: {'identical' if cmd_same else 'differs'}"
        f" · cwd: {'identical' if cwd_same else 'differs'}"
    )

    ra, rb = man_a["result"], man_b["result"]
    if ra == rb:
        out.append(
            f"result: unchanged (exit={ra['exitCode']} signal={ra['signal']} "
            f"timedOut={ra['timedOut']})"
        )
    else:
        parts = []
        for key, label in (("exitCode", "exit"), ("signal", "signal"), ("timedOut", "timedOut")):
            if ra[key] != rb[key]:
                parts.append(f"{label} {ra[key]} → {rb[key]}")
        out.append("result: " + " · ".join(parts))

    names = sorted(set(streams_a) | set(streams_b))
    identical_streams = all(
        (streams_a.get(n) or {}).get("blob") == (streams_b.get(n) or {}).get("blob")
        for n in names
    )
    if identical_streams and ra == rb:
        out.append("no behavioral delta")
        for n in names:
            m = streams_a.get(n) or {"lines": 0, "bytes": 0}
            out.append(
                f"  {n}: {fmt_int(int(m['lines']))} lines · "
                f"{fmt_bytes(int(m['bytes']))} (identical)"
            )
        result = _emit(ws, "\n".join(out), budget.result_tokens)
        record_telemetry(store, "diff", raw_scanned, len(result.encode("utf-8")))
        return result

    out.append("streams:")
    for n in names:
        ma = streams_a.get(n) or {"lines": 0, "bytes": 0, "blob": None}
        mb = streams_b.get(n) or {"lines": 0, "bytes": 0, "blob": None}
        if ma.get("blob") == mb.get("blob"):
            out.append(
                f"  {n}: identical ({fmt_int(int(ma['lines']))} lines · "
                f"{fmt_bytes(int(ma['bytes']))})"
            )
        else:
            delta = int(mb["lines"]) - int(ma["lines"])
            out.append(
                f"  {n}: {fmt_int(int(ma['lines']))} → {fmt_int(int(mb['lines']))} lines "
                f"({delta:+,}) · {fmt_bytes(int(ma['bytes']))} → {fmt_bytes(int(mb['bytes']))}"
            )

    text_a = _stream_text(store, streams_a.get("stdout"))
    text_b = _stream_text(store, streams_b.get("stdout"))
    blob_b = str((streams_b.get("stdout") or {}).get("blob") or "").removeprefix("sha256:")
    lines_b_total = int((streams_b.get("stdout") or {}).get("lines", 0))
    # A binary side is UNKNOWN, not empty. Substituting "" for it and carrying
    # on made every signature and template on the text side read as "only in
    # B" -- a delta manufactured from the absence of a comparison. "skipped"
    # has to mean skipped, so one binary side skips both analyses and the
    # notice names which side is unreadable.
    if text_a is None or text_b is None:
        binary_sides = [
            name for name, txt in (("A", text_a), ("B", text_b)) if txt is None
        ]
        out.append(
            f"analysis: binary stdout in {' and '.join(binary_sides)} — "
            "signature/template delta skipped (no text side to compare against)"
        )
        result = _emit(ws, "\n".join(out), budget.result_tokens)
        record_telemetry(store, "diff", raw_scanned, len(result.encode("utf-8")))
        return result

    next_span: str | None = None  # most salient new-in-B evidence

    # ---- failure-signature delta (pytest-style summary lines, A vs B)
    fails_a = _pytest_failures(text_a) if text_a else None
    fails_b = _pytest_failures(text_b) if text_b else None
    if fails_a is not None or fails_b is not None:
        new = sorted(set(fails_b or {}) - set(fails_a or {}))
        resolved = sorted(set(fails_a or {}) - set(fails_b or {}))
        if new or resolved:
            out.append("failures:")
        if new:
            out.append(f"  new failures: {fmt_int(len(new))}")
            blocks = _fail_blocks(text_b)
            anchors = sorted({ln for _n, ln in blocks} | set((fails_b or {}).values()))
            for nodeid in new[:_TOP]:
                start = _block_start(blocks, nodeid) or (fails_b or {}).get(nodeid) or 1
                end = _span_end(start, anchors, lines_b_total)
                tag = ""
                if blob_b:
                    sid = store.register_span(blob_b, "region", a=start, b=end)
                    tag = f" · span {sid}"
                    if next_span is None:
                        next_span = sid
                out.append(f"    {nodeid} · B stdout:L{start}-L{end}{tag}")
            if len(new) > _TOP:
                out.append(f"    … +{fmt_int(len(new) - _TOP)} more")
        if resolved:
            out.append(f"  resolved: {fmt_int(len(resolved))}")
            for nodeid in resolved[:_TOP]:
                out.append(f"    {nodeid}")
            if len(resolved) > _TOP:
                out.append(f"    … +{fmt_int(len(resolved) - _TOP)} more")

    # ---- template delta (bounded mining over both stdouts)
    tpl_a = mine_templates(text_a.splitlines()[:_MINE_CAP])[0] if text_a else {}
    tpl_b = mine_templates(text_b.splitlines()[:_MINE_CAP])[0] if text_b else {}
    only_b = sorted(
        ((t, rec) for t, rec in tpl_b.items() if t not in tpl_a),
        key=lambda kv: (-kv[1][0], kv[1][1], kv[0]),
    )
    only_a = sorted(
        ((t, rec) for t, rec in tpl_a.items() if t not in tpl_b),
        key=lambda kv: (-kv[1][0], kv[1][1], kv[0]),
    )
    shifts: list[tuple[float, int, int, str]] = []
    for t, (cb, _first) in tpl_b.items():
        rec = tpl_a.get(t)
        if rec is None:
            continue
        ca = rec[0]
        if cb > 2 * ca or ca > 2 * cb:
            shifts.append((max(ca, cb) / min(ca, cb), ca, cb, t))
    shifts.sort(key=lambda s: (-s[0], s[3]))

    if only_b or only_a or shifts:
        out.append("templates (stdout):")
    if only_b:
        out.append(f"  only in B: {fmt_int(len(only_b))}")
        for t, (count, first) in only_b[:_TOP]:
            tag = ""
            if blob_b:
                sid = store.register_span(blob_b, "template", template=t)
                tag = f" · span {sid}"
                if next_span is None:
                    next_span = sid
            out.append(f"    {fmt_int(count)}× L{first}: {t[:140]}{tag}")
        if len(only_b) > _TOP:
            out.append(f"    … +{fmt_int(len(only_b) - _TOP)} more")
    if only_a:
        out.append(f"  only in A: {fmt_int(len(only_a))}")
        for t, (count, _first) in only_a[:_TOP]:
            out.append(f"    {fmt_int(count)}× {t[:140]}")
        if len(only_a) > _TOP:
            out.append(f"    … +{fmt_int(len(only_a) - _TOP)} more")
    if shifts:
        out.append(f"  count shifts (>2x): {fmt_int(len(shifts))}")
        for _ratio, ca, cb, t in shifts[:_TOP]:
            out.append(f"    {fmt_int(ca)}× → {fmt_int(cb)}×: {t[:130]}")
        if len(shifts) > _TOP:
            out.append(f"    … +{fmt_int(len(shifts) - _TOP)} more")

    continuation = None
    if next_span:
        continuation = f"ctx get run:{b12}#stdout --span {next_span}"
    result = _emit(ws, "\n".join(out), budget.result_tokens, continuation)
    record_telemetry(store, "diff", raw_scanned, len(result.encode("utf-8")))
    return result
