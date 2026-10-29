"""JSON and JSON Lines digest profiles: schema shape without payload bytes."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from ctx.digest.base import DigestContext, Profile
from ctx.textutil import fmt_int, loads_fast


HEAD_RECORDS = 5  # records inlined verbatim before a span to the rest
HEAD_WIDTH = 200  # per-record character cap


def _project(rec: Any) -> str:
    """One deterministic line per record: scalar top-level fields only, nested
    containers collapsed to ``{…}`` / ``[…]``. Byte-stable (sorted keys)."""
    if isinstance(rec, dict):
        flat = {
            k: v
            for k, v in rec.items()
            if isinstance(v, (str, int, float, bool)) or v is None
        }
        s = json.dumps(flat, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    elif isinstance(rec, list):
        s = "[…]"
    else:
        s = json.dumps(rec, ensure_ascii=False)
    return s if len(s) <= HEAD_WIDTH else s[: HEAD_WIDTH - 1] + "…"


def _dominant_array(doc: Any) -> tuple[list[Any], str] | None:
    """The array a reader most likely wants to page through, plus its
    json-pointer prefix. Top-level list → the doc itself; else the first
    list-valued key (sorted, for determinism)."""
    if isinstance(doc, list):
        return doc, ""
    if isinstance(doc, dict):
        for k in sorted(doc.keys()):
            v = doc[k]
            if isinstance(v, list) and v:
                escaped = k.replace("~", "~0").replace("/", "~1")  # RFC 6901: escape ~ and / in the pointer segment
                return v, f"/{escaped}"
    return None


def _shape(value: Any, depth: int = 0) -> str:
    if isinstance(value, dict):
        if depth >= 2:
            return "object"
        keys = sorted(value.keys())[:8]
        inner = ", ".join(f"{k}: {_shape(value[k], depth + 1)}" for k in keys)
        more = "" if len(value) <= 8 else f", …+{len(value) - 8}"
        return "{" + inner + more + "}"
    if isinstance(value, list):
        if not value:
            return "array[0]"
        return f"array[{len(value)}] of {_shape(value[0], depth + 1)}"
    return type(value).__name__.replace("NoneType", "null").replace("str", "string").replace(
        "int", "number"
    ).replace("float", "number").replace("bool", "boolean")


class JsonProfile(Profile):
    version = "json/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        text = ctx.stdout.text.lstrip()
        if not text or text[0] not in "{[":
            return None
        if not ctx.stdout.parsed_fully:
            return None
        try:
            self._doc = loads_fast(ctx.stdout.text)
        except json.JSONDecodeError:
            return None
        return "stdout parses as a single JSON document"

    def render(self, ctx: DigestContext) -> str:
        doc = self._doc
        summary = ["summary:", f"  shape (exact): {_shape(doc)}"]
        if isinstance(doc, list):
            summary.append(f"  records (exact): {fmt_int(len(doc))}")
        elif isinstance(doc, dict):
            summary.append(f"  top-level keys (exact): {fmt_int(len(doc))}")

        # Head-N record inlining: a shape line alone forces a re-fetch to see
        # any actual data. Inline the first records (scalar fields) + a span to
        # the rest, mirroring search/v1's top-matches+span. run:PENDING is
        # rewritten to the real id by render_run_digest.
        rid = "run:PENDING"
        head: list[str] = []
        dom = _dominant_array(doc)
        if dom is not None:
            arr, ptr_prefix = dom
            head.append("records (head):")
            for rec in arr[:HEAD_RECORDS]:
                head.append("  " + _project(rec))
            if len(arr) > HEAD_RECORDS:
                head.append(
                    f"  … +{fmt_int(len(arr) - HEAD_RECORDS)} more records · "
                    f"ctx get {rid}#stdout --json-pointer {ptr_prefix}/{HEAD_RECORDS}"
                )

        suggestions = [
            f"ctx get {rid}#stdout --json-pointer /<path>",
            f"ctx stats {rid}",
        ]
        return "\n".join(
            ctx.header_lines()
            + summary
            + head
            + self.coverage_lines(ctx, 1)
            + self.next_lines(ctx, suggestions)
        )


class JsonLinesProfile(Profile):
    version = "jsonl/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        lines = [ln for ln in ctx.stdout.text_lines[:50] if ln.strip()]
        if len(lines) < 2:
            return None
        parsed = []
        for ln in lines[:5]:
            try:
                parsed.append(loads_fast(ln))
            except json.JSONDecodeError:
                return None
        if not all(isinstance(p, dict) for p in parsed):
            return None
        self._sample = parsed
        return "first stdout lines each parse as JSON objects"

    def render(self, ctx: DigestContext) -> str:
        records = 0
        key_counter: Counter[str] = Counter()
        levels: Counter[str] = Counter()
        for ln in ctx.stdout.text_lines:
            if not ln.strip():
                continue
            records += 1
            try:
                obj = loads_fast(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                key_counter.update(obj.keys())
                lvl = obj.get("level") or obj.get("severity")
                if isinstance(lvl, str):
                    levels[lvl.lower()] += 1
        exact = ctx.stdout.parsed_fully
        label = "exact" if exact else "approximate"
        summary = [
            "summary:",
            f"  records ({label}): {fmt_int(records)}",
            "  fields ("
            + label
            + "): "
            + ", ".join(f"{k}×{v}" for k, v in sorted(key_counter.most_common(8))),
        ]
        if levels:
            summary.append(
                f"  levels ({label}): " + " · ".join(f"{k}:{v}" for k, v in sorted(levels.items()))
            )
        spans = ctx.focus_spans()
        shown = 0
        for stream, a, b, sample in spans:
            summary.append(f"  focus {stream}:L{a}-L{b}: {sample}")
            shown += 1
        rid = "run:PENDING"
        suggestions = [
            f"ctx search {rid} '<pattern>' --context 1",
            f"ctx get {rid}#stdout --records 1:20",
        ]
        return "\n".join(
            ctx.header_lines() + summary + self.coverage_lines(ctx, shown or 1) + self.next_lines(ctx, suggestions)
        )
