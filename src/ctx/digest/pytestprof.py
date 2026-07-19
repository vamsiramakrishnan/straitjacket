"""pytest digest profile: extraction/rendering split (EDC phases 1+3).

pytest/v2 is the first EDC instance. ALL parsing lives in
:func:`extract_pytest` (layer 1, semantic extraction): failure node ids,
file:line locations, failure class (exception name), a bounded one-line
summary per item, a minted region span per traceback block, an honest
coverage attestation under pipe truncation, and volatile timing
quarantined out of graph identity. Rendering for failures goes through
the plan-obeying pure renderer (ctx.digest.evidence_render) against the
committed pytest contract — the layering law made real: policy selects
among representations extraction built; it can never invent one.

The PASS path stays byte-identical to pytest/v1 (the legacy renderer is
kept verbatim for it), and the digest meta records the split: pass
renders stay ``pytest/v1``; failure renders are ``pytest/v2``.

Elapsed times from pytest's summary line are deliberately dropped from
rendering — digests must not carry timing noise (SPEC §8); extraction
quarantines the duration in the graph's ``volatile`` map.
"""

from __future__ import annotations

import re
from collections import Counter

from ctx.digest.base import DigestContext, Profile
from ctx.evidence import EvidenceGraph, EvidenceItem, EvidenceRef
from ctx.textutil import fmt_int

_SESSION_RE = re.compile(r"=+ test session starts =+")
_SUMMARY_RE = re.compile(
    r"=+ (?P<body>[^=]*?(?:passed|failed|error|skipped|xfailed|xpassed|deselected|warnings?)[^=]*?) =+"
)
# pytest -q (and piped/quiet output) ends with a BARE summary line — no `=`
# banner: "8 failed, 1 passed in 0.05s". The spec3 re-run loop hit exactly
# this shape ("summary line not found" on every `| head`-sliced re-run).
_BARE_SUMMARY_RE = re.compile(r"^(?P<body>\d+ [a-z]+(?:, \d+ [a-z]+)*) in [\d.]+s\b.*$")
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR) (?P<nodeid>\S+)(?: - (?P<msg>.*))?$")
# Verbose-mode progress rows ("path::test FAILED [ 22%]") — the census
# fallback when output was truncated before the short-summary section.
_VERBOSE_PROG_RE = re.compile(r"^(?P<nodeid>\S+::\S+) (?:FAILED|ERROR)\b")
_FAIL_HEADER_RE = re.compile(r"^_{3,} (?P<nodeid>.+?) _{3,}$")
_SHORT_SUMMARY_BANNER_RE = re.compile(r"=+ short test summary info =+")
_FAILURES_BANNER_RE = re.compile(r"^=+ (?:FAILURES|ERRORS) =+", re.MULTILINE)
_DURATION_RE = re.compile(r"\bin (?P<secs>[\d.]+)s\b")
# Exception-name extraction (hierarchy level 3): the E-line's leading
# token when it names an exception class, else the traceback locus line
# ("path:12: AssertionError"), else the short-summary message.
_E_CLASS_RE = re.compile(
    r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Failure|Timeout|Interrupt|Exit|Warning))\b"
)
_LOCUS_RE = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):(?:\s+(?P<cls>[A-Za-z_][\w.]*))?\s*$")
_MSG_CLASS_RE = re.compile(
    r"^([A-Za-z_][\w.]*(?:Error|Exception|Failure|Timeout|Interrupt|Exit|Warning))\b"
)


def _is_banner(line: str) -> bool:
    return line.startswith("=") and line.rstrip().endswith("=")


def _short_nodeid(nid: str) -> str:
    """Shorten the file-path part to its last two components (lint/v1 house
    style); the ::-qualified test name always rides in full."""
    if "::" not in nid:
        return nid
    path, rest = nid.split("::", 1)
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 2:
        path = "/".join(parts[-2:])
    return path + "::" + rest


# ---------------------------------------------------------------- parsing
# Module-level, shared by extract_pytest (layer 1) and the legacy pass
# renderer — one parse, one truth.
def _parse_counts(text: str, out_lines: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in _SUMMARY_RE.finditer(text):
        for n, kind in _COUNT_RE.findall(m.group("body")):
            kind = kind.rstrip("s") if kind in ("errors",) else kind
            counts[kind] = int(n)
    if not counts:
        # Bare -q summary: scan the terminal lines only (deterministic;
        # the summary is always last when present at all).
        for ln in reversed(out_lines[-40:]):
            m = _BARE_SUMMARY_RE.match(ln.strip())
            if not m:
                continue
            found = _COUNT_RE.findall(m.group("body"))
            if not found:
                continue
            for n, kind in found:
                kind = kind.rstrip("s") if kind in ("errors",) else kind
                counts[kind] = int(n)
            break
    return counts


def _parse_duration(out_lines: list[str]) -> str | None:
    """The summary line's elapsed time — volatile, quarantined (EDC §5
    amendment 1): never in graph identity, never rendered."""
    for ln in reversed(out_lines[-40:]):
        stripped = ln.strip()
        if _BARE_SUMMARY_RE.match(stripped) or (_is_banner(stripped) and _COUNT_RE.search(stripped)):
            m = _DURATION_RE.search(stripped)
            if m:
                return m.group("secs") + "s"
    return None


def _collect_failed_tests(out_lines: list[str]) -> list[tuple[int, str, str]]:
    """Failing node ids: short-summary FAILED/ERROR lines, else (truncated
    output) verbose progress rows. (line_no, nodeid, msg)."""
    failed_tests: list[tuple[int, str, str]] = []
    for i, ln in enumerate(out_lines, start=1):
        fm = _FAILED_LINE_RE.match(ln.strip())
        if fm:
            failed_tests.append((i, fm.group("nodeid"), fm.group("msg") or ""))
    if not failed_tests:
        for i, ln in enumerate(out_lines, start=1):
            vm = _VERBOSE_PROG_RE.match(ln.strip())
            if vm:
                failed_tests.append((i, vm.group("nodeid"), ""))
    return failed_tests


def _collect_blocks(out_lines: list[str]) -> list[tuple[str, int, int]]:
    """Traceback blocks: each `___ name ___` header owns the lines up to
    the next header or `=` banner (trailing blanks trimmed) — real block
    extents, so census spans resolve to whole tracebacks."""
    headers: list[tuple[str, int]] = []
    boundaries: list[int] = []
    for i, ln in enumerate(out_lines, start=1):
        hm = _FAIL_HEADER_RE.match(ln.strip())
        if hm:
            headers.append((hm.group("nodeid").strip(), i))
            boundaries.append(i)
        elif _is_banner(ln):
            boundaries.append(i)
    blocks: list[tuple[str, int, int]] = []
    n_lines = len(out_lines)
    for name, start in headers:
        nxt = [b for b in boundaries if b > start]
        end = (min(nxt) - 1) if nxt else n_lines
        while end > start and not out_lines[end - 1].strip():
            end -= 1
        blocks.append((name, start, end))
    return blocks


def _match_entries(
    failed_tests: list[tuple[int, str, str]], blocks: list[tuple[str, int, int]]
) -> list[dict]:
    """Census entries: blocks matched to full node ids by name (headers
    carry only "test_x" / "TestC.test_x"); leftover summary-only rows
    (e.g. --tb=line/--tb=no output has no blocks) keep their own
    FAILED-line coordinate. dict: id, name, a, b (None = no block), msg."""
    used: set[int] = set()

    def _match(name: str) -> tuple[str, str] | None:
        suffixes = ("::" + name, "::" + name.replace(".", "::"))
        for idx, (_, nid, msg) in enumerate(failed_tests):
            if idx in used:
                continue
            if nid == name or nid.endswith(suffixes[0]) or nid.endswith(suffixes[1]):
                used.add(idx)
                return nid, msg
        return None

    entries: list[dict] = []
    for name, start, end in blocks:
        hit = _match(name)
        entries.append(
            {
                "id": hit[0] if hit else name,
                "name": name,
                "a": start,
                "b": end,
                "msg": hit[1] if hit else "",
            }
        )
    for idx, (line_no, nid, msg) in enumerate(failed_tests):
        if idx not in used:
            entries.append({"id": nid, "name": nid.split("::")[-1], "a": line_no, "b": None, "msg": msg})
    return entries


def _entry_semantics(
    ent: dict, out_lines: list[str]
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    """(failure_class, location, one_line_summary, evidence_lines) for one
    census entry — hierarchy levels 3 and 4, extracted here or nowhere."""
    e_lines: list[str] = []
    locus_file = locus_line = locus_cls = None
    if ent["b"] is not None:
        block = out_lines[ent["a"] - 1 : ent["b"]]
        for raw in block:
            if raw.startswith("E ") and raw.strip() != "E":
                e_lines.append(raw.strip())
        for raw in reversed(block):
            lm = _LOCUS_RE.match(raw.strip())
            if lm:
                locus_file, locus_line, locus_cls = (
                    lm.group("file"),
                    lm.group("line"),
                    lm.group("cls"),
                )
                break
    msg = (ent["msg"] or "").strip()

    # failure class: E-line exception token > locus class > message class >
    # bare-assert inference. Never invented — None when nothing attests it.
    failure_class: str | None = None
    for el in e_lines:
        cm = _E_CLASS_RE.match(el)
        if cm:
            failure_class = cm.group(1)
            break
    if failure_class is None and locus_cls:
        failure_class = locus_cls
    if failure_class is None and msg:
        mm = _MSG_CLASS_RE.match(msg)
        if mm:
            failure_class = mm.group(1)
        elif msg.startswith("assert"):
            failure_class = "AssertionError"
    if failure_class is None and e_lines and e_lines[0][2:].lstrip().startswith("assert"):
        failure_class = "AssertionError"

    # location: the traceback locus (source coordinates), else the node
    # id's file part — always file-anchored when any file is known.
    location: str | None = None
    if locus_file and locus_line:
        location = f"{locus_file}:{locus_line}"
    elif "::" in ent["id"]:
        location = ent["id"].split("::", 1)[0]

    # bounded one-line summary (level 4): first E line, else the summary
    # message, else the block's last non-blank line; class prefix deduped.
    summary: str | None = None
    if e_lines:
        summary = e_lines[0][1:].lstrip()  # strip the "E" gutter
    elif msg:
        summary = msg
    elif ent["b"] is not None:
        for raw in reversed(out_lines[ent["a"] - 1 : ent["b"]]):
            if raw.strip():
                summary = raw.strip()
                break
    if summary and failure_class and summary.startswith(failure_class):
        rest = summary[len(failure_class) :].lstrip(": ").strip()
        if rest:
            summary = rest
    if summary:
        summary = summary[:120]

    evidence = tuple(el[:160] for el in e_lines[:4])
    if not evidence:
        fallback = msg or summary
        if fallback:
            evidence = (fallback[:160],)
    return failure_class, location, summary, evidence


# ------------------------------------------------------------- extraction
def extract_pytest(ctx: DigestContext) -> EvidenceGraph:
    """Layer 1: the complete typed extraction of a pytest run.

    Facts extracted per failing test: identity (node id), stdout block
    coordinates + minted region span (``span:<id>`` when a store is
    attached, ``lines:<a>:<b>`` otherwise), source location (file:line
    from the traceback locus), failure class (exception name), a bounded
    one-line summary, and up to four E-line evidence lines for dense
    rendering. Coverage attests completeness honestly: a parse that saw
    fewer failure blocks than the summary counted (pipe truncation), or a
    stream cut at the parse cap, is never attested complete."""
    out = ctx.stdout
    out_lines = out.text_lines
    text = out.text
    counts = _parse_counts(text, out_lines)
    duration = _parse_duration(out_lines)
    failed_tests = _collect_failed_tests(out_lines)
    blocks = _collect_blocks(out_lines)
    entries = _match_entries(failed_tests, blocks)

    items: list[EvidenceItem] = []
    root_seen = False
    for rank, ent in enumerate(entries):
        failure_class, location, summary, evidence = _entry_semantics(ent, out_lines)
        attributes: dict = {"stdout_a": ent["a"]}
        selector: str | None = None
        if ent["b"] is not None:
            attributes["stdout_b"] = ent["b"]
            attributes["block_name"] = ent["name"]
            sid = ctx.mint_span(ctx.stdout, "region", a=ent["a"], b=ent["b"])
            selector = f"span:{sid}" if sid else f"lines:{ent['a']}:{ent['b']}"
            if not root_seen:
                # Root detail (hierarchy level 5): the block head, banner-
                # stopped — the anticipatory-inline slice (mechanism E).
                head: list[str] = []
                for raw in out_lines[ent["a"] - 1 : min(ent["b"], ent["a"] + 12)]:
                    if _is_banner(raw):
                        break
                    head.append(raw[:160])
                attributes["detail_head"] = tuple(head)
                root_seen = True
        else:
            selector = f"lines:{ent['a']}:{ent['a']}"
        if evidence:
            attributes["evidence_lines"] = evidence
        items.append(
            EvidenceItem(
                id=ent["id"],
                kind="failing_test",
                severity="error",
                summary=summary,
                failure_class=failure_class,
                location=location,
                detail_ref=EvidenceRef(artifact="stdout", selector=selector),
                causal_rank=rank,
                attributes=attributes,
            )
        )

    expected = counts.get("failed", 0) + counts.get("error", 0)
    if items or expected > 0:
        outcome = "error" if (counts.get("error", 0) > 0 and counts.get("failed", 0) == 0 and counts) else "fail"
    elif counts:
        outcome = "pass"
    else:
        outcome = "unknown"

    complete = bool(counts) and out.parsed_fully and expected > 0 and len(items) >= expected
    if outcome == "pass":
        complete = bool(counts) and out.parsed_fully
    coverage = {
        "parsed": len(items),
        "total_estimate": max(expected, len(items)),
        "complete": complete,
    }

    artifacts = {}
    for name in ("stdout", "stderr"):
        try:
            artifacts[name] = str(ctx.manifest["streams"][name]["blob"])
        except Exception:
            pass

    return EvidenceGraph(
        family="pytest",
        profile_version="pytest/v2",
        outcome=outcome,
        aggregate=dict(counts),
        items=tuple(items),
        artifacts=artifacts,
        parser_warnings=(),
        coverage=coverage,
        volatile={"duration": duration} if duration else {},
    )


class PytestProfile(Profile):
    version = "pytest/v1"
    failure_version = "pytest/v2"

    def detect(self, ctx: DigestContext) -> str | None:
        argv = ctx.manifest["argv"]
        joined = " ".join(argv)
        if "pytest" in joined or "py.test" in joined:
            return "argv invokes pytest"
        text = ctx.stdout.text
        if _SESSION_RE.search(text[:4000]):
            return "stdout contains pytest session banner"
        # Quiet (-q) and pipe-truncated pytest output has no session banner;
        # the FAILURES / short-summary sections are still unambiguous.
        if _SHORT_SUMMARY_BANNER_RE.search(text):
            return "stdout contains pytest short-summary section"
        if _FAILURES_BANNER_RE.search(text):
            return "stdout contains pytest FAILURES section"
        return None

    # ------------------------------------------------------------ dispatch
    def render(self, ctx: DigestContext) -> str:
        """Extract once, then dispatch: failures render through the plan-
        obeying evidence renderer as pytest/v2; the pass path (and a run
        with no extractable failure identities) keeps the v1 rendering
        byte-identical, still versioned pytest/v1 in digest meta."""
        graph = extract_pytest(ctx)
        if graph.outcome in ("fail", "error") and graph.items:
            return self._render_v2(ctx, graph)
        return self._render_v1(ctx)

    # ---------------------------------------------------------- v2 (EDC)
    def _render_v2(self, ctx: DigestContext, graph: EvidenceGraph) -> str:
        from dataclasses import replace as dc_replace

        from ctx.contracts import contract_for_family
        from ctx.digest.evidence_render import (
            RenderEnv,
            default_fail_plan,
            flood_census_payload,
            render_fail_evidence,
        )

        plan = getattr(ctx, "plan", None)
        usable = plan is not None and getattr(plan, "mode", "") in (
            "fail_census",
            "dense",
            "bypass",
            "flood",
        )
        if not usable:
            plan = default_fail_plan(ctx.ws.config.budgets, dense=bool(getattr(ctx, "dense", False)))
        elif bool(getattr(ctx, "dense", False)) and plan.mode == "fail_census":
            # The reflex densify latch arrived beside a plain census plan:
            # densify wins (starvation evidence outranks the default).
            try:
                plan = dc_replace(plan, mode="dense", item_summary="expanded")
            except Exception:
                pass

        contract = contract_for_family("pytest")
        stderr_head = None
        if ctx.stderr.lines and ctx.stderr.text_lines:
            stderr_head = ctx.stderr.text_lines[0].strip()[:140]
        focus_lines = tuple(
            f"  focus {stream}:L{a}-L{b}: {sample}"
            for stream, a, b, sample in ctx.focus_spans(max_spans=2)
        )
        header_lines = ctx.header_lines()
        env = RenderEnv(
            stdout_lines=ctx.stdout.lines,
            stderr_lines=ctx.stderr.lines,
            parsed_fully=ctx.stdout.parsed_fully and ctx.stderr.parsed_fully,
            stderr_head=stderr_head,
            focus_lines=focus_lines,
            envelope_bytes=sum(len(ln.encode("utf-8")) + 1 for ln in header_lines) + 48,
            run_ref="run:PENDING",
        )
        rendered = render_fail_evidence(graph, contract, plan, env)

        # FLOOD mints the derived census blob (EDC §12 correction 3): the
        # renderer computed the content address purely; persist the bytes
        # so `blob:<id>` resolves. put_blob is idempotent by construction.
        if "full census blob:" in rendered.text and ctx.store is not None:
            try:
                ctx.store.put_blob(flood_census_payload(graph))
            except Exception:
                pass

        # Expose the receipt + graph to the caller/tests (selection-seam
        # accounting, never re-parsed from text) and version the meta.
        ctx.rendered_evidence = rendered
        ctx.evidence_graph = graph
        ctx.meta_profile_version = self.failure_version
        return "\n".join(header_lines + [rendered.text])

    # ------------------------------------------------- v1 (pass + legacy)
    def _render_v1(self, ctx: DigestContext) -> str:
        out_lines = ctx.stdout.text_lines
        counts = _parse_counts(ctx.stdout.text, out_lines)

        failed_tests = _collect_failed_tests(out_lines)
        raw_blocks = _collect_blocks(out_lines)
        entries = _match_entries(failed_tests, raw_blocks)
        blocks = raw_blocks

        total = sum(v for k, v in counts.items() if k in ("passed", "failed", "skipped", "error", "xfailed", "xpassed"))
        summary = ["summary:"]
        if counts:
            parts = [f"passed {fmt_int(counts.get('passed', 0))}"]
            for key in ("failed", "error", "skipped", "xfailed", "xpassed"):
                if counts.get(key):
                    parts.append(f"{key} {fmt_int(counts[key])}")
            summary.append(f"  tests: {fmt_int(total)} · " + " · ".join(parts))
        else:
            summary.append("  tests: summary line not found (session may have crashed)")

        shown = 0
        budgets = ctx.ws.config.budgets
        dense = bool(getattr(ctx, "dense", False))

        # ------------------------------------------------- failing-test census
        # Priority over the inline detail: census rows render first, so the
        # downstream bounded() emitter (which cuts from the bottom) and this
        # explicit cap both starve the detail before the census.
        if entries:
            census_budget = int(budgets.result_tokens * budgets.failure_budget_factor) * 2  # bytes
            summary.append("  failing tests (census):")
            spent = 0
            shown_entries = 0
            for ent in entries:
                row = "    " + _short_nodeid(ent["id"])[:160]
                if ent["b"] is not None:
                    sid = ctx.mint_span(ctx.stdout, "region", a=ent["a"], b=ent["b"])
                    row += f"  stdout:L{ent['a']}-L{ent['b']}"
                    if sid:
                        row += f" · span {sid}"
                else:
                    row += f"  stdout:L{ent['a']}"
                chunk = [row]
                if dense:
                    err = self._error_line(ent, out_lines)
                    if err:
                        chunk.append("      " + err[:160])
                cost = sum(len(c.encode("utf-8")) + 1 for c in chunk)
                if shown_entries and spent + cost > census_budget:
                    break
                summary.extend(chunk)
                spent += cost
                shown_entries += 1
            omitted_entries = entries[shown_entries:]
            if omitted_entries:
                rest_blocks = [e for e in omitted_entries if e["b"] is not None]
                if rest_blocks:
                    a = min(e["a"] for e in rest_blocks)
                    b = max(e["b"] for e in rest_blocks)
                    sid = ctx.mint_span(ctx.stdout, "region", a=a, b=b)
                    tail = f"stdout:L{a}-L{b}" + (f" · span {sid}" if sid else "")
                else:
                    tail = "ctx search run:PENDING 'FAILED' --context 0"
                summary.append(f"    … +{len(omitted_entries)} more failures · {tail}")
            expected = counts.get("failed", 0) + counts.get("error", 0)
            if expected > len(entries):
                summary.append(
                    f"    … census: {len(entries)} of {expected} failure blocks parsed (output truncated?)"
                )
            shown += shown_entries

        # ------------------------------------------- first-failure detail
        if blocks:
            name, start, end = blocks[0]
            sid = ctx.mint_span(ctx.stdout, "region", a=start, b=end)
            tag = f" · span {sid}" if sid else ""
            summary.append(f"  first failure stdout:L{start}-L{end}: {name}{tag}")
            shown += 1
            # Anticipatory inlining (mechanism E): the first failure region
            # is the one slice the model asks for next in almost every
            # failing-test loop, and a retrieval hop costs a full turn of
            # ttfb + suffix cache write. Inline it when the budget allows —
            # deterministic: pure function of bytes + committed budget.
            if budgets.result_tokens >= 600:
                for raw in out_lines[start - 1 : min(end, start + 12)]:
                    # Stop at the next section: separator banners carry
                    # elapsed-time noise and belong to other regions.
                    if _is_banner(raw):
                        break
                    summary.append(f"    | {raw[:160]}")
        elif failed_tests:
            summary.append(f"  first failure: {failed_tests[0][1]} - {failed_tests[0][2][:120]}")
            shown += 1

        # Modal failure signature: exception class frequencies.
        sig = Counter()
        for ln in out_lines:
            m = re.match(r"^E?\s*([A-Z][A-Za-z0-9_.]*(?:Error|Exception|Timeout|Failure))\b", ln.strip())
            if m:
                sig[m.group(1)] += 1
        if sig:
            summary.append("  failure signatures:")
            for name, n in sorted(sig.most_common(4)):
                summary.append(f"    {name}  {n}")

        if ctx.stderr.lines:
            err_first = ctx.stderr.text_lines[0].strip()[:140] if ctx.stderr.text_lines else ""
            summary.append(f"  stderr head stderr:L1: {err_first}")
            shown += 1

        for stream, a, b, sample in ctx.focus_spans(max_spans=2):
            summary.append(f"  focus {stream}:L{a}-L{b}: {sample}")
            shown += 1

        rid = "run:PENDING"
        suggestions = []
        if failed_tests:
            first_word = re.sub(r"[^\w.-]", " ", failed_tests[0][2]).split()
            probe = first_word[0] if first_word else failed_tests[0][1].split("::")[-1]
            suggestions.append(f"ctx search {rid} '{probe}' 'FAILED' --context 3")
        if blocks:
            start, end = blocks[0][1], blocks[0][2]
            suggestions.append(f"ctx get {rid}#stdout --lines {start}:{end}")
        if not suggestions:
            suggestions.append(f"ctx search {rid} 'failed' 'error' --context 3")

        return "\n".join(
            ctx.header_lines() + summary + self.coverage_lines(ctx, shown or 1) + self.next_lines(ctx, suggestions)
        )

    # ------------------------------------------------------------ dense mode
    def _error_line(self, ent: dict, out_lines: list[str]) -> str:
        """The one line that names an entry's failure: the block's first
        `E ...` assertion/exception line, else the short-summary message,
        else the block's last non-blank line (the `file:line: Error` locus)."""
        if ent["b"] is not None:
            block = out_lines[ent["a"] - 1 : ent["b"]]
            for raw in block:
                if raw.startswith("E "):
                    return raw.strip()
            if ent["msg"]:
                return ent["msg"].strip()
            for raw in reversed(block):
                if raw.strip():
                    return raw.strip()
        return ent["msg"].strip()
