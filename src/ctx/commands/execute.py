"""Capture verbs: run · job · jobs · py · seq."""

from __future__ import annotations

import sys

from ctx.commands.emit import (
    _delivery_plan,
    _emit_bounded_digest,
    _emit_run_digest,
)


def cmd_run(ws, ns) -> int:
    from ctx.digest import render_run_digest
    from ctx.execution import ExecutionError, run_capture
    from ctx.store import Store
    from ctx.textutil import short_id

    command = list(ns.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("ctx run: no command given (use: ctx run -- <command> [args...])", file=sys.stderr)
        return 2

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)

    if ns.bg or ns.bg_after is not None:
        return _run_bg(ws, store, ns, command)

    try:
        capture = run_capture(
            ws,
            command,
            cwd=ns.cwd,
            shell=ns.shell,
            timeout=ns.timeout,
            store=store,
        )
    except ExecutionError as e:
        print(f"ctx run: {e}", file=sys.stderr)
        return 1

    # Reflex arc (docs/REFLEX.md layer 3): a signature already intervened on
    # this session re-arriving here IS the starvation loop — check_command
    # scores it (deduped against the hook's sighting of the same re-run) and
    # reports the densify latch. Latched → render the dense census and
    # declare it in the printed header. Fail-open: broken reflex state means
    # a plain digest, never a failed run.
    sig = None
    dense = False
    try:
        import shlex

        from ctx import reflex

        cmd_str = command[0] if ns.shell else shlex.join(command)
        sig = reflex.command_signature(cmd_str)
        if sig:
            dense = reflex.check_command(ws.root, cmd_str) == "densify" or (
                reflex.densify_latched(ws.root, sig)
            )
    except Exception:
        sig, dense = None, False

    # EDC phase 4: resolve the delivery plan and hand it to the renderer
    # when the digest layer accepts it (duck-typed `plan=` kwarg; absent →
    # legacy rendering, byte-identical by construction). The render-time
    # plan uses the digest base budget; the emission backstop re-resolves
    # with the actual zero-hop marker in `_emit_run_digest`.
    render_kwargs = {}
    try:
        import inspect

        if "plan" in inspect.signature(render_run_digest).parameters:
            outcome = (
                "success" if capture.manifest["result"]["exitCode"] == 0 else "failure"
            )
            render_kwargs["plan"] = _delivery_plan(
                ws,
                outcome=outcome,
                family="run",
                base_tokens=ws.config.budgets.digest_tokens,
                signature=sig,
            )
    except Exception:
        render_kwargs = {}

    digest, manifest = render_run_digest(
        store, ws, capture.manifest, focus=ns.focus, dense=dense, **render_kwargs
    )
    # A digest that omitted content is an intervention (hypothesis: the model
    # uses the digest, not a re-run). Record it so the reflex arc can score
    # the next command against it.
    try:
        if sig:
            from ctx import reflex

            if reflex.has_omissions(digest):
                short = short_id(manifest.get("id", ""))
                reflex.note_intervention(
                    ws.root, sig, short, hints=reflex.count_hints(digest)
                )
    except Exception:
        pass
    if dense:
        # Printed declaration only — the stored digest identity/meta hash is
        # computed inside render_run_digest and never sees reflex state.
        from ctx.reflex import DENSIFY_HEADER

        digest = DENSIFY_HEADER + "\n" + digest
    return _emit_run_digest(ws, digest, manifest, store=store, signature=sig)


def _run_bg(ws, store, ns, command: list[str]) -> int:
    """`ctx run --bg / --bg-after T`: supervised launch, then a bounded
    patience window. Finished in time → the normal digest, byte-identical
    to a foreground run. Still running → job handle, exit 0."""
    from ctx.jobs import (
        JobError,
        backgrounded_status,
        finalize_job,
        start_job,
        wait_for_done,
    )
    from ctx.workspace import WorkspaceError

    patience = ns.bg_after if ns.bg_after is not None else 0.0  # --bg ⇒ 0
    try:
        job_id = start_job(
            ws, store, command,
            cwd=ns.cwd, shell=ns.shell, timeout=ns.timeout, focus=ns.focus,
        )
    except (JobError, WorkspaceError) as e:
        print(f"ctx run: {e}", file=sys.stderr)
        return 1
    try:
        if wait_for_done(store, job_id, timeout=max(0.0, patience)):
            digest, manifest = finalize_job(ws, store, job_id)
            return _emit_run_digest(ws, digest, manifest, store=store)
        print(backgrounded_status(store, job_id))
        return 0
    except JobError as e:
        print(f"ctx run: {e}", file=sys.stderr)
        return 1


def cmd_job(ws, ns) -> int:
    from ctx.jobs import (
        JobError,
        finalize_job,
        job_state,
        job_status,
        kill_job,
        resolve_job_id,
        wait_for_done,
    )
    from ctx.store import Store
    from ctx.textutil import short_id

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        job_id = resolve_job_id(store, ns.job_id)
        if ns.kill:
            digest, manifest = kill_job(ws, store, job_id)
            short = short_id(manifest["id"])
            print(f"[ctx job:{job_id} killed · finalized → run:{short}]")
            _emit_run_digest(ws, digest, manifest, store=store)
            return 0
        if ns.wait:
            if not wait_for_done(store, job_id, timeout=ns.timeout):
                print(job_status(store, job_id, tail=ns.tail))
                return 124
            digest, manifest = finalize_job(ws, store, job_id)
            short = short_id(manifest["id"])
            print(f"[ctx job:{job_id} finalized → run:{short}]")
            return _emit_run_digest(ws, digest, manifest, store=store)
        state = job_state(store, job_id)
        if state in ("done", "finalized"):
            digest, manifest = finalize_job(ws, store, job_id)
            short = short_id(manifest["id"])
            print(f"[ctx job:{job_id} finalized → run:{short}]")
            _emit_run_digest(ws, digest, manifest, store=store)
            return 0
        status = job_status(store, job_id, tail=ns.tail)
        print(status)
        # 3, not 1: "the thing you asked about failed" is not "ctx failed".
        # run/py/seq already draw that line (see _emit_run_digest), and the
        # 1 here collided with this function's own JobError handler on the
        # next line — a caller could not tell a failed job from a bad job id.
        return 3 if state == "failed" else 0
    except JobError as e:
        print(f"ctx job: {e}", file=sys.stderr)
        return 1


def cmd_jobs(ws, ns=None) -> int:
    """`ctx jobs` takes no options; ns is accepted for dispatch uniformity."""
    from ctx.jobs import list_jobs
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(list_jobs(store))
    return 0


def cmd_py(ws, ns) -> int:
    from ctx.execution import ExecutionError
    from ctx.pyeval import run_eval
    from ctx.store import Store

    if ns.file:
        full = ws.confine(ns.file, must_exist=True)
        rel = ws.relativize(full)
        if ws.is_ignored(rel):
            print(f"ctx py: path is excluded from capture by policy: {rel}", file=sys.stderr)
            return 1
        script = full.read_text(encoding="utf-8")
    elif ns.script in (None, "-"):
        if sys.stdin.isatty():
            print(
                "ctx py: no script given (pass text, --file <path>, or pipe stdin)",
                file=sys.stderr,
            )
            return 2
        script = sys.stdin.read()
    else:
        script = ns.script

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        text, code = run_eval(
            ws, store, script, timeout=ns.timeout, cwd=ns.cwd, focus=ns.focus
        )
    except ExecutionError as e:
        print(f"ctx py: {e}", file=sys.stderr)
        return 1

    # Delivery plan (EDC §13): zero-hop inline uses the result budget;
    # failure asymmetry (a failing script's traceback is evidence — timeout
    # 124 included, docs/LADDERS.md edge 4) and window pressure compose in
    # the resolver, floor-protected.
    base = (
        ws.config.budgets.result_tokens
        if "output (complete):" in text
        else ws.config.budgets.digest_tokens
    )
    plan = _delivery_plan(
        ws,
        outcome="success" if code == 0 else "failure",
        family="eval",
        base_tokens=base,
    )
    _emit_bounded_digest(ws, store, text, plan)
    if code == 124:
        return 124
    return 0 if code == 0 else 3


def cmd_seq(ws, ns) -> int:
    """`ctx seq` — run several commands as one step."""
    from ctx.seq import run_seq
    from ctx.store import Store as _Store

    store = _Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    text, code = run_seq(
        ws, store, ns.steps,
        halt_on_fail=not ns.keep_going,
        timeout=ns.timeout, focus=ns.focus,
    )
    # Delivery plan (EDC §13): seq always emits against the result
    # budget; failure asymmetry + pressure compose in the resolver.
    # Engagement parity with run/eval (docs/LADDERS.md edge 1): lean
    # or passive sessions must not pay for suggestion lines here
    # either — _emit_bounded_digest applies the same filter.
    plan = _delivery_plan(
        ws,
        outcome="success" if code == 0 else "failure",
        family="seq",
        base_tokens=ws.config.budgets.result_tokens,
    )
    _emit_bounded_digest(ws, store, text, plan)
    return 0 if code == 0 else 3
