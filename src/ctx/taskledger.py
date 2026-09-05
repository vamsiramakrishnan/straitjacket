"""The task ledger: how harnesses collaborate without ever talking to each other.

``ctx orchestrate`` already splits a task into a DAG, assigns each node a
``(harness, model)`` by capability and price, and hands results forward as
``checkpoint:`` addresses rather than raw bytes. What it did not have was a
*record of the collaboration itself* that outlived the orchestrating process.
Kill the process mid-run and the checkpoints survived but the run did not: no
way to know which nodes had finished, what they cost, why one stopped, or what
a node meant to tell the next one. Every harness spoke to the orchestrator
through stdout and to nothing else.

This module is the bus, built the way this project builds everything: as an
append-only, schema-versioned, privacy-safe ledger of small typed rows, one
file per task, under the workspace's own bookkeeping directory. Harnesses never
address each other. They append to the ledger and read from it; the
orchestrator is one more reader. Durability is then free — the ledger *is* the
run — and ``ctx orchestrate --resume`` is a replay, not a reconstruction.

## The rows

Six schemas, each a closed set of fields, each carrying an **address** rather
than content wherever content is involved::

    ctx.task/v1       the task: goal as a blob: ref, the assigned DAG, the budget
    ctx.claim/v1      "node N, attempt A: I am <host/model>, expect ~T turns, ~$C"
    ctx.handback/v1   "node N, attempt A: stopping — <reason>, <failure_kind>,
                       checkpoint: …, T turns, $C"
    ctx.steward/v1    the steward's decision on a handback and why
    ctx.verdict/v1    a verification result and its evidence address
    ctx.inbox/v1      one node telling another: an address, plus a bounded note

A handback is the row that turns collaboration into a loop. A node used to
have two exits, done or failed; now it has six, each a typed reason the
steward (:mod:`ctx.steward`) decides on rather than a crash to route around.

## Privacy

The route receipt (``route.jsonl``) is the export-safe artifact and carries no
task text; this ledger holds to the same rule. The goal lives in the store as a
blob and the ledger carries its address. A node's output lives behind its
checkpoint. ``failure_kind`` and ``reason`` are closed vocabularies. The only
free text anywhere is the optional inbox ``note``, bounded and sanitized, and
declared as such. A ledger that quoted an agent's output would stop being safe
to attach to a receipt, and this one is meant to be attached.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ctx.sessiondir import session_reads_path

TASK_SCHEMA = "ctx.task/v1"
CLAIM_SCHEMA = "ctx.claim/v1"
HANDBACK_SCHEMA = "ctx.handback/v1"
STEWARD_SCHEMA = "ctx.steward/v1"
VERDICT_SCHEMA = "ctx.verdict/v1"
INBOX_SCHEMA = "ctx.inbox/v1"

SCHEMAS = (
    TASK_SCHEMA, CLAIM_SCHEMA, HANDBACK_SCHEMA,
    STEWARD_SCHEMA, VERDICT_SCHEMA, INBOX_SCHEMA,
)

#: Why a node stopped. ``done`` is the only success; everything else is a
#: reason the steward reads. Closed, so a summary can count them and a policy
#: can branch on them without string-matching prose.
HANDBACK_REASONS = (
    "done", "failed", "blocked", "over_budget", "over_turns", "low_confidence",
    # A deliberate, successful stop: a frontier model planned, made one
    # validated edit, and handed off by design -- not a failure needing
    # recovery. See ctx.steward.de_escalation_target and docs/PREWALK.md.
    "prewalk_handoff",
)

#: The typed failure vocabulary the promoted recovery policy was evolved
#: against (``evals/alphaevolve/escalation_policy``), plus ``none`` for a clean
#: finish. The steward's classifier emits these; nothing else does.
FAILURE_KINDS = (
    "none",
    "permission_denied", "auth_failure", "safety_denied",
    "missing_evidence", "context_omission",
    "incomplete_contract", "verification_failure",
    "transient_transport", "rate_limited",
    "capability_limit", "repeated_incomplete",
    # How a node ran out of time, kept apart: "stalled" went silent for
    # idle_timeout (a stuck model), "wall_timeout" was still active when
    # node_timeout ran out (work too big for one node). Neither is a
    # transport blip, which is what a killed node used to be filed as.
    "stalled", "wall_timeout",
    "unknown",
)

#: What the steward can decide. Mirrors the recovery policy's action ids; the
#: steward offers only the subset that exists for the node in front of it.
STEWARD_ACTIONS = (
    "retry_same", "escalate", "replan", "stop_blocked", "stop_budget",
    "handoff_cheap",  # prewalk's de-escalation: the mirror of "escalate"
)

#: Bound on the one free-text field. Long enough to say what an address is
#: for, short enough that a note cannot become a transcript.
INBOX_NOTE_CHARS = 200
# An inbox ref is an ADDRESS: the first token must parse under the reference
# grammar (ctx.refs), and anything after it may only be `ctx get` options
# (`--lines 40:52@07407f1c`, `--hashlines`). Bounded so the ledger can never
# be used to smuggle content into a node's prompt.
INBOX_REF_CHARS = 256
INBOX_ID_CHARS = 64
_INBOX_FLAG_RE = re.compile(r"^--[a-z][a-z0-9-]*$")
_INBOX_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:@=/#,+~-]+$")
_INBOX_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class LedgerError(Exception):
    """A row that does not fit the closed contract. Raised, never swallowed:
    the ledger is the bus, and a message quietly dropped is a handoff lost."""


def new_task_id() -> str:
    """Time-ordered, collision-free without coordination. Sorts by creation."""
    return f"task-{time.time_ns():x}"


def ledger_path(workspace_root: Path | str, task_id: str) -> Path:
    _check_task_id(task_id)
    return session_reads_path(workspace_root, "tasks", f"{task_id}.jsonl")


def _check_task_id(task_id: str) -> None:
    if not task_id or not task_id.startswith("task-") or "/" in task_id or ".." in task_id:
        raise LedgerError(f"invalid task id {task_id!r}")


def _check(row: dict[str, Any]) -> None:
    schema = row.get("schema")
    if schema not in SCHEMAS:
        raise LedgerError(f"unknown ledger schema {schema!r}")
    _check_task_id(str(row.get("task_id") or ""))
    if schema == HANDBACK_SCHEMA:
        if row.get("reason") not in HANDBACK_REASONS:
            raise LedgerError(f"handback reason {row.get('reason')!r} not in {HANDBACK_REASONS}")
        if row.get("failure_kind") not in FAILURE_KINDS:
            raise LedgerError(f"failure_kind {row.get('failure_kind')!r} not in FAILURE_KINDS")
    if schema == STEWARD_SCHEMA and row.get("action") not in STEWARD_ACTIONS:
        raise LedgerError(f"steward action {row.get('action')!r} not in {STEWARD_ACTIONS}")
    if schema == INBOX_SCHEMA:
        _check_inbox(row)


def check_address(ref: Any) -> str:
    """Return ``ref`` if it is an address the receiving node can resolve with
    ``ctx get``; raise LedgerError otherwise. An address is one reference
    (``repo:path``, ``checkpoint:<id>``, ``run:<id>#stdout`` …) optionally
    followed by ``ctx get`` options. Prose, output and anything unbounded is
    refused here, before it reaches the ledger or a prompt."""
    from ctx.refs import RefError, parse_ref

    if not isinstance(ref, str) or not ref.strip():
        raise LedgerError("inbox row needs a ref (an address, never content)")
    if len(ref) > INBOX_REF_CHARS:
        raise LedgerError(f"inbox ref must be at most {INBOX_REF_CHARS} chars (an address, never content)")
    if ref != ref.strip() or any(ord(c) < 32 or ord(c) == 127 for c in ref):
        raise LedgerError("inbox ref must be a single line with no control characters")
    head, *rest = ref.split(" ")
    try:
        parse_ref(head)
    except RefError as e:
        raise LedgerError(f"inbox ref must be an address: {e}") from None
    expect_flag = True
    for token in rest:
        if not token:
            raise LedgerError("inbox ref options must be single-spaced")
        if _INBOX_FLAG_RE.match(token):
            expect_flag = False
            continue
        if expect_flag or not _INBOX_VALUE_RE.match(token):
            raise LedgerError(
                f"inbox ref may carry only `ctx get` options after the address, not {token!r}"
            )
        expect_flag = True
    return ref


def _check_inbox(row: dict[str, Any]) -> None:
    note = row.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > INBOX_NOTE_CHARS):
        raise LedgerError(f"inbox note must be a string of at most {INBOX_NOTE_CHARS} chars")
    for key in ("to", "from"):
        value = row.get(key)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > INBOX_ID_CHARS
            or not _INBOX_ID_RE.match(value)
        ):
            raise LedgerError(f"inbox {key!r} must be a node id of at most {INBOX_ID_CHARS} chars")
    check_address(row.get("ref"))


def append(workspace_root: Path | str, row: dict[str, Any]) -> dict[str, Any]:
    """Validate and append one row. Stamps ``ts``. Returns the stored row.

    Raises :class:`LedgerError` on a row outside the contract and lets I/O
    errors propagate. Callers that must never fail (the orchestrator's own
    bookkeeping) wrap this; the safe failure direction there is a missing
    claim or handback, which resume treats as "not done" and re-runs.

    The tail check and the write are one held ``flock`` critical section --
    the idiom already used by ``ctx.engagement._mutate_state`` and
    ``ctx.hook._ledger_charge`` -- so two separate OS processes, not just
    threads inside one orchestrator, can never interleave a write or race
    the torn-line check below. Locking is best-effort: a platform without
    ``fcntl`` falls back to the prior single-writer-only behavior.
    """
    _check(row)
    stored = dict(row)
    stored.setdefault("ts", time.time())
    path = ledger_path(workspace_root, str(row["task_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(stored, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        # A process killed mid-write leaves a torn last line with no
        # newline. The next append must not land on that same line -- it
        # would glue a valid row onto the fragment and lose BOTH to the
        # parser. Check the tail and start clean; the torn fragment is then
        # skipped by load() on its own. Held under the same lock as the
        # write below, so a live concurrent writer's in-progress row can
        # never be misread as a torn one.
        size = os.lseek(fd, 0, os.SEEK_END)
        if size > 0:
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                payload = b"\n" + payload
        os.lseek(fd, 0, os.SEEK_END)
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
    finally:
        os.close(fd)  # closing releases the flock
    return stored


def load(workspace_root: Path | str, task_id: str) -> list[dict[str, Any]]:
    """Every valid row for a task, in append order. Malformed lines are skipped
    (a torn write from a killed process must not poison the whole task)."""
    rows: list[dict[str, Any]] = []
    try:
        with ledger_path(workspace_root, task_id).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    doc = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(doc, dict) and doc.get("schema") in SCHEMAS:
                    rows.append(doc)
    except OSError:
        return []
    return rows


def list_tasks(workspace_root: Path | str) -> list[str]:
    """Task ids with a ledger, newest first (ids are time-ordered)."""
    root = session_reads_path(workspace_root, "tasks")
    try:
        names = [p.stem for p in root.iterdir() if p.suffix == ".jsonl" and p.stem.startswith("task-")]
    except OSError:
        return []
    return sorted(names, reverse=True)


# ------------------------------------------------------------ row builders
# One builder per schema, so every writer spells a row the same way and the
# validator above is the only place the contract is stated.

def task_row(
    task_id: str, *, goal_ref: str, nodes: list[dict[str, Any]], budget_usd: float,
    task_kind: str, source: str,
) -> dict[str, Any]:
    return {
        "schema": TASK_SCHEMA, "task_id": task_id, "goal_ref": goal_ref,
        "nodes": nodes, "budget_usd": float(budget_usd), "task_kind": task_kind,
        "source": source,
    }


def claim_row(
    task_id: str, node_id: str, *, attempt: int, host: str, model: str, tier: str,
    expected_turns: int, expected_cost_usd: float,
) -> dict[str, Any]:
    return {
        "schema": CLAIM_SCHEMA, "task_id": task_id, "node_id": node_id,
        "attempt": int(attempt), "host": host, "model": model, "tier": tier,
        "expected_turns": int(expected_turns),
        "expected_cost_usd": float(expected_cost_usd),
    }


def handback_row(
    task_id: str, node_id: str, *, attempt: int, reason: str, failure_kind: str,
    checkpoint: str | None, turns: int, cost_usd: float | None, tokens: int,
    exit_code: int | None, host: str, model: str,
) -> dict[str, Any]:
    return {
        "schema": HANDBACK_SCHEMA, "task_id": task_id, "node_id": node_id,
        "attempt": int(attempt), "reason": reason, "failure_kind": failure_kind,
        "checkpoint": checkpoint, "turns": int(turns),
        "cost_usd": (float(cost_usd) if cost_usd is not None else None),
        "tokens": int(tokens), "exit_code": exit_code, "host": host, "model": model,
    }


def steward_row(
    task_id: str, node_id: str, *, attempt: int, on_reason: str, failure_kind: str,
    action: str, target: str | None, budget_remaining_usd: float | None,
) -> dict[str, Any]:
    # None = unbounded budget. Not +inf: JSON has no infinity, and a row that
    # cannot round-trip through json.loads is a row resume cannot read.
    remaining = (
        None if budget_remaining_usd is None or budget_remaining_usd == float("inf")
        else float(budget_remaining_usd)
    )
    return {
        "schema": STEWARD_SCHEMA, "task_id": task_id, "node_id": node_id,
        "attempt": int(attempt), "on_reason": on_reason, "failure_kind": failure_kind,
        "action": action, "target": target, "budget_remaining_usd": remaining,
    }


def verdict_row(
    task_id: str, node_id: str, *, passed: bool, evidence_kind: str, ref: str | None,
) -> dict[str, Any]:
    return {
        "schema": VERDICT_SCHEMA, "task_id": task_id, "node_id": node_id,
        "passed": bool(passed), "evidence_kind": evidence_kind, "ref": ref,
    }


def inbox_row(
    task_id: str, *, to: str, sender: str, ref: str, note: str | None = None,
) -> dict[str, Any]:
    clean = None
    if note is not None:
        from ctx.textutil import strip_control

        clean = strip_control(str(note))[:INBOX_NOTE_CHARS]
    return {
        "schema": INBOX_SCHEMA, "task_id": task_id, "to": to, "from": sender,
        "ref": ref, "note": clean,
    }


# --------------------------------------------------------------- derivation
@dataclass
class NodeState:
    node_id: str
    attempts: int = 0
    last_claim: dict[str, Any] | None = None
    last_handback: dict[str, Any] | None = None
    checkpoint: str | None = None
    turns: int = 0
    cost_usd: float = 0.0
    cost_complete: bool = True

    @property
    def done(self) -> bool:
        return bool(self.last_handback and self.last_handback.get("reason") == "done")

    @property
    def open_claim(self) -> dict[str, Any] | None:
        """The claim still in flight: claimed, not yet handed back."""
        if not self.last_claim:
            return None
        claimed = int(self.last_claim.get("attempt") or 0)
        handed = int((self.last_handback or {}).get("attempt") or 0)
        return self.last_claim if claimed > handed else None

    @property
    def reserved_usd(self) -> float:
        """What an open claim expects to cost. Reserved against the budget so
        two nodes claiming in parallel cannot both spend the same dollar."""
        claim = self.open_claim
        return float(claim.get("expected_cost_usd") or 0.0) if claim else 0.0

    @property
    def status(self) -> str:
        if self.done:
            return "ok"
        if self.last_handback:
            return "failed"
        return "claimed" if self.last_claim else "pending"


@dataclass
class TaskState:
    task_id: str
    task: dict[str, Any] | None
    # Every ctx.task/v1 row in order: the opening row, then one per accepted
    # coordinator re-plan (source "replan") carrying only the nodes it added.
    task_rows: list[dict[str, Any]] = field(default_factory=list)
    nodes: dict[str, NodeState] = field(default_factory=dict)
    steward: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    inbox: list[dict[str, Any]] = field(default_factory=list)

    @property
    def budget_usd(self) -> float:
        return float((self.task or {}).get("budget_usd") or 0.0)

    @property
    def spent_usd(self) -> float:
        return sum(n.cost_usd for n in self.nodes.values())

    @property
    def cost_complete(self) -> bool:
        return all(n.cost_complete for n in self.nodes.values())

    @property
    def turns(self) -> int:
        return sum(n.turns for n in self.nodes.values())

    @property
    def reserved_usd(self) -> float:
        return sum(n.reserved_usd for n in self.nodes.values())

    @property
    def remaining_usd(self) -> float:
        """Budget left against ACTUALS, less what open claims have reserved.
        Unbounded (0) budgets report +inf so a policy comparing an action's
        cost against it never refuses on 0."""
        if self.budget_usd <= 0:
            return float("inf")
        return self.budget_usd - self.spent_usd - self.reserved_usd


def task_state(rows: Iterable[dict[str, Any]]) -> TaskState:
    """Fold the ledger into the current state of the collaboration.

    Pure over the rows: same ledger, same state, on any machine. This is what
    resume reads, what ``ctx task show`` renders, and what the steward consults
    for budget-against-actuals. Cost is summed from handbacks; a handback with
    no cost marks the node's cost incomplete rather than counting as zero — a
    missing observation is not a free node.
    """
    rows = list(rows)
    task_id = next((str(r["task_id"]) for r in rows if r.get("task_id")), "")
    state = TaskState(task_id=task_id, task=None)
    for r in rows:
        schema = r.get("schema")
        if schema == TASK_SCHEMA:
            if state.task is None:
                state.task = r
            state.task_rows.append(r)
            for n in r.get("nodes") or []:
                nid = str(n.get("id") or "")
                if nid:
                    state.nodes.setdefault(nid, NodeState(nid))
            continue
        nid = str(r.get("node_id") or r.get("to") or "")
        node = state.nodes.setdefault(nid, NodeState(nid)) if nid else None
        if schema == CLAIM_SCHEMA and node:
            node.attempts = max(node.attempts, int(r.get("attempt") or 0))
            node.last_claim = r
        elif schema == HANDBACK_SCHEMA and node:
            node.last_handback = r
            if r.get("checkpoint"):
                node.checkpoint = str(r["checkpoint"])
            node.turns += int(r.get("turns") or 0)
            if r.get("cost_usd") is None:
                node.cost_complete = False
            else:
                node.cost_usd += float(r["cost_usd"])
        elif schema == STEWARD_SCHEMA:
            state.steward.append(r)
        elif schema == VERDICT_SCHEMA:
            state.verdicts.append(r)
        elif schema == INBOX_SCHEMA:
            state.inbox.append(r)
    return state


def inbox_for(state: TaskState, node_id: str) -> list[dict[str, Any]]:
    """Messages addressed to a node, in order. Addresses, never content."""
    return [m for m in state.inbox if m.get("to") == node_id]


def render_task(state: TaskState) -> str:
    """The bounded human/agent view. Every line is an address or a number."""
    t = state.task or {}
    lines = [f"[ctx task {state.task_id}]"]
    if t:
        lines.append(
            f"goal: {t.get('goal_ref')} · kind {t.get('task_kind')} · source {t.get('source')} · "
            f"budget ${state.budget_usd:.2f}" if state.budget_usd > 0 else
            f"goal: {t.get('goal_ref')} · kind {t.get('task_kind')} · source {t.get('source')} · budget unbounded"
        )
    spent = f"${state.spent_usd:.4f}" + ("" if state.cost_complete else " (partial)")
    lines.append(f"spent: {spent} · turns: {state.turns} · nodes: {len(state.nodes)}")
    for nid, n in state.nodes.items():
        hb = n.last_handback or {}
        who = f"{hb.get('host')}/{hb.get('model')}" if hb else (
            f"{n.last_claim.get('host')}/{n.last_claim.get('model')}" if n.last_claim else "-"
        )
        tail = ""
        if hb and hb.get("reason") != "done":
            tail = f" · {hb.get('reason')}/{hb.get('failure_kind')}"
        cp = f" · {n.checkpoint}" if n.checkpoint else ""
        lines.append(f"  {nid:<12} {n.status:<8} {who} · attempts {n.attempts} · turns {n.turns}{tail}{cp}")
    for s in state.steward:
        lines.append(
            f"  steward: {s.get('node_id')}#{s.get('attempt')} on {s.get('on_reason')}/"
            f"{s.get('failure_kind')} → {s.get('action')}"
            + (f" ({s.get('target')})" if s.get("target") else "")
        )
    for m in state.inbox:
        note = f" — {m['note']}" if m.get("note") else ""
        lines.append(f"  inbox → {m.get('to')} from {m.get('from')}: {m.get('ref')}{note}")
    if state.verdicts:
        for v in state.verdicts:
            lines.append(
                f"  verdict: {v.get('node_id')} {'pass' if v.get('passed') else 'FAIL'} "
                f"({v.get('evidence_kind')}) {v.get('ref') or ''}"
            )
    return "\n".join(lines)


__all__ = [
    "TASK_SCHEMA", "CLAIM_SCHEMA", "HANDBACK_SCHEMA", "STEWARD_SCHEMA",
    "VERDICT_SCHEMA", "INBOX_SCHEMA", "SCHEMAS",
    "HANDBACK_REASONS", "FAILURE_KINDS", "STEWARD_ACTIONS", "INBOX_NOTE_CHARS",
    "INBOX_REF_CHARS", "check_address",
    "LedgerError", "new_task_id", "ledger_path", "append", "load", "list_tasks",
    "task_row", "claim_row", "handback_row", "steward_row", "verdict_row", "inbox_row",
    "NodeState", "TaskState", "task_state", "inbox_for", "render_task",
]
