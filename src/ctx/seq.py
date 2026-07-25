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
    """Execute a declared step list; return (composite digest, exit code)."""
    from ctx.digest import render_run_digest
    from ctx.execution import ExecutionError, run_capture

    lines_out: list[str] = []
    step_digests: list[tuple[int, str, str, int]] = []  # (idx, cmd, digest, exit)
    final_exit = 0
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
        step_digests.append((idx, cmd, digest, exit_code if exit_code is not None else 1))
        if failed:
            final_exit = final_exit or (exit_code if exit_code not in (0, None) else 124)
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
    # Failure asymmetry: the failing step's digest rides in full; on green
    # trees only the final step's digest is included (the others were
    # boilerplate by definition — each remains addressable via run:<id>).
    detail: tuple[int, str, str, int] | None = None
    if halted_at is not None and step_digests and step_digests[-1][0] == halted_at:
        detail = step_digests[-1]
    elif step_digests and final_exit == 0:
        detail = step_digests[-1]
    if detail is not None:
        idx, _, digest, _ = detail
        body.append(f"--- step {idx} digest ---")
        body.append(digest)
    return "\n".join(body), final_exit
