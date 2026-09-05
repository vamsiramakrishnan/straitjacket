"""Shared emission boundary for every command that returns a digest.

One resolver, one engagement filter, one bounded() backstop — the
budget choke points named in docs/LADDERS.md edge 8. Imports stay
inside the functions: a command pays only for what it reaches."""

from __future__ import annotations


def _delivery_plan(ws, *, outcome: str, family: str, base_tokens: int, signature=None):
    """One resolver for every emission budget (docs/EDC.md §13, LADDERS
    edge 8): outcome + circuit + signal record + config in, DeliveryPlan
    out. Fail-open inside the resolver by contract."""
    from ctx import resolver

    return resolver.resolve_delivery(
        outcome,
        family,
        contract_rendering={"base_tokens": base_tokens},
        session=resolver.session_state(ws.root, signature),
        environment=resolver.environment_signals(ws.root),
        config_budgets=ws.config.budgets,
    )


_HANDLE_RE = None


def _truncation_handle(text: str) -> str | None:
    """The retrieval address a truncated digest must still end with.

    Two ways a digest loses its handle: ``bounded()`` cuts from the bottom
    and the ``next:`` block is last in every profile, and ``filter_digest``
    drops that block outright at cap 0 (the default for passive and
    lean-model sessions) — so the sessions most likely to need the handle
    were guaranteed not to get it.

    Read from the UNFILTERED digest, so cap 0 does not hide the answer:
    first the digest's own first suggestion (it already names the best next
    address), else a bare `ctx get <handle>` built from the first artifact
    handle in the text. None when the digest names no artifact at all."""
    global _HANDLE_RE
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line == "next:":
            for cand in lines[i + 1:]:
                if cand.startswith("  ") and cand.strip():
                    return cand.strip()
                break
            break
    if _HANDLE_RE is None:
        import re

        _HANDLE_RE = re.compile(r"\b(run|blob|snapshot|checkpoint):([0-9a-f]{6,64})\b")
    m = _HANDLE_RE.search(text)
    return f"ctx get {m.group(1)}:{m.group(2)}" if m else None


def _emit_bounded_digest(ws, store, text: str, plan, continuation=None) -> None:
    """Shared emission boundary: engagement filtering (teaching prose obeys
    both the plan and the graduated-engagement cap), the plan's token
    budget as the bounded() backstop, and the plan receipt telemetry.

    A digest the backstop had to cut always ends with a usable retrieval
    handle — see ``_truncation_handle``. That is not an affordance the
    engagement cap governs: a truncated digest is precisely the signal
    ``engagement.note_truncation`` treats as proof the session outgrew
    'small', so withholding the address there is the one case where
    mechanism C would cost a round trip rather than save one."""
    from ctx import resolver
    from ctx.engagement import filter_digest, suggestion_cap
    from ctx.textutil import bounded

    eng = ws.config.engagement
    cap = suggestion_cap(ws.root, mode=eng.mode, lean_models=eng.lean_models)
    if not plan.include_teaching:
        cap = 0  # include_teaching=False maps to suggestion cap 0
    resolver.record_plan_receipt(store.audit_dir if store is not None else None, plan)
    print(
        bounded(
            filter_digest(text, cap),
            plan.token_budget,
            continuation,
            truncation_continuation=_truncation_handle(text),
        )
    )


def _emit_run_digest(ws, digest: str, manifest: dict, store=None, signature=None) -> int:
    """Shared emission tail for foreground runs and finalized background
    jobs: delivery-plan resolution (budget selection, failure asymmetry,
    window pressure), engagement filtering, and the run's exit-code
    semantics (124 timeout, 3 nonzero, 0 success)."""
    # Zero-hop inline digests may exceed the summary budget by design; the
    # result budget is the hard emission backstop either way.
    base = (
        ws.config.budgets.result_tokens
        if "output (complete):" in digest
        else ws.config.budgets.digest_tokens
    )
    # Failure asymmetry rides through the resolver: a failing run's output
    # is evidence, not boilerplate. exitCode != 0 covers None too: timeouts
    # and signal deaths are failures (docs/LADDERS.md edge 4 — parity with
    # eval's treatment).
    outcome = "success" if manifest["result"]["exitCode"] == 0 else "failure"
    plan = _delivery_plan(
        ws, outcome=outcome, family="run", base_tokens=base, signature=signature
    )
    # Graduated engagement (mechanism C): affordances are filtered at this
    # emission boundary only — the stored digest identity stays canonical.
    _emit_bounded_digest(ws, store, digest, plan)
    result = manifest["result"]
    if result["timedOut"]:
        return 124
    return 0 if result["exitCode"] == 0 else 3


def _emit_retrieval(ws, store, out: str, *, exact: bool = False) -> int:
    """Shared emission tail for every retrieval-path verb (search/get/
    stats/diff/map/code): the ONE budget choke point (LADDERS edge 8).
    ``resolve_retrieval_budget`` returns exactly the configured
    turn-retrieval budget today — the same value ``charge_turn_budget``
    enforces — so behavior is unchanged; the window-pressure hook-in for
    retrieval lands in the resolver, not in seven call sites."""
    from ctx import resolver
    from ctx.retrieval import charge_turn_budget

    resolver.resolve_retrieval_budget(ws.config, resolver.environment_signals(ws.root))
    from ctx.textutil import write_exact

    warning = charge_turn_budget(store, ws, out)
    if warning:
        print(warning)
    # write_exact, not print: `ctx get --bytes` may carry surrogate-escaped
    # bytes, and print() encodes through the stream's own STRICT handler --
    # which would raise on exactly the results the exactness fix preserves.
    #
    # An exact answer also gets no trailing newline. Everywhere else that
    # newline is the shell convention; here it is one byte the caller did not
    # ask for, appended to a slice whose whole promise is that it is the bytes
    # requested and nothing else -- and it cannot be stripped back off, since
    # a payload may legitimately end in \n.
    write_exact(out, newline=not exact)
    return 0


def _emit_investigation(ws, store, text: str) -> None:
    """Emission tail for plan/investigate digests: the shared resolver
    (family 'investigate'; a digest naming failed nodes rides the failure
    budget) + engagement filter + bounded backstop."""
    outcome = "failure" if ("ERROR:" in text or "candidates (census): 0" in text) else "success"  # was inverted: "not in" misclassified successful census as failure
    plan = _delivery_plan(
        ws,
        outcome=outcome,
        family="investigate",
        base_tokens=ws.config.budgets.result_tokens,
    )
    _emit_bounded_digest(ws, store, text, plan)
