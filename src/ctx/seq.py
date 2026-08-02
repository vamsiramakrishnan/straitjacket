"""`ctx seq`: declared command trees — round economy without losing the gates.

Measured motivation (Tura wave, wire replay over five real sessions): 32%
of tool-bearing rounds were mechanical bash-after-bash chains — 70% on
lint-fix, 65% on creation — each costing ~1.5–2s ttfb plus a suffix cache
write. Tura merges such chains by owning the runtime; `ctx seq` merges
them from inside one tool call while keeping what a runtime-owner drops:
every step is a full birth-gate capture (artifact + deterministic digest +
spans), individually addressable as `run:<id>` forever.

Semantics: steps run in order; by default the tree halts at the first
failure (`&&` semantics) and that step's digest is emitted in full
(failure asymmetry — failure is evidence). On success the composite stays
terse: one status line per step plus the final step's digest. The seq's
exit code is the first failure's exit code, else 0.
"""

from __future__ import annotations

from ctx.store import Store
from ctx.textutil import short_id
from ctx.workspace import Workspace


def run_seq(
    ws: Workspace,
    store: Store,
    steps: list[str],
    *,
    halt_on_fail: bool = True,
    timeout: float | None = None,
    focus: str | None = None,
) -> tuple[str, int]:
    """Execute a declared step list; return (composite digest, exit code,
    timed_out).

    ``timed_out`` travels beside the exit code rather than inside it. A step
    that the runner killed on timeout and a step that chose to return 124 are
    different events, and the child's exit code cannot tell them apart -- the
    same overload that made ``ctx py`` misreport ``sys.exit(124)``."""
    from ctx.digest import render_run_digest
    from ctx.execution import ExecutionError, run_capture

    lines_out: list[str] = []
    # (idx, cmd, digest, exit, failed). `failed` is carried rather than
    # re-derived from the exit code: a step killed by the runner can still
    # report exitCode 0, and the selection below has to agree with the ✓/✗
    # already printed beside it.
    step_digests: list[tuple[int, str, str, int, bool]] = []
    final_exit = 0
    any_timed_out = False
    halted_at: int | None = None

    for idx, cmd in enumerate(steps, start=1):
        try:
            capture = run_capture(
                ws, [cmd], shell=True, timeout=timeout, store=store
            )
        except ExecutionError as e:
            lines_out.append(f"step {idx} ✗ failed to start ({e}) · {cmd}")
            final_exit = final_exit or 127
            halted_at = idx
            break
        digest, manifest = render_run_digest(
            store, ws, capture.manifest, focus=focus
        )
        rid = short_id(manifest["id"])
        result = manifest["result"]
        exit_code = result["exitCode"]
        # exitCode None means signal death — failure, not success (S6 finding).
        failed = bool(result["timedOut"]) or exit_code != 0
        mark = "✗" if failed else "✓"
        status = f"exit {exit_code}" if exit_code is not None else f"signal {result['signal']}"
        if result["timedOut"]:
            status += " · timed out"
        lines_out.append(f"step {idx} {mark} {status} · run:{rid} · {cmd}")
        step_digests.append(
            (idx, cmd, digest, exit_code if exit_code is not None else 1, failed)
        )
        if failed:
            # A timeout the RUNNER observed outranks whatever the child
            # returned: previously a killed step fell through to the child's
            # code, so `ctx seq` reported 3 for a timeout the manifest
            # recorded, against the 124 in the docs/CLI.md exit-code table.
            if result["timedOut"]:
                any_timed_out = True
                final_exit = final_exit or 124
            else:
                final_exit = final_exit or (
                    exit_code if exit_code not in (0, None) else 124
                )
            if halt_on_fail:
                halted_at = idx
                break

    header = f"[ctx seq · {len(steps)} steps"
    if halted_at is not None:
        header += f" · halted at step {halted_at}"
        remaining = len(steps) - halted_at
        if remaining:
            header += f" · {remaining} not run"
    header += "]"

    body = [header] + lines_out
    # Failure asymmetry, derived from the rule rather than enumerated:
    # if anything failed, the FIRST failing step's digest rides in full;
    # otherwise the last step's does (the earlier ones were boilerplate by
    # definition, and each stays addressable via run:<id>).
    #
    # It used to be two explicit branches -- halted-at-the-last-step, and
    # green -- which between them missed the third case entirely. Under
    # `--keep-going` a failure sets neither condition, so `detail` stayed
    # None and NO digest was emitted at all: the one time a step actually
    # failed, its captured output was the single thing the summary omitted,
    # directly against this module's own "failure is evidence".
    #
    # FIRST failing, not last: it is the one the halting mode would have
    # stopped at, and later failures are usually its consequences.
    failures = [d for d in step_digests if d[4]]
    detail = failures[0] if failures else (step_digests[-1] if step_digests else None)
    if detail is not None:
        idx, _, digest, _, _ = detail
        body.append(f"--- step {idx} digest ---")
        body.append(digest)
        if len(failures) > 1:
            # Never silently: the other failures are named and addressable.
            others = ", ".join(f"step {d[0]}" for d in failures[1:])
            body.append(
                f"also failed: {others} (digest omitted; each is addressable "
                "via its run: handle above)"
            )
    return "\n".join(body), final_exit, any_timed_out
