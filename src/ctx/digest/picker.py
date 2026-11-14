"""Optional decision-model picker for the evidence map (``[picker] mode``).

The local rule (:mod:`ctx.digest.evidence_map`) always builds the distinct
messages and their order. This module only decides WHICH of them the digest
expands, and only when ``[picker] mode`` is ``shadow`` or ``model``:

* ``model``: one request to the decision endpoint asks a yes/no relevance
  question per candidate; candidates scoring at or above ``threshold`` are
  expanded best first, and the remaining slots follow the local order. Each
  expanded excerpt says who picked it. The list of every other message stays
  in the local order, so the model's choice cannot hide anything.
* ``shadow``: the same request; the LOCAL choice is delivered and both are
  recorded, so an operator can compare before turning ``model`` on.

Nothing here imports a network module: the HTTP client
(:mod:`ctx.digest.decision_client`) is imported inside :meth:`PickerRun.choose`
on the shadow/model path only. With the default ``local`` no
:class:`PickerRun` is ever created.

Fail open: a timeout, an HTTP or transport error, a missing key or an answer
that does not parse leaves the digest exactly the local one; the failure is
written once to ``guard-failures.jsonl`` (a category, never the key or the
payload) and to the picker ledger record, and the reader is told nothing.

Ledger: one JSON line per digest the picker ran for, in
``.ctx-session-reads/picker.jsonl`` (schema ``ctx.picker/v1``): the handle,
mode, number of candidates, the local order and the model order (as
``stream:Lfirst`` references, never text), the scores, what was expanded,
latency, tokens when the endpoint reports them, and the error category.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Protocol

LEDGER = "picker.jsonl"
SCHEMA = "ctx.picker/v1"
INTENT_CHARS = 300
#: All candidate excerpts of one request together stay under this many bytes.
REQUEST_CANDIDATE_BYTES = 24_000
QUESTION = (
    "Is this excerpt the root cause that answers the engineer's question "
    "(not a warning, a retry that recovered, or an unrelated error)?\n"
)


class Scorer(Protocol):
    """``score(intent, command, candidates) -> (scores, tokens)``: one
    probability of "yes" per candidate, in order; ``tokens`` when reported.
    Raises :class:`PickerError` (or anything) on failure."""

    def score(self, intent: str, command: str, candidates: list[str]) -> tuple[list[float], int | None]:
        ...


class PickerError(Exception):
    """A failure whose message is a fixed, key-free category."""


def _ref(m: Any) -> str:
    return f"{m.stream}:L{m.first}"


def _redacted(text: str, width: int) -> str:
    from ctx.digest.evidence_map import _shown_line

    return _shown_line(text, width, set())


def intent_line(command: str, focus: str | None) -> str:
    """What the question is about: the user's last prompt when ctx has it
    (first 300 characters, redacted), else the command and any focus."""
    try:
        from ctx.task_context import current_query

        q = current_query()
    except Exception:
        q = None
    if q and q.strip():
        return _redacted(" ".join(q.split()), INTENT_CHARS)
    base = f"(no prompt available) the command `{_redacted(command, INTENT_CHARS)}`"
    if focus:
        base += f", focus: {_redacted(focus, 120)}"
    return base


class PickerRun:
    """One digest's picker decision. Created by ``render_run_digest`` only
    when the mode is shadow/model and the caller is a live delivery path."""

    def __init__(self, policy: Any, ws_root: Any, command: str, focus: str | None,
                 scorer: Scorer | None = None) -> None:
        self.policy = policy
        self.ws_root = ws_root
        self.command = command
        self.focus = focus
        self.scorer = scorer
        self.record: dict[str, Any] | None = None
        self.model_used = False
        self.scores_hash: str | None = None

    # -- the decision -----------------------------------------------------
    def candidates(self, emap: Any) -> tuple[list[int], list[str]]:
        """Indices (local order) and redacted sample excerpts sent."""
        idx: list[int] = []
        texts: list[str] = []
        used = 0
        cap = int(self.policy.max_candidate_bytes)
        from ctx.digest.evidence_map import line_of, n_lines

        for i, m in enumerate(emap.messages[: int(self.policy.max_candidates)]):
            # line_of: the same sanitised line the digest would show (ANSI
            # removed, private-key body lines replaced), then redacted.
            a, b = max(emap.first_line, m.first - 2), min(n_lines(emap, m.stream), m.first + 2)
            rows = [f"L{n}: {_redacted(line_of(emap, m.stream, n), 200)}" for n in range(a, b + 1)]
            if m.count > 1:
                rows.append(f"(this message occurs {m.count} times, lines {m.first}-{m.last})")
            text = _redacted("\n".join(rows), cap)
            size = len(text.encode("utf-8"))
            if used + size > REQUEST_CANDIDATE_BYTES:
                break
            used += size
            idx.append(i)
            texts.append(text)
        return idx, texts

    def choose(self, emap: Any, k: int) -> tuple[list[int] | None, dict[int, str] | None]:
        """(expansion order, labels) for the render; (None, None) = local."""
        mode = self.policy.mode
        local_pick = list(range(min(k, len(emap.messages))))
        if mode not in ("shadow", "model"):
            # Local: nothing is sent and no client is imported; the record
            # is the baseline `ctx replay --picker` compares the modes with.
            n = min(int(self.policy.max_candidates), len(emap.messages))
            refs = [_ref(emap.messages[i]) for i in range(n)]
            self.record = {
                "schema": SCHEMA, "ts": round(time.time(), 3), "mode": "local", "handle": None,
                "candidates": n, "messages": len(emap.messages), "local_order": refs,
                "scores": None, "model_order": None,
                "local_expanded": [_ref(emap.messages[i]) for i in local_pick],
                "expanded": [_ref(emap.messages[i]) for i in local_pick],
                "latency_ms": 0.0, "tokens": None, "error": None, "model": None,
            }
            return None, None
        idx, texts = self.candidates(emap)
        rec: dict[str, Any] = {
            "schema": SCHEMA, "ts": round(time.time(), 3), "mode": mode, "handle": None,
            "candidates": len(idx), "messages": len(emap.messages),
            "local_order": [_ref(emap.messages[i]) for i in idx],
            "scores": None, "model_order": None,
            "local_expanded": [_ref(emap.messages[i]) for i in local_pick],
            "expanded": None, "latency_ms": None, "tokens": None, "error": None,
            "model": str(self.policy.model),
        }
        self.record = rec
        t0 = time.perf_counter()
        scores: list[float] | None = None
        try:
            if not idx:
                raise PickerError("no candidates")
            scorer = self.scorer
            if scorer is None:
                from ctx.digest.decision_client import DecisionClient

                scorer = DecisionClient(self.policy)
            command = _redacted(self.command, INTENT_CHARS)
            got, tokens = scorer.score(intent_line(self.command, self.focus), command, texts)
            if (not isinstance(got, list) or len(got) != len(idx)
                    or not all(isinstance(s, (int, float)) and not isinstance(s, bool)
                               and 0.0 <= float(s) <= 1.0 for s in got)):
                raise PickerError("malformed answer")
            scores = [float(s) for s in got]
            rec["tokens"] = tokens if isinstance(tokens, int) else None
        except Exception as exc:  # noqa: BLE001 -- fail open, by contract
            rec["error"] = _category(exc)
            _note_failure(self.ws_root, rec["error"])
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        if scores is None:
            rec["expanded"] = rec["local_expanded"]
            return None, None
        rec["scores"] = [round(s, 4) for s in scores]
        ranked = sorted(range(len(idx)), key=lambda j: (-scores[j], j))
        model_pick = [idx[j] for j in ranked if scores[j] >= float(self.policy.threshold)][:k]
        rec["model_order"] = [_ref(emap.messages[idx[j]]) for j in ranked]
        labels: dict[int, str] = {}
        order = list(model_pick)
        for i in model_pick:
            labels[i] = f"the question (p={scores[idx.index(i)]:.2f})"
        for i in range(len(emap.messages)):
            if len(order) >= k:
                break
            if i not in order:
                order.append(i)
                labels[i] = "the local rule"
        rec["model_expanded"] = [_ref(emap.messages[i]) for i in order]
        if mode != "model":
            rec["expanded"] = rec["local_expanded"]
            return None, None
        self.model_used = True
        self.scores_hash = hashlib.sha256(
            json.dumps([rec["local_order"], rec["scores"]]).encode("utf-8")).hexdigest()[:12]
        rec["expanded"] = rec["model_expanded"]
        return order, labels

    def finish(self, handle: str) -> None:
        """Write the ledger record once the digest's handle is known."""
        if self.record is None or not self.ws_root:
            return
        self.record["handle"] = handle
        try:
            from ctx.sessiondir import session_reads_dir

            d = session_reads_dir(self.ws_root)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, LEDGER), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(self.record, sort_keys=True) + "\n")
        except Exception:
            pass


def _category(exc: BaseException) -> str:
    """A fixed, key-free description of a failure (never ``str(exc)`` of a
    transport error, which can quote a URL or a server's echo)."""
    if isinstance(exc, PickerError):
        return str(exc)
    if isinstance(exc, TimeoutError) or type(exc).__name__ in ("timeout", "TimeoutError"):
        return "timeout"
    return f"error: {type(exc).__name__}"


def _note_failure(ws_root: Any, category: str) -> None:
    """One line in the guard-failure ledger (the hook's format)."""
    if not ws_root:
        return
    try:
        from ctx.sessiondir import session_reads_dir

        d = session_reads_dir(ws_root)
        os.makedirs(d, exist_ok=True)
        line = json.dumps({"op": "picker", "stage": "score", "error": category.split(":")[0],
                           "detail": category[:200], "ts": time.time()}, sort_keys=True)
        with open(os.path.join(d, "guard-failures.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def read_ledger(ws_root: Any) -> list[dict[str, Any]]:
    from ctx.sessiondir import session_reads_dir

    rows: list[dict[str, Any]] = []
    try:
        with open(session_reads_dir(ws_root, LEDGER), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("schema") == SCHEMA:
                    rows.append(rec)
    except OSError:
        pass
    return rows


# ---------------------------------------------------------------- replay report
def picker_report(ws_root: Any, telemetry_path: Any) -> dict[str, Any]:
    """Per picker mode: digests the picker ran for, how many were followed
    by a fetch (``ctx get``/``ctx search``) from the same handle, the fetch
    count, failures, and -- where the model answered -- how often its top
    pick differed from the local rule's."""
    rows = read_ledger(ws_root)
    fetches: dict[str, list[float]] = {}
    try:
        with open(telemetry_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("op") in ("get", "search") and ev.get("ref"):
                    fetches.setdefault(str(ev["ref"]), []).append(float(ev.get("ts") or 0))
    except OSError:
        pass
    # Each fetch belongs to ONE record: the latest record of that handle at
    # or before the fetch (the same output digested twice yields one handle).
    owned: dict[int, int] = {}
    by_handle: dict[str, list[tuple[float, int]]] = {}
    for k, rec in enumerate(rows):
        by_handle.setdefault(str(rec.get("handle")), []).append((float(rec.get("ts") or 0), k))
    for handle, times in fetches.items():
        recs = sorted(by_handle.get(handle, ()))
        for t in times:
            owner = None
            for ts, k in recs:
                if ts <= t:
                    owner = k
            if owner is not None:
                owned[owner] = owned.get(owner, 0) + 1
    modes: dict[str, dict[str, Any]] = {}
    for k, rec in enumerate(rows):
        m = modes.setdefault(str(rec.get("mode")), {
            "digests": 0, "followed_by_fetch": 0, "fetches": 0, "errors": 0,
            "compared": 0, "top_pick_differs": 0, "latency_ms": [], "tokens": 0})
        m["digests"] += 1
        if owned.get(k):
            m["followed_by_fetch"] += 1
            m["fetches"] += owned[k]
        if rec.get("error"):
            m["errors"] += 1
        lo, mo = rec.get("local_order") or [], rec.get("model_order") or []
        if lo and mo:
            m["compared"] += 1
            m["top_pick_differs"] += int(lo[0] != mo[0])
        if isinstance(rec.get("latency_ms"), (int, float)):
            m["latency_ms"].append(float(rec["latency_ms"]))
        if isinstance(rec.get("tokens"), int):
            m["tokens"] += rec["tokens"]
    for m in modes.values():
        lat = sorted(m.pop("latency_ms"))
        m["latency_ms_median"] = lat[(len(lat) - 1) // 2] if lat else None
    return {"schema": "ctx.picker-report/v1", "modes": modes}


def render_picker_report(report: dict[str, Any]) -> str:
    modes = report.get("modes") or {}
    if not modes:
        return ("picker: no records (.ctx-session-reads/picker.jsonl is written only "
                "when [picker] mode is shadow or model)")
    out = ["picker report (per mode; a fetch is a later ctx get/search of the same run handle)",
           "  mode    digests  followed-by-fetch  fetches  errors  top-pick-differs  median-ms  tokens"]
    for name in sorted(modes):
        m = modes[name]
        diff = f"{m['top_pick_differs']}/{m['compared']}" if m["compared"] else "-"
        med = "-" if m["latency_ms_median"] is None else f"{m['latency_ms_median']:.0f}"
        out.append(f"  {name:<7} {m['digests']:>7}  {m['followed_by_fetch']:>17}  "
                   f"{m['fetches']:>7}  {m['errors']:>6}  {diff:>16}  {med:>9}  {m['tokens']:>6}")
    return "\n".join(out)
