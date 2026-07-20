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


# ------------------------------------------------------------------ presets
def _locate(*, symbol: str, question: str, run=None, depth=None) -> dict[str, Any]:
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


def _impact(*, symbol: str, question: str, run=None, depth=None) -> dict[str, Any]:
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
             "args": {"symbol": symbol, "depth": int(depth or 3)}},
            {"id": "tests", "op": "code.related_tests", "input": "blast"},
            {"id": "changes", "op": "repo.changed"},
        ],
    }


def _diagnose(*, question: str, symbol=None, run=None, depth=None) -> dict[str, Any]:
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


INTENTS: dict[str, Intent] = {
    "locate": Intent("locate", "where is X defined and used", True, _locate),
    "impact": Intent("impact", "what could break if X changes", True, _impact),
    "diagnose": Intent(
        "diagnose", "what explains the captured failures", False, _diagnose
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
)


def suggest_intent(question: str) -> str | None:
    q = (question or "").lower()
    for word, intent in _INTENT_HINTS:
        if word in q:
            return intent
    return None


# ---------------------------------------------------------------- compiler
def compile_ask(
    intent_name: str | None,
    question: str,
    *,
    symbol: str | None = None,
    run: str | None = None,
    depth: int | None = None,
) -> tuple[str, list[str]]:
    """Slots → canonical ``ctx.plan/v1`` JSON + disclosure lines.

    Raises :class:`AskError` with a teaching line when a slot is missing
    or ambiguous. Determinism: ``json.dumps(sort_keys=True)`` means the
    same slots always compile to the same bytes, so the plan id — and
    therefore every node-cache key — is stable across phrasings that
    resolve to the same slots."""
    menu = " | ".join(sorted(INTENTS))
    if not intent_name:
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
    if run:
        disclosure.append(f"run: {run}")

    q_text = (question or "").strip() or f"{it.name}: {symbol or run or 'workspace'}"
    plan = it.compile(symbol=symbol, question=q_text, run=run, depth=depth)
    return json.dumps(plan, sort_keys=True), disclosure
