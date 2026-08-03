"""``ctx ask`` — intents as typed plan presets (docs/ASK.md, M-L).

The Ponytail principle applied to retrieval: compress INTENT, not
English. An intent here is a frozen ``ctx.plan/v1`` template with typed
slots — deterministic, canonical-JSON stable (same slots ⇒ same plan id
⇒ node-cache hits), validated by the shipped plan validator, executed by
the shipped executor, rendered as the shipped investigate digest. There
is deliberately NO natural-language parser: the subject comes from
``--symbol``/``--run``, or from the question text only in the one
provably-unambiguous case (exactly one identifier-shaped token), always
disclosed. Everything else is a teaching error that SUGGESTS an intent
and never acts on the guess — advisory, disclosed, total.

What each intent guarantees (the contract, enforced by the plan shape):

* every intent is **observe-class end to end** — ``diagnose`` reads the
  captured failure facts (``evidence.failures``), it never reruns tests;
* counterevidence is structural, not optional — ``diagnose`` carries the
  ``untouched_failures`` join; the investigate renderer prints the
  section even when empty (anti-anchoring, plan_exec);
* materialization is terminal — the only ``text``-emitting node is
  ``code.context``, so bytes enter exactly once, at the end.
"""

from __future__ import annotations

from ctx import bounds

import difflib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable


class AskError(Exception):
    """One-line teaching error: what is missing, and the exact flag that
    supplies it (the q QueryError shape)."""


@dataclass(frozen=True)
class Intent:
    name: str
    doc: str
    needs_symbol: bool
    compile: Callable[..., dict]
    klass: str = "observe"  # observe | execute (execute runs tests → CLI-only)
    needs_refs: int = 0  # compare needs two run refs
    default_command: str | None = None  # verify/review test command


# ------------------------------------------------------------------ presets
def _locate(*, symbol: str, question: str, run=None, depth=None, **_) -> dict[str, Any]:
    """Where is X defined and used? refs → warmed symbol rows → per-file
    census → the definitions' exact bodies (terminal)."""
    return {
        "version": "ctx.plan/v1",
        "objective": {"kind": "locate", "question": question},
        "budget": {"wall_seconds": 60},
        # locate has no root-cause candidates and no counterevidence: render
        # coverage only, so the digest never emits diagnose-shaped "0
        # conclusion candidates" noise for a where-is question.
        "emit": {"sections": ["coverage"]},
        "steps": [
            {"id": "refs", "op": "code.refs", "args": {"symbol": symbol}},
            {"id": "defs", "op": "code.symbols", "input": "refs",
             "args": {"symbol": symbol}},
            {"id": "by_file", "op": "evidence.group", "input": "refs",
             "args": {"field": "file"}},
            {"id": "context", "op": "code.context", "input": "defs",
             "args": {"cap": 2}},
        ],
    }


def _impact(*, symbol: str, question: str, run=None, depth=None, **_) -> dict[str, Any]:
    """What could break if X changes? direct callers → bounded blast
    radius → the tests that plausibly cover it → what already changed."""
    return {
        "version": "ctx.plan/v1",
        "objective": {"kind": "impact", "question": question},
        "budget": {"wall_seconds": 90},
        # impact is a blast-radius census, not a diagnosis: coverage only.
        "emit": {"sections": ["coverage"]},
        "steps": [
            {"id": "callers", "op": "code.callers", "args": {"symbol": symbol}},
            {"id": "blast", "op": "code.impact",
             "args": {"symbol": symbol, "depth": int(bounds.explicit(depth, 3))}},
            {"id": "tests", "op": "code.related_tests", "input": "blast"},
            {"id": "changes", "op": "repo.changed"},
        ],
    }


def _diagnose(*, question: str, symbol=None, run=None, depth=None, **_) -> dict[str, Any]:
    """What explains the captured failures? changes × failures → the
    root-cause join, its counterevidence, and the failing frames' exact
    context. Never reruns tests — freshness is declared, not assumed."""
    run_args: dict[str, Any] = {"run": run} if run else {}
    return {
        "version": "ctx.plan/v1",
        "objective": {"kind": "diagnose", "question": question},
        "budget": {"wall_seconds": 90},
        "steps": [
            {"id": "changes", "op": "repo.changed"},
            {"id": "fails", "op": "evidence.failures", "args": dict(run_args)},
            {"id": "culprits", "op": "evidence.join",
             "args": {"on": "failing_in_changed", **run_args},
             "after": ["fails", "changes"]},
            {"id": "counter", "op": "evidence.join",
             "args": {"on": "untouched_failures", **run_args},
             "after": ["fails", "changes"]},
            {"id": "context", "op": "code.context", "input": "fails",
             "args": {"cap": 4}},
        ],
    }


def _trace(*, symbol: str, question: str, run=None, depth=None, **_) -> dict[str, Any]:
    """How does control/data flow through X? A structural call path: who
    reaches X (callers), what X reaches (callees), and the transitive blast
    radius (impact, hop-grouped) — the edges are the trace, and unresolved
    ones are declared in coverage (name-resolution is labeled per node).
    ``refs`` locates X's own sites so the path is anchored. Dataflow
    (taint) is a follow-up — it needs a committed semgrep rules file, so
    the default trace is structural."""
    return {
        "version": "ctx.plan/v1",
        "objective": {"kind": "trace", "question": question},
        "budget": {"wall_seconds": 90},
        "emit": {"sections": ["coverage"]},
        "steps": [
            {"id": "site", "op": "code.refs", "args": {"symbol": symbol}},
            {"id": "into", "op": "code.callers", "args": {"symbol": symbol}},
            {"id": "outof", "op": "code.callees", "args": {"symbol": symbol}},
            {"id": "reach", "op": "code.impact",
             "args": {"symbol": symbol, "depth": int(bounds.explicit(depth, 3))}},
        ],
    }


def _compare(*, question: str, ref_a: str, ref_b: str, **_) -> dict[str, Any]:
    """What differs between two captured runs? The behavioral delta
    (failure-set and template changes with spans), not two full outputs."""
    return {
        "version": "ctx.plan/v1",
        "objective": {"kind": "compare", "question": question},
        "budget": {"wall_seconds": 60},
        "emit": {"sections": ["coverage"]},
        "steps": [
            {"id": "delta", "op": "evidence.diff",
             "args": {"ref_a": ref_a, "ref_b": ref_b}},
        ],
    }


def _verify(*, question: str, command: str, **_) -> dict[str, Any]:
    """What proves this change is correct? The tests that plausibly cover
    the change set, then the suite run under the birth gate — outcome in
    coverage, the run addressable as run:<id>. Execute-class: it runs
    tests, so it is CLI-only (the plan validator rejects test.run on the
    bounded tier)."""
    return {
        "version": "ctx.plan/v1",
        "objective": {"kind": "verify", "question": question},
        "budget": {"wall_seconds": 600},
        "emit": {"sections": ["coverage"]},
        "steps": [
            {"id": "changes", "op": "repo.changed"},
            {"id": "tests", "op": "code.related_tests", "input": "changes"},
            {"id": "run", "op": "test.run", "args": {"command": command}},
        ],
    }


def _review(*, question: str, command: str, **_) -> dict[str, Any]:
    """What changed, what is risky, and what remains under-verified? The
    composition: changed symbols + the tests covering them + a fresh run,
    then the root-cause join over that run's failures against the change
    set (targeted diagnose) with counterevidence. Execute-class."""
    return {
        "version": "ctx.plan/v1",
        "objective": {"kind": "review", "question": question},
        "budget": {"wall_seconds": 600},
        "emit": {"sections": ["conclusion_candidates", "counterevidence", "coverage"]},
        "steps": [
            {"id": "changes", "op": "repo.changed"},
            {"id": "symbols", "op": "code.symbols", "input": "changes"},
            {"id": "tests", "op": "code.related_tests", "input": "changes"},
            {"id": "run", "op": "test.run", "args": {"command": command}},
            {"id": "culprits", "op": "evidence.join",
             "args": {"on": "failing_in_changed"}, "after": ["run", "changes"]},
            {"id": "counter", "op": "evidence.join",
             "args": {"on": "untouched_failures"}, "after": ["run", "changes"]},
        ],
    }


_DEFAULT_TEST_CMD = "python -m pytest -q"

INTENTS: dict[str, Intent] = {
    "locate": Intent("locate", "where is X defined and used", True, _locate),
    "impact": Intent("impact", "what could break if X changes", True, _impact),
    "diagnose": Intent(
        "diagnose", "what explains the captured failures", False, _diagnose
    ),
    "trace": Intent("trace", "how control/data flows through X", True, _trace),
    "compare": Intent(
        "compare", "what differs between two runs", False, _compare, needs_refs=2
    ),
    "verify": Intent(
        "verify", "what proves this change is correct", False, _verify,
        klass="execute", default_command=_DEFAULT_TEST_CMD,
    ),
    "review": Intent(
        "review", "what changed, what is risky, what is under-verified", False,
        _review, klass="execute", default_command=_DEFAULT_TEST_CMD,
    ),
}


# ------------------------------------------------- deterministic inference
# Identifier-shaped: dotted names, snake_case, or CamelCase with an
# internal capital. Deliberately conservative — "Where" and "What" are
# capitalized English, not subjects; a single capitalized word never
# qualifies. Inference fires ONLY when exactly one distinct candidate
# exists (the provably-unambiguous case) and is always disclosed.
_CAMEL_RE = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+$")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def infer_symbol(question: str) -> tuple[str | None, list[str]]:
    """(inferred_symbol_or_None, all_candidates) — pure and total."""
    cands: list[str] = []
    for tok in _TOKEN_RE.findall(question or ""):
        tok = tok.strip(".")
        if not tok or tok in cands:
            continue
        if "." in tok or "_" in tok or _CAMEL_RE.match(tok):
            cands.append(tok)
    return (cands[0] if len(cands) == 1 else None), cands


#: Advisory only: the suggestion rides a teaching ERROR and nothing runs.
_INTENT_HINTS: tuple[tuple[str, str], ...] = (
    ("where", "locate"), ("defined", "locate"), ("definition", "locate"),
    ("used", "locate"), ("find", "locate"),
    ("break", "impact"), ("affected", "impact"), ("impact", "impact"),
    ("depends", "impact"),
    ("why", "diagnose"), ("failing", "diagnose"), ("fails", "diagnose"),
    ("failure", "diagnose"), ("explains", "diagnose"),
    ("flow", "trace"), ("reach", "trace"), ("trace", "trace"),
    ("path", "trace"),
    ("differ", "compare"), ("difference", "compare"), ("changed between", "compare"),
    ("prove", "verify"), ("verify", "verify"), ("correct", "verify"),
    ("review", "review"), ("risky", "review"), ("risk", "review"),
)


#: Question shapes no intent answers, but a single verb does. `ctx ask` is a
#: preset over evidence *plans*; "what implements X" is one graph lookup, so
#: inventing an intent for it would be a plan with one node. Without this the
#: teaching error for those questions was the bare menu — every other shape
#: got a suggestion, and this one got nothing to try next.
#: Stems, not inflections — "extend" has to cover "extends" and "extending",
#: and enumerating forms is how a table like this silently stops matching.
_VERB_HINTS: tuple[tuple[str, str], ...] = (
    ("implement", "impls"), ("subclass", "impls"), ("subtype", "impls"),
    ("extend", "impls"), ("inherit", "impls"), ("derive", "impls"),
)


def suggest_intent(question: str) -> str | None:
    q = (question or "").lower()
    for word, intent in _INTENT_HINTS:
        if word in q:
            return intent
    return None


def suggest_verb(question: str) -> str | None:
    """A `ctx` verb that answers this question outright, or None."""
    q = (question or "").lower()
    for word, verb in _VERB_HINTS:
        if word in q:
            return verb
    return None


# ---------------------------------------------------------------- compiler
def compile_ask(
    intent_name: str | None,
    question: str,
    *,
    symbol: str | None = None,
    run: str | None = None,
    depth: int | None = None,
    ref_a: str | None = None,
    ref_b: str | None = None,
    command: str | None = None,
) -> tuple[str, list[str]]:
    """Slots → canonical ``ctx.plan/v1`` JSON + disclosure lines.

    Raises :class:`AskError` with a teaching line when a slot is missing
    or ambiguous. Determinism: ``json.dumps(sort_keys=True)`` means the
    same slots always compile to the same bytes, so the plan id — and
    therefore every node-cache key — is stable across phrasings that
    resolve to the same slots."""
    menu = " | ".join(sorted(INTENTS))
    if not intent_name:
        verb = suggest_verb(question)
        if verb:
            raise AskError(
                f"ctx ask needs --intent ({menu}); but this question is "
                f"answered outright by `ctx {verb} <Type>` — no plan needed "
                "(advisory — nothing was run)"
            )
        hint = suggest_intent(question)
        raise AskError(
            f"ctx ask needs --intent ({menu})"
            + (
                f"; your question looks like --intent {hint} "
                "(advisory — nothing was run)"
                if hint
                else ""
            )
        )
    it = INTENTS.get(intent_name)
    if it is None:
        close = difflib.get_close_matches(intent_name, sorted(INTENTS), n=1, cutoff=0.4)
        raise AskError(
            f"ctx ask: unknown intent {intent_name!r} ({menu})"
            + (f"; did you mean --intent {close[0]}?" if close else "")
        )

    disclosure = [f"intent: {it.name} — {it.doc}"]
    if it.klass == "execute":
        disclosure.append(
            "class: execute — runs tests under the birth gate (CLI-only; "
            "the bounded MCP tier rejects it)"
        )
    if it.needs_symbol and not symbol:
        inferred, cands = infer_symbol(question)
        if inferred is None:
            if not cands:
                raise AskError(
                    f"ctx ask --intent {it.name} needs a subject: pass "
                    "--symbol <Name> (no identifier-shaped token in the question)"
                )
            raise AskError(
                f"ctx ask --intent {it.name}: ambiguous subject — candidates: "
                + ", ".join(cands[:6])
                + "; pass --symbol <Name>"
            )
        symbol = inferred
        disclosure.append(
            f"subject: {symbol} (inferred — the question's only "
            "identifier-shaped token; --symbol overrides)"
        )
    elif symbol:
        disclosure.append(f"subject: {symbol}")

    if it.needs_refs == 2 and not (ref_a and ref_b):
        raise AskError(
            f"ctx ask --intent {it.name} compares two runs: pass "
            "--run <run:A> --against <run:B>"
        )
    if it.needs_refs == 2:
        disclosure.append(f"comparing: {ref_a} → {ref_b}")
    if it.default_command is not None:
        command = command or it.default_command
        disclosure.append(f"test command: {command}")
    if run:
        disclosure.append(f"run: {run}")

    q_text = (question or "").strip() or f"{it.name}: {symbol or run or 'workspace'}"
    plan = it.compile(
        symbol=symbol, question=q_text, run=run, depth=depth,
        ref_a=ref_a, ref_b=ref_b, command=command,
    )
    return json.dumps(plan, sort_keys=True), disclosure
