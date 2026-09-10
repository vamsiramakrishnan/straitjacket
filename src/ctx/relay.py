"""The cross-harness relay: how one harness reaches another between turns.

Every ctx→harness data flow in this project is a *return value*. The harness
calls in — a hook subprocess, an MCP tool call, a CLI invocation — and ctx
answers on that same call. Nothing in the system can start a conversation.
:mod:`ctx.taskledger` made harnesses share a *record*; it did not give them a
way to be *told*. Three consequences, all of them things people actually hit:

* ``ctx run --bg`` spawns a supervisor and hands back a job handle. When the
  job finishes, nothing says so. The agent must remember to poll ``ctx job``,
  and an agent that forgets simply never learns the build broke.
* Two harnesses on one workspace — the common shape once ``ctx orchestrate``
  is in play — cannot tell each other anything. The second one rediscovers
  what the first already paid for.
* Nothing can stop a harness that is confidently walking into a wall somebody
  else already found.

This module is the missing direction, built to the same rules as the ledger:
append-only, schema-versioned, addresses instead of content, one file under
the workspace's own bookkeeping directory.

## What it is honest about

There is no push. ctx cannot interrupt a model mid-stream on any hook host,
and :mod:`ctx.stream_rules` says so plainly; this module does not quietly
claim otherwise. What it does is convert *polling* into *delivery at the next
boundary the harness already crosses*:

===================  ==========================================================
delivery point       what drains there
===================  ==========================================================
``session-start``    everything pending, as session advisory context
``pre-invocation``   everything pending (Antigravity: before every model call)
``post-tool-use``    ``report`` and ``advise`` signals, as additional context
``pre-tool-use``     ``interrupt`` signals, as a deny/ask on the next tool call
===================  ==========================================================

So the latency of a relay signal is **one hook boundary** — the next tool call
or the next turn — and an interrupt lands at a **tool-call boundary**, not
mid-token. That is a real capability with a real bound, and the bound is part
of the contract rather than a footnote. A host that does own its stream (an
ACP worker, the SDK-backed runner) can drain the same queue earlier without
changing anything here.

## The rows

``ctx.watch/v1``     a subscription: who wants to hear about what
``ctx.signal/v1``    a queued delivery: an address, a bounded note, a deadline
``ctx.delivery/v1``  the receipt: which signal reached whom, where, when

Delivery receipts are what make draining exactly-once without a mutable
cursor: a signal is pending while no delivery row exists for it. Matching on
the *signal* rather than on the subscriber string is deliberate — ``claude``
and ``claude:s1`` are two addresses for the same reader, so a per-string key
would re-deliver every host-addressed signal to the next session of that host.
A broadcast to ``*`` is the one case that genuinely needs a copy each, and it
keeps the per-subscriber key. The queue stays append-only, and "what did the
other harness actually see, and when" is answerable after the fact — which is
the same reason the task ledger is shaped this way.

## Privacy and the address rule

A signal carries a **ref** and an optional **note**, and nothing else that a
model will read. The ref is validated by :func:`ctx.taskledger.check_address`
— the same closed grammar the inbox uses, one reference plus ``ctx get``
options — so the relay cannot become a channel for smuggling output into
another harness's prompt. The note is bounded to
:data:`~ctx.taskledger.INBOX_NOTE_CHARS` and sanitized to one line. The
receiving agent resolves the address itself, under its own permissions, and
pays for exactly the bytes it chooses to read.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from ctx.sessiondir import session_reads_path
from ctx.taskledger import INBOX_NOTE_CHARS, LedgerError, check_address

WATCH_SCHEMA = "ctx.watch/v1"
SIGNAL_SCHEMA = "ctx.signal/v1"
DELIVERY_SCHEMA = "ctx.delivery/v1"
SCHEMAS = (WATCH_SCHEMA, SIGNAL_SCHEMA, DELIVERY_SCHEMA)

#: What a signal asks the receiving harness to do. Closed, so a hook can
#: branch on it without string-matching prose, and so a future kind is a
#: deliberate contract change rather than a new free-text value.
#:
#: ``report``    something you were waiting on finished. Advisory.
#: ``advise``    a peer found something worth your attention. Advisory.
#: ``interrupt`` stop what you are doing and read this before the next tool
#:               call. The only kind that can deny.
SIGNAL_KINDS = ("report", "advise", "interrupt")

#: What a signal is *about*. A watch subscribes to a topic and an optional
#: selector (a job id, a task id, a path prefix); the topic is what makes a
#: subscription cheap to match without parsing the ref.
TOPICS = ("job", "task", "digest", "edit", "peer")

#: The hook stages that may drain. Kept here rather than in :mod:`ctx.hook`
#: so the queue and its delivery points version together.
STAGES = (
    "session-start", "pre-invocation", "pre-tool-use", "post-tool-use",
    # The ACP transport's own two delivery points. Named rather than folded
    # into the hook stages so a delivery receipt says where a signal actually
    # landed: an ACP worker has no hooks, and "session-start" would be a lie.
    "acp-prompt", "acp-cancel",
    "cli",
)

#: Kinds each stage is allowed to deliver. ``pre-tool-use`` sees only
#: interrupts: it is the stage that can *deny*, and turning an advisory
#: report into a denied tool call would make the relay a hazard rather than a
#: channel.
STAGE_KINDS: dict[str, tuple[str, ...]] = {
    "session-start": ("report", "advise", "interrupt"),
    "pre-invocation": ("report", "advise", "interrupt"),
    "post-tool-use": ("report", "advise"),
    "pre-tool-use": ("interrupt",),
    # An ACP worker's prompt carries advisories only. Folding an interrupt in
    # there would demote a stop into a suggestion the model may ignore — and
    # would consume it, so the cancel that should have fired never would.
    "acp-prompt": ("report", "advise"),
    "acp-cancel": ("interrupt",),
    "cli": SIGNAL_KINDS,
}

#: Bounds. A relay that can grow without limit is a context leak with extra
#: steps, so every one of these is enforced on write *and* on render.
MAX_PENDING_PER_SUBSCRIBER = 32
MAX_SIGNALS_PER_DRAIN = 8
MAX_RENDER_CHARS = 1400
SUBSCRIBER_CHARS = 96
SELECTOR_CHARS = 128
DEFAULT_TTL_SECONDS = 6 * 3600

_SUBSCRIBER_RE = re.compile(r"^[A-Za-z0-9_.:@*-]{1,%d}$" % SUBSCRIBER_CHARS)
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_.:/@*#-]{0,%d}$" % SELECTOR_CHARS)


class RelayError(LedgerError):
    """A row outside the relay's closed contract.

    Subclasses :class:`~ctx.taskledger.LedgerError` on purpose: a caller that
    already handles ledger contract violations handles these too, and the
    relay is the same kind of bus with the same failure direction.
    """


def _check_ref(ref: Any) -> str:
    """:func:`ctx.taskledger.check_address`, re-raised as a relay error.

    The grammar is deliberately the ledger's — one address plus ``ctx get``
    options, nothing else — because the two buses must not drift into
    accepting different things. Only the exception type is the relay's, so a
    caller that handles ``RelayError`` handles every way a relay write can be
    refused.
    """
    try:
        return check_address(ref)
    except LedgerError as e:
        raise RelayError(str(e)) from None



# --------------------------------------------------------------------------
# identity


def new_signal_id() -> str:
    """Time-ordered and collision-free without coordination, like task ids."""
    return f"sig-{time.time_ns():x}"


def new_watch_id() -> str:
    return f"watch-{time.time_ns():x}"


def subscriber_id(host: str, session: str | None = None) -> str:
    """The address of one harness, optionally one of its sessions.

    ``claude`` addresses every Claude Code session on this workspace;
    ``claude:0f1e2d`` addresses one. A signal sent to the host reaches
    whichever session drains first, which is what you want for "the build
    finished" and not what you want for "answer the question you asked".
    """
    host = (host or "").strip().lower()
    if not host:
        raise RelayError("subscriber needs a host")
    ident = host if not session else f"{host}:{str(session)[:32]}"
    if not _SUBSCRIBER_RE.match(ident):
        raise RelayError(f"invalid subscriber id {ident!r}")
    return ident


def _addresses(subscriber: str) -> tuple[str, ...]:
    """Every address a subscriber answers to: itself, its host, and ``*``."""
    host = subscriber.split(":", 1)[0]
    return tuple(dict.fromkeys((subscriber, host, "*")))


# --------------------------------------------------------------------------
# storage


def relay_path(workspace_root: Path | str) -> Path:
    return session_reads_path(workspace_root, "relay", "relay.jsonl")


def _check(row: dict[str, Any]) -> None:
    schema = row.get("schema")
    if schema not in SCHEMAS:
        raise RelayError(f"unknown relay schema {schema!r}")

    if schema == WATCH_SCHEMA:
        if row.get("topic") not in TOPICS:
            raise RelayError(f"watch topic {row.get('topic')!r} not in {TOPICS}")
        if row.get("action") not in SIGNAL_KINDS:
            raise RelayError(f"watch action {row.get('action')!r} not in {SIGNAL_KINDS}")
        _check_subscriber(row.get("subscriber"))
        _check_selector(row.get("selector"))

    elif schema == SIGNAL_SCHEMA:
        if row.get("kind") not in SIGNAL_KINDS:
            raise RelayError(f"signal kind {row.get('kind')!r} not in {SIGNAL_KINDS}")
        if row.get("topic") not in TOPICS:
            raise RelayError(f"signal topic {row.get('topic')!r} not in {TOPICS}")
        _check_subscriber(row.get("to"))
        _check_subscriber(row.get("origin"))
        _check_selector(row.get("selector"))
        # The address rule, enforced at the boundary rather than at render:
        # a ref that is not an address must never reach the file, because
        # every reader downstream would then have to re-validate it.
        _check_ref(row.get("ref"))
        note = row.get("note")
        if note is not None and (
            not isinstance(note, str) or len(note) > INBOX_NOTE_CHARS
        ):
            raise RelayError(f"note must be a string of at most {INBOX_NOTE_CHARS} chars")
        if not isinstance(row.get("expires_at"), (int, float)):
            raise RelayError("signal needs an expires_at")

    elif schema == DELIVERY_SCHEMA:
        _check_subscriber(row.get("subscriber"))
        if row.get("stage") not in STAGES:
            raise RelayError(f"delivery stage {row.get('stage')!r} not in {STAGES}")
        if not isinstance(row.get("signal_id"), str) or not row["signal_id"]:
            raise RelayError("delivery needs a signal_id")


def _check_subscriber(value: Any) -> None:
    if not isinstance(value, str) or not _SUBSCRIBER_RE.match(value):
        raise RelayError(f"invalid subscriber id {value!r}")


def _check_selector(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _SELECTOR_RE.match(value):
        raise RelayError(f"invalid selector {value!r}")


def _sanitize_note(note: str | None) -> str:
    """One line, bounded, no control characters. The only free text here."""
    if not note:
        return ""
    flat = " ".join(str(note).split())
    flat = "".join(c for c in flat if ord(c) >= 32 and ord(c) != 127)
    return flat[:INBOX_NOTE_CHARS]


def append(workspace_root: Path | str, row: dict[str, Any]) -> dict[str, Any]:
    """Validate and append one row under an exclusive lock.

    Same critical section and torn-line repair as
    :func:`ctx.taskledger.append`: the relay is written by hook subprocesses
    that several harnesses may run at the same moment, so two OS processes
    interleaving a write is the expected case, not the exotic one.
    """
    _check(row)
    stored = dict(row)
    stored.setdefault("ts", time.time())
    path = relay_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(stored, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
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
        os.close(fd)
    return stored


def load(workspace_root: Path | str) -> list[dict[str, Any]]:
    """Every valid row, in append order. Torn lines are skipped, never fatal."""
    rows: list[dict[str, Any]] = []
    try:
        with relay_path(workspace_root).open(encoding="utf-8") as handle:
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


# --------------------------------------------------------------------------
# subscriptions


def watch(
    workspace_root: Path | str,
    *,
    subscriber: str,
    topic: str,
    selector: str = "",
    action: str = "advise",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Register interest. Returns the stored watch row.

    ``selector`` narrows within a topic and is matched by prefix, so a watch
    on ``("job", "")`` hears about every job and one on ``("job", "job-1a2b")``
    hears about one. ``action`` is the kind the resulting signal carries: a
    watch that asks for ``interrupt`` is asking to be *stopped*, not merely
    told, and only the pre-tool-use stage will deliver it.
    """
    _check_subscriber(subscriber)
    row = {
        "schema": WATCH_SCHEMA,
        "watch_id": new_watch_id(),
        "subscriber": subscriber,
        "topic": topic,
        "selector": selector or "",
        "action": action,
        "expires_at": time.time() + max(60.0, float(ttl_seconds)),
    }
    return append(workspace_root, row)


def unwatch(workspace_root: Path | str, watch_id: str) -> bool:
    """Retire a watch by expiring it. Append-only: nothing is ever rewritten."""
    for row in reversed(load(workspace_root)):
        if row.get("schema") == WATCH_SCHEMA and row.get("watch_id") == watch_id:
            append(
                workspace_root,
                {**row, "watch_id": watch_id, "expires_at": 0.0, "retired": True},
            )
            return True
    return False


def active_watches(workspace_root: Path | str, now: float | None = None) -> list[dict]:
    """Live watches, last write per ``watch_id`` winning."""
    now = time.time() if now is None else now
    latest: dict[str, dict] = {}
    for row in load(workspace_root):
        if row.get("schema") == WATCH_SCHEMA:
            latest[str(row.get("watch_id"))] = row
    return [w for w in latest.values() if float(w.get("expires_at", 0)) > now]


# --------------------------------------------------------------------------
# publishing


def publish(
    workspace_root: Path | str,
    *,
    topic: str,
    ref: str,
    origin: str,
    selector: str = "",
    note: str = "",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    to: str | None = None,
    exclude: str | None = None,
) -> list[dict[str, Any]]:
    """Announce something. Returns the signals actually queued.

    With ``to``, this is a direct message to one subscriber. Without it, the
    relay fans the announcement out to every matching watch — and to nobody
    if no one is listening, which is the point: publishing is cheap and
    silent, so a producer never needs to know who cares.

    A publisher never chooses the kind. The *subscriber* declared what it
    wanted when it registered the watch, so the same job completion can be an
    advisory report to one harness and an interrupt to another.

    ``exclude`` drops the producer from its own fan-out, matched on host so a
    sibling session of the same harness is dropped too. Without it, a harness
    watching ``digest`` is told about every expensive capture it just made
    itself, which is noise dressed as collaboration.
    """
    if topic not in TOPICS:
        raise RelayError(f"topic {topic!r} not in {TOPICS}")
    _check_ref(ref)
    note = _sanitize_note(note)
    now = time.time()
    expires = now + max(60.0, float(ttl_seconds))

    targets: list[tuple[str, str]] = []
    if to is not None:
        _check_subscriber(to)
        targets.append((to, "advise"))
    else:
        skip_host = (exclude or "").split(":", 1)[0].strip().lower() or None
        for w in active_watches(workspace_root, now):
            if w.get("topic") != topic:
                continue
            sel = str(w.get("selector") or "")
            if sel and not str(selector or "").startswith(sel):
                continue
            subscriber = str(w["subscriber"])
            if skip_host and subscriber.split(":", 1)[0].lower() == skip_host:
                continue
            targets.append((subscriber, str(w.get("action") or "advise")))

    queued: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for subscriber, kind in targets:
        if (subscriber, kind) in seen:
            continue
        seen.add((subscriber, kind))
        if len(pending(workspace_root, subscriber, now=now)) >= MAX_PENDING_PER_SUBSCRIBER:
            # A subscriber that never drains must not be able to grow the
            # queue without bound. Dropping the newest is the safe
            # direction: the backlog it already has says the same thing.
            continue
        queued.append(
            append(
                workspace_root,
                {
                    "schema": SIGNAL_SCHEMA,
                    "signal_id": new_signal_id(),
                    "kind": kind,
                    "topic": topic,
                    "selector": selector or "",
                    "to": subscriber,
                    "origin": origin,
                    "ref": ref,
                    "note": note,
                    "expires_at": expires,
                },
            )
        )
    return queued


def interrupt(
    workspace_root: Path | str,
    *,
    to: str,
    ref: str,
    origin: str,
    note: str = "",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Ask a harness to stop at its next tool call and read ``ref`` first.

    This is the honest form of interruption available across hook hosts: the
    signal is delivered at the next ``pre-tool-use``, where the guard already
    has the authority to deny or force an ask. It cannot stop a model
    mid-sentence, and nothing here pretends it can.
    """
    _check_subscriber(to)
    _check_ref(ref)
    return append(
        workspace_root,
        {
            "schema": SIGNAL_SCHEMA,
            "signal_id": new_signal_id(),
            "kind": "interrupt",
            "topic": "peer",
            "selector": "",
            "to": to,
            "origin": origin,
            "ref": ref,
            "note": _sanitize_note(note),
            "expires_at": time.time() + max(60.0, float(ttl_seconds)),
        },
    )


# --------------------------------------------------------------------------
# draining


def pending(
    workspace_root: Path | str,
    subscriber: str,
    *,
    kinds: Iterable[str] | None = None,
    now: float | None = None,
    rows: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Undelivered, unexpired signals addressed to this subscriber.

    Exactly-once falls out of the delivery receipts: a signal is pending
    while no ``ctx.delivery/v1`` row pairs it with this subscriber. No
    mutable cursor, so two processes draining at once cannot lose a signal
    between them — at worst one is delivered twice, and a duplicate advisory
    is a far better failure than a dropped one.
    """
    now = time.time() if now is None else now
    rows = load(workspace_root) if rows is None else rows
    mine = set(_addresses(subscriber))
    want = set(kinds) if kinds is not None else set(SIGNAL_KINDS)

    # Delivery is matched on the signal, not on the exact subscriber string.
    # `claude` and `claude:s1` are two addresses for the same reader, so
    # keying exactly-once on the string re-delivered every host-addressed
    # signal to the next session of that host — "the build finished" arriving
    # once per session is precisely the noise this queue exists to avoid.
    # A broadcast to ``*`` is the one case where each subscriber genuinely
    # needs its own copy, so it keeps the per-subscriber key.
    delivered_any: set[str] = set()
    delivered_by: set[tuple[str, str]] = set()
    for r in rows:
        if r.get("schema") == DELIVERY_SCHEMA:
            delivered_any.add(str(r.get("signal_id")))
            delivered_by.add((str(r.get("signal_id")), str(r.get("subscriber"))))

    out = []
    for r in rows:
        if r.get("schema") != SIGNAL_SCHEMA:
            continue
        to = str(r.get("to"))
        if to not in mine or str(r.get("kind")) not in want:
            continue
        if float(r.get("expires_at", 0)) <= now:
            continue
        sid = str(r.get("signal_id"))
        seen = (sid, subscriber) in delivered_by if to == "*" else sid in delivered_any
        if seen:
            continue
        out.append(r)
    out.sort(key=lambda r: (r.get("kind") != "interrupt", r.get("ts", 0)))
    return out


def render(signals: list[dict[str, Any]], *, max_chars: int = MAX_RENDER_CHARS) -> str:
    """The bounded advisory a harness actually sees.

    Deterministic and address-shaped, exactly like a digest: each line names
    who sent it, what it is about, and the address to resolve. The receiving
    agent spends tokens only on what it decides to fetch.
    """
    if not signals:
        return ""
    verb = {"report": "finished", "advise": "found", "interrupt": "STOP"}
    lines = [f"[ctx relay · {len(signals)} signal{'s' if len(signals) != 1 else ''}]"]
    for s in signals:
        kind = str(s.get("kind"))
        head = (
            f"  {verb.get(kind, kind)} · {s.get('topic')}"
            f"{'/' + str(s.get('selector')) if s.get('selector') else ''}"
            f" · from {s.get('origin')}"
        )
        lines.append(head)
        lines.append(f"    resolve: ctx get {s.get('ref')}")
        if s.get("note"):
            lines.append(f"    note: {s['note']}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20].rstrip() + "\n    … truncated"
    return text


def drain(
    workspace_root: Path | str,
    subscriber: str,
    *,
    stage: str,
    limit: int = MAX_SIGNALS_PER_DRAIN,
    max_chars: int = MAX_RENDER_CHARS,
    record: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    """Take up to ``limit`` signals this stage may deliver, and render them.

    Returns ``(rendered_text, signals)``. With ``record`` (the default) a
    delivery receipt is appended for each, so the next drain will not repeat
    them. Callers that only want to *look* — a status command, a dry run —
    pass ``record=False``.
    """
    if stage not in STAGES:
        raise RelayError(f"stage {stage!r} not in {STAGES}")
    signals = pending(workspace_root, subscriber, kinds=STAGE_KINDS[stage])[:limit]
    if not signals:
        return "", []
    if record:
        for s in signals:
            append(
                workspace_root,
                {
                    "schema": DELIVERY_SCHEMA,
                    "signal_id": str(s.get("signal_id")),
                    "subscriber": subscriber,
                    "stage": stage,
                },
            )
    return render(signals, max_chars=max_chars), signals


def pending_interrupt(
    workspace_root: Path | str, subscriber: str
) -> dict[str, Any] | None:
    """The oldest undelivered interrupt for this subscriber, without taking it.

    Peeks rather than drains: the caller that acts on it (stopping a worker)
    is not the same step as the caller that reports it, and a signal consumed
    by a liveness check that then fails to act would be a stop nobody hears.
    """
    signals = pending(workspace_root, subscriber, kinds=("interrupt",))
    return signals[0] if signals else None


def interrupt_watcher(
    workspace_root: Path | str,
    subscriber: str,
    *,
    poll_seconds: float = 1.0,
    chain: Any = None,
):
    """A zero-argument predicate that turns true once an interrupt is queued.

    Shaped for a transport that already owns its worker and already polls a
    cancellation source — :class:`ctx.acp.Client` calls its ``cancelled`` hook
    on every wait iteration, roughly ten times a second. Three properties
    matter there and none of them are optional:

    * **Throttled.** Re-reading and re-parsing the queue at 10 Hz would make
      an advisory channel the most expensive thing in the loop. The queue is
      consulted at most every ``poll_seconds``, and not at all until the file
      exists.
    * **Latching.** Once true it stays true. The transport polls again while
      tearing down, and a predicate that flickered back to false would leave
      a worker half-cancelled.
    * **Chaining.** ``chain`` composes with the caller's own cancellation
      source, so adding the relay never removes a budget or a task cancel.

    Never raises: a broken queue means "no interrupt", exactly as everywhere
    else in this module.
    """
    state = {"hit": False, "checked": 0.0}

    def cancelled() -> bool:
        if chain is not None:
            try:
                if chain():
                    return True
            except Exception:  # noqa: BLE001 — the caller's source, not ours
                pass
        if state["hit"]:
            return True
        now = time.monotonic()
        if now - state["checked"] < poll_seconds:
            return False
        state["checked"] = now
        try:
            if not relay_path(workspace_root).exists():
                return False
            state["hit"] = pending_interrupt(workspace_root, subscriber) is not None
        except Exception:  # noqa: BLE001 — advisory, never fatal
            return False
        return state["hit"]

    return cancelled


def drain_quietly(
    workspace_root: Path | str, subscriber: str, *, stage: str
) -> tuple[str, list[dict[str, Any]]]:
    """:func:`drain` that can never raise.

    The hook calls this. A relay that is corrupt, unwritable, or on a
    read-only checkout must degrade to "no signals" — never to a failed tool
    call. The relay is an enhancement to the loop; it is not allowed to
    become a new way for the loop to break.
    """
    try:
        return drain(workspace_root, subscriber, stage=stage)
    except Exception:  # noqa: BLE001 — advisory channel, never fatal
        return "", []


# --------------------------------------------------------------------------
# reporting


def status(workspace_root: Path | str, now: float | None = None) -> dict[str, Any]:
    """What is queued, who is listening, and what has already been delivered."""
    now = time.time() if now is None else now
    rows = load(workspace_root)
    signals = [r for r in rows if r.get("schema") == SIGNAL_SCHEMA]
    deliveries = [r for r in rows if r.get("schema") == DELIVERY_SCHEMA]
    delivered_ids = {str(r.get("signal_id")) for r in deliveries}

    by_subscriber: dict[str, int] = {}
    for s in signals:
        if str(s.get("signal_id")) in delivered_ids:
            continue
        if float(s.get("expires_at", 0)) <= now:
            continue
        by_subscriber[str(s.get("to"))] = by_subscriber.get(str(s.get("to")), 0) + 1

    return {
        "path": str(relay_path(workspace_root)),
        "watches": active_watches(workspace_root, now),
        "signals": len(signals),
        "delivered": len(deliveries),
        "expired": sum(
            1
            for s in signals
            if float(s.get("expires_at", 0)) <= now
            and str(s.get("signal_id")) not in delivered_ids
        ),
        "pending_by_subscriber": by_subscriber,
    }


def gc(workspace_root: Path | str, *, keep_seconds: float = 7 * 24 * 3600) -> int:
    """Compact the relay, dropping rows that can no longer affect a drain.

    A delivered or expired signal is history the store already holds through
    its ref; the relay is a queue, not an archive. Returns the number of rows
    dropped. Rewrite-in-place under the same lock, so a concurrent append
    cannot be lost.
    """
    now = time.time()
    path = relay_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        return _compact_locked(workspace_root, path, now, keep_seconds)
    finally:
        os.close(fd)


def _compact_locked(
    workspace_root: Path | str, path: Path, now: float, keep_seconds: float
) -> int:
    """The read-decide-rewrite, all of it inside the caller's lock.

    Snapshotting before taking the lock is how a publisher's append between
    the two silently vanished: `os.replace` wrote back rows that predated it.
    The docstring above always claimed this ran under one lock; now it does.
    """
    rows = load(workspace_root)
    delivered = {
        str(r.get("signal_id")) for r in rows if r.get("schema") == DELIVERY_SCHEMA
    }
    keep: list[dict] = []
    for r in rows:
        age = now - float(r.get("ts", now))
        if r.get("schema") == SIGNAL_SCHEMA:
            done = str(r.get("signal_id")) in delivered or float(
                r.get("expires_at", 0)
            ) <= now
            if done and age > keep_seconds:
                continue
        elif r.get("schema") == DELIVERY_SCHEMA and age > keep_seconds:
            continue
        elif r.get("schema") == WATCH_SCHEMA and float(r.get("expires_at", 0)) <= now:
            if age > keep_seconds:
                continue
        keep.append(r)

    dropped = len(rows) - len(keep)
    if not dropped:
        return 0
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in keep), encoding="utf-8"
    )
    os.replace(tmp, path)
    return dropped
